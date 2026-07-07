import importlib.util
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from requests import Session
from requests.exceptions import ConnectionError


MODULE_PATH = Path(__file__).resolve().parents[1] / "gofile_downloader.py"
spec = importlib.util.spec_from_file_location("gofile_downloader", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
Downloader = module.Downloader
DEFAULT_PROBE_BYTES = module.DEFAULT_PROBE_BYTES


class FakeResponse:
    def __init__(self, status_code=200, headers=None, payload=b"test"):
        self.status_code = status_code
        self.headers = headers or {"Content-Length": str(len(payload))}
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_content(self, chunk_size=1):
        yield self._payload


def _make_downloader(probe_bytes: int = 4, **kwargs) -> Downloader:
    defaults = {
        "root_dir": ".",
        "interactive": False,
        "max_workers": 1,
        "number_retries": 1,
        "timeout": 1.0,
        "chunk_size": 1024,
        "stop_event": Event(),
        "session": Session(),
        "url": "https://gofile.io/d/example",
        "probe_bytes": probe_bytes,
    }
    defaults.update(kwargs)
    return Downloader(**defaults)


class ResumeIntegrityTests(unittest.TestCase):
    def test_default_probe_bytes_constant(self):
        self.assertEqual(DEFAULT_PROBE_BYTES, 4096)

    def test_parse_expected_size(self):
        self.assertEqual(Downloader._parse_expected_size("123"), 123)
        self.assertIsNone(Downloader._parse_expected_size(None))
        self.assertIsNone(Downloader._parse_expected_size("bad"))

    def test_final_file_requires_exact_expected_size(self):
        self.assertFalse(Downloader._is_complete_file(100, None))
        self.assertTrue(Downloader._is_complete_file(100, 100))
        self.assertFalse(Downloader._is_complete_file(101, 100))
        self.assertFalse(Downloader._is_complete_file(99, 100))

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
        downloader = _make_downloader()
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

    def test_backoff_delay_is_capped(self):
        self.assertEqual(Downloader._backoff_delay(0, 5.0), 5.0)
        self.assertEqual(Downloader._backoff_delay(3, 5.0), 40.0)
        self.assertEqual(Downloader._backoff_delay(10, 5.0), module.MAX_RETRY_DELAY)

    def test_download_retries_after_connection_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            part_file = base / "file.bin.part"
            part_file.write_bytes(b"partial")
            file_info = {
                "path": str(base),
                "filename": "file.bin",
                "link": "https://example.com/file.bin",
                "size": "10",
            }
            downloader = Downloader(
                root_dir=str(base),
                interactive=False,
                max_workers=1,
                number_retries=2,
                timeout=1.0,
                chunk_size=1024,
                stop_event=Event(),
                session=Session(),
                url="https://gofile.io/d/example",
                retry_delay=0.01,
            )
            calls: list[int] = []

            def fake_perform(*_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionError("network dropped")
                part_file.write_bytes(b"0123456789")
                return "10", False

            downloader._perform_download = fake_perform
            downloader._verify_partial_head = lambda *_args, **_kwargs: True

            with patch.object(downloader, "_wait_before_retry"):
                downloader._download_content(file_info)

            self.assertEqual(len(calls), 2)
            self.assertTrue((base / "file.bin").exists())

    def test_verify_file_integrity_checks_head_and_tail(self):
        payload = b"HEAD----BODY----TAIL"
        downloader = _make_downloader(probe_bytes=4)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name

        try:
            def fetch_range(url, start, end):
                return payload[start : end + 1]

            downloader._fetch_byte_range = fetch_range
            self.assertTrue(
                downloader._verify_file_integrity("https://example.com/file.bin", tmp_path, len(payload))
            )

            corrupted = bytearray(payload)
            corrupted[-1] = ord("X")
            Path(tmp_path).write_bytes(corrupted)
            self.assertFalse(
                downloader._verify_file_integrity("https://example.com/file.bin", tmp_path, len(payload))
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_verify_partial_head_checks_first_bytes(self):
        payload = b"abcdefghij"
        downloader = _make_downloader(probe_bytes=4)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(payload[:6])
            tmp_path = tmp.name

        try:
            downloader._fetch_byte_range = lambda url, start, end: payload[start : end + 1]
            self.assertTrue(downloader._verify_partial_head("https://example.com/file.bin", tmp_path, 6))

            Path(tmp_path).write_bytes(b"WXYZefghij")
            self.assertFalse(downloader._verify_partial_head("https://example.com/file.bin", tmp_path, 6))
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_probe_disabled_skips_integrity_checks(self):
        downloader = _make_downloader(probe_bytes=0)
        self.assertTrue(
            downloader._verify_file_integrity("https://example.com/file.bin", __file__, 1_000_000)
        )
        self.assertTrue(
            downloader._verify_partial_head("https://example.com/file.bin", __file__, 1_000_000)
        )

    def test_partial_final_file_without_part_is_moved_for_resume(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            final_file = base / "file.bin"
            final_file.write_bytes(b"abcd")
            file_info = {
                "path": str(base),
                "filename": "file.bin",
                "link": "https://example.com/file.bin",
                "size": "8",
            }

            downloader = _make_downloader(probe_bytes=4)
            downloader._fetch_byte_range = lambda url, start, end: b"abcd"[start : end + 1]
            downloader._perform_download = lambda *args, **kwargs: ("8", False)
            downloader._finalize_download = lambda *args, **kwargs: True

            downloader._download_content(file_info)

            self.assertFalse(final_file.exists())
            self.assertTrue((base / "file.bin.part").exists())
            self.assertEqual((base / "file.bin.part").read_bytes(), b"abcd")

    def test_corrupted_partial_without_part_is_redownloaded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            final_file = base / "file.bin"
            final_file.write_bytes(b"WXYZ")
            file_info = {
                "path": str(base),
                "filename": "file.bin",
                "link": "https://example.com/file.bin",
                "size": "8",
            }

            downloader = _make_downloader(probe_bytes=4)
            downloader._fetch_byte_range = lambda url, start, end: b"abcd"[start : end + 1]

            perform_calls: list[int] = []

            def fake_perform(file_info, url, tmp_file, headers, part_size, task_id=None):
                perform_calls.append(part_size)
                return "8", False

            downloader._perform_download = fake_perform
            downloader._finalize_download = lambda *args, **kwargs: True

            downloader._download_content(file_info)

            self.assertFalse(final_file.exists())
            self.assertFalse((base / "file.bin.part").exists())
            self.assertEqual(perform_calls, [0])

    def test_corrupted_complete_size_file_is_redownloaded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            final_file = base / "file.bin"
            payload = b"HEAD----BODY----TAIL"
            final_file.write_bytes(payload)
            file_info = {
                "path": str(base),
                "filename": "file.bin",
                "link": "https://example.com/file.bin",
                "size": str(len(payload)),
            }

            downloader = _make_downloader(probe_bytes=4)

            def fetch_range(url, start, end):
                remote = bytearray(payload)
                remote[-1] = ord("X")
                return bytes(remote[start : end + 1])

            downloader._fetch_byte_range = fetch_range

            perform_calls: list[int] = []

            def fake_perform(file_info, url, tmp_file, headers, part_size, task_id=None):
                perform_calls.append(part_size)
                return str(len(payload)), False

            downloader._perform_download = fake_perform
            downloader._finalize_download = lambda *args, **kwargs: True

            downloader._download_content(file_info)

            self.assertFalse(final_file.exists())
            self.assertEqual(perform_calls, [0])


if __name__ == "__main__":
    unittest.main()
