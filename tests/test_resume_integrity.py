import importlib.util
import tempfile
import unittest
from pathlib import Path
from threading import Event

from requests import Session


MODULE_PATH = Path(__file__).resolve().parents[1] / "gofile_downloader.py"
spec = importlib.util.spec_from_file_location("gofile_downloader", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
Downloader = module.Downloader


class FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Length": "4"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_content(self, chunk_size=1):
        yield b"test"


class ResumeIntegrityTests(unittest.TestCase):
    def test_parse_expected_size(self):
        self.assertEqual(Downloader._parse_expected_size("123"), 123)
        self.assertIsNone(Downloader._parse_expected_size(None))
        self.assertIsNone(Downloader._parse_expected_size("bad"))

    def test_final_file_not_complete_without_expected_size(self):
        self.assertFalse(Downloader._is_complete_file(100, None))
        self.assertTrue(Downloader._is_complete_file(100, 100))
        self.assertTrue(Downloader._is_complete_file(101, 100))

    def test_partial_size_classification(self):
        self.assertEqual(Downloader._evaluate_partial_size(0, 100), "fresh")
        self.assertEqual(Downloader._evaluate_partial_size(50, 100), "resume")
        self.assertEqual(Downloader._evaluate_partial_size(100, 100), "complete")
        self.assertEqual(Downloader._evaluate_partial_size(150, 100), "restart")

    def test_finalize_only_renames_on_exact_size(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            part_file = base / "file.bin.part"
            final_file = base / "file.bin"
            part_file.write_bytes(b"abc")
            file_info = {"path": str(base), "filename": "file.bin"}

            finalized = Downloader._finalize_download(file_info, str(part_file), 4)
            self.assertFalse(finalized)
            self.assertTrue(part_file.exists())
            self.assertFalse(final_file.exists())

            part_file.write_bytes(b"abcd")
            finalized = Downloader._finalize_download(file_info, str(part_file), 4)
            self.assertTrue(finalized)
            self.assertFalse(part_file.exists())
            self.assertTrue(final_file.exists())

    def test_range_unsupported_triggers_restart(self):
        downloader = Downloader(
            root_dir=".",
            interactive=False,
            max_workers=1,
            number_retries=1,
            timeout=1.0,
            chunk_size=1024,
            stop_event=Event(),
            session=Session(),
            url="https://gofile.io/d/example",
        )

        downloader._get_response = lambda **kwargs: FakeResponse(status_code=200)
        has_size, should_restart = downloader._perform_download(
            file_info={"filename": "file.bin"},
            url="https://example.com/file.bin",
            tmp_file="/tmp/unused.part",
            headers={"Range": "bytes=2-"},
            part_size=2,
        )

        self.assertIsNone(has_size)
        self.assertTrue(should_restart)


if __name__ == "__main__":
    unittest.main()
