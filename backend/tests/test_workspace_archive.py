import io
import tarfile
import unittest

from app.workspace.archive import read_file_archive


class Stream:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def __iter__(self):
        yield self.data

    def close(self):
        self.closed = True


def make_archive(names, kind=tarfile.REGTYPE, content=b"hello"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in names:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(entry, io.BytesIO(content))
    return Stream(buffer.getvalue())


class FileArchiveTests(unittest.TestCase):
    def test_read_and_close(self):
        stream = make_archive(["booking.py"])
        self.assertEqual(read_file_archive(stream, "src/booking.py"), "hello")
        self.assertTrue(stream.closed)

    def test_reject_unsafe_entries_and_close(self):
        for names, kind in [
            (["../booking.py"], tarfile.REGTYPE),
            (["booking.py"], tarfile.SYMTYPE),
            (["booking.py"], tarfile.LNKTYPE),
            (["booking.py", "extra"], tarfile.REGTYPE),
            ([], tarfile.REGTYPE),
        ]:
            stream = make_archive(names, kind)
            with self.subTest(names=names, kind=kind), self.assertRaises(ValueError):
                read_file_archive(stream, "src/booking.py")
            self.assertTrue(stream.closed)

    def test_oversized_stream_is_closed(self):
        stream = Stream(b"x" * (2 * 1024 * 1024))
        with self.assertRaises(ValueError):
            read_file_archive(stream, "src/booking.py")
        self.assertTrue(stream.closed)

    def test_invalid_utf8_is_closed(self):
        stream = make_archive(["booking.py"], content=b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            read_file_archive(stream, "src/booking.py")
        self.assertTrue(stream.closed)
