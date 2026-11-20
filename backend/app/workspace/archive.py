"""Read one Docker archive without extracting candidate-controlled paths."""

import io
import tarfile

from .files import MAX_FILE_BYTES, validate_path


def read_file_archive(stream, path, *, validator=validate_path):
    """Consume and close a Docker get_archive stream; accept one regular file.

    The caller must authenticate, hold the session guard and verify the original
    container binding before requesting /workspace/<path> from Docker.
    """
    try:
        validator(path)
        data = bytearray()
        for chunk in stream:
            if len(data) + len(chunk) > MAX_FILE_BYTES + 64 * 1024:
                raise ValueError("file archive exceeds limit")
            data.extend(chunk)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            entry = archive.next()
            if (
                entry is None
                or not entry.isfile()
                or entry.name != path.rsplit("/", 1)[-1]
                or not 0 <= entry.size <= MAX_FILE_BYTES
            ):
                raise ValueError("unexpected file archive entry")
            with archive.extractfile(entry) as source:
                content = source.read(MAX_FILE_BYTES + 1)
            if len(content) != entry.size or archive.next() is not None:
                raise ValueError("unexpected file archive contents")
            return content.decode("utf-8")
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
