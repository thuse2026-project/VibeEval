import hashlib
import tarfile
import unittest

from examples.build_tinydb_images import context
from examples.r2_exam import source_files


class TinyDBMaterialsTests(unittest.TestCase):
    def test_build_context_contains_only_each_published_repository(self):
        manifests = []
        for folder in ("01-explore", "03-review"):
            expected = source_files(folder)
            self.assertTrue(expected)
            with (
                context(folder, "test-base") as stream,
                tarfile.open(fileobj=stream) as archive,
            ):
                self.assertEqual(set(archive.getnames()), {"Dockerfile", *expected})
                for path, sha in expected.items():
                    self.assertTrue(path.startswith("repository/"))
                    self.assertEqual(
                        hashlib.sha256(archive.extractfile(path).read()).hexdigest(),
                        sha,
                    )
            manifests.append(expected)
        self.assertNotEqual(*manifests)
