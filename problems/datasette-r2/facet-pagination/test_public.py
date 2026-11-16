import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from datasette.app import Datasette


class FacetPagination(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        path = Path(root.name) / "data.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                create table items (id integer primary key, category text, score integer);
                insert into items values
                    (1, 'A', 10), (2, 'B', 20), (3, 'A', 30), (4, 'C', 40);
            """)
        conn.close()
        self.ds = Datasette([path])
        self.addCleanup(self.ds.close)
        first = await self.ds.client.get("/data/items.json?_facet=category&_size=2")
        self.assertEqual(first.status_code, 200)
        second = await self.ds.client.get(
            first.json()["next_url"].removeprefix("http://localhost")
        )
        self.assertEqual(second.status_code, 200)
        self.buckets = second.json()["facet_results"]["results"]["category"]["results"]

    async def test_second_page_counts_all_matching_rows(self):
        self.assertEqual(
            {row["value"]: row["count"] for row in self.buckets},
            {"A": 2, "B": 1, "C": 1},
        )

    async def test_toggle_restarts_at_first_page(self):
        link = next(row["toggle_url"] for row in self.buckets if row["value"] == "A")
        args = parse_qsl(urlsplit(link).query)
        self.assertNotIn("_next", dict(args))
        self.assertEqual(dict(args)["category"], "A")
        self.assertEqual(dict(args)["_size"], "2")
