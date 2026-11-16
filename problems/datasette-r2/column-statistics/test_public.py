import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from datasette.app import Datasette


class ColumnStatistics(unittest.IsolatedAsyncioTestCase):
    async def test_filtered_page_exposes_statistics_context(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "data.db"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    create table items (id integer primary key, category text, score integer);
                    insert into items values (1, 'A', 10), (2, 'B', 20), (3, 'A', 30);
                """)
            conn.close()
            ds = Datasette([path])
            try:
                response = await ds.client.get("/data/items?category=A&_size=1&_next=1")
                self.assertEqual(response.status_code, 200)
                match = re.search(
                    r"window\._datasetteTableData\s*=\s*({.*?});", response.text
                )
                self.assertIsNotNone(match)
                data = json.loads(match.group(1))
                self.assertIn("executeSql", data)
                stats = data["executeSql"]
                self.assertEqual(stats["queryUrl"], "/data/-/query.json")
                self.assertEqual(
                    stats["columns"],
                    [
                        {"name": "id", "type": "integer"},
                        {"name": "category", "type": "text"},
                        {"name": "score", "type": "integer"},
                    ],
                )
                self.assertEqual(list(stats["filter"]["params"].values()), ["A"])
            finally:
                ds.close()
