from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jqqlib.publish import publish_dataset_build


class PublishDatasetBuildTests(unittest.TestCase):
    def test_publish_dataset_build_creates_provider_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build = root / "builds" / "dataset-id" / "qlib_jp"
            build.mkdir(parents=True)
            provider = root / "data" / "qlib_jp"

            published = publish_dataset_build(build, provider)

            self.assertEqual(published, provider)
            self.assertTrue(provider.is_symlink())
            self.assertEqual(provider.resolve(), build.resolve())

    def test_publish_dataset_build_refuses_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build = root / "builds" / "dataset-id" / "qlib_jp"
            build.mkdir(parents=True)
            provider = root / "data" / "qlib_jp"
            provider.mkdir(parents=True)

            with self.assertRaises(FileExistsError):
                publish_dataset_build(build, provider)


if __name__ == "__main__":
    unittest.main()
