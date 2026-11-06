import io
import tarfile
import unittest

from app.sandbox.submission_bridge import decode_workspace


def archive(name, kind=tarfile.REGTYPE):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.size = 1 if kind == tarfile.REGTYPE else 0
        tar.addfile(item, io.BytesIO(b"x"))
    return buf.getvalue()


class ExportTests(unittest.TestCase):
    def test_text(self):
        self.assertEqual(
            decode_workspace(archive("workspace/src/a.py"))[0].content, "x"
        )

    def test_traversal(self):
        with self.assertRaises(ValueError):
            decode_workspace(archive("workspace/../a"))

    def test_symlink(self):
        with self.assertRaises(ValueError):
            decode_workspace(archive("workspace/a", tarfile.SYMTYPE))

    def test_hardlink(self):
        with self.assertRaises(ValueError):
            decode_workspace(archive("workspace/a", tarfile.LNKTYPE))
