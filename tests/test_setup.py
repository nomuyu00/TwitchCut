"""Small offline checks for recipient configuration and honest readiness reports."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import setup_env


class SetupTests(unittest.TestCase):
    def test_init_never_overwrites_recipient_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "twitchcut.local.json"
            self.assertTrue(setup_env.init_config(path))
            initial = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(initial["asr"]["device"], "cpu")
            self.assertEqual(initial["asr"]["compute_type"], "int8")
            self.assertEqual(initial["workspace"], "./workspace")
            self.assertTrue(all(value == "" for key, value in initial["profile"].items() if key != "verified_related_links"))
            path.write_text('{"my_existing_setting": "keep me"}', encoding="utf-8")
            before = path.read_bytes()
            self.assertFalse(setup_env.init_config(path))
            self.assertEqual(path.read_bytes(), before)

    def test_even_invalid_existing_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "twitchcut.local.json"
            path.write_bytes(b"not valid JSON\n")
            self.assertFalse(setup_env.init_config(path))
            self.assertEqual(path.read_bytes(), b"not valid JSON\n")

    def test_config_path_resolution_uses_config_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            self.assertEqual(setup_env.local_path("./workspace", base), base.resolve() / "workspace")
            self.assertEqual(setup_env.path_value(base / "tool.exe", base), "./tool.exe")

    def test_bad_explicit_tool_does_not_fall_back_to_path(self):
        with mock.patch.object(setup_env.shutil, "which", return_value="good-ffmpeg.exe"):
            path = setup_env.find_tool("ffmpeg", "./missing/ffmpeg.exe", Path.cwd())
            self.assertTrue(path.endswith(str(Path("missing") / "ffmpeg.exe")))
            self.assertNotEqual(path, "good-ffmpeg.exe")

    def test_probe_rejects_nonzero_or_unexpected_output(self):
        result = mock.Mock(returncode=1, stdout="ffmpeg version 9.0.2", stderr="")
        with mock.patch.object(setup_env.subprocess, "run", return_value=result):
            self.assertFalse(setup_env.probe("ffmpeg", ["-version"], "ffmpeg")[0])
            self.assertTrue(setup_env.probe("ffmpeg", ["-version"], "ffmpeg", (0, 1))[0])
            self.assertFalse(setup_env.probe("ffmpeg", ["-version"], "TwitchDownloaderCLI", (0, 1))[0])

    def test_missing_dependencies_fail_and_doctor_does_not_create_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "missing.json"
            with mock.patch.object(setup_env, "find_tool", return_value=""), mock.patch.object(setup_env, "module_check", return_value=(False, "not installed")):
                report = setup_env.doctor(config)
            self.assertFalse(report["setup_ready"])
            self.assertFalse(report["production_ready"])
            self.assertFalse(report["upload_ready"])
            self.assertFalse(report["profile_ready"])
            self.assertFalse(config.exists())
            self.assertTrue(any(item["name"] == "ffmpeg" and item["status"] == "error" for item in report["checks"]))

    def test_blank_profile_stays_pending_not_setup_error(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            setup_env.init_config(config)
            with mock.patch.object(setup_env, "find_tool", return_value=""), mock.patch.object(setup_env, "module_check", return_value=(True, "test")):
                report = setup_env.doctor(config)
            check = next(item for item in report["checks"] if item["name"] == "recipient_profile")
            self.assertEqual(check["status"], "pending")
            self.assertFalse(check["required"])
            self.assertFalse(report["production_ready"])

    def test_require_asr_changes_missing_runtime_to_error(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "missing.json"
            with mock.patch.object(setup_env, "find_tool", return_value=""), mock.patch.object(setup_env, "module_check", return_value=(False, "not installed")):
                report = setup_env.doctor(config, require_asr=True)
            check = next(item for item in report["checks"] if item["name"] == "asr_runtime")
            self.assertEqual(check["status"], "error")
            self.assertTrue(check["required"])

    def test_malformed_schema_is_reported_without_crashing(self):
        self.assertTrue(setup_env.validate_config([]))
        self.assertTrue(setup_env.validate_config({"schema": 1, "workspace": "./workspace", "profile": [], "tools": {}, "render": {}, "asr": {}}))
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text("[]", encoding="utf-8")
            with mock.patch.object(setup_env, "find_tool", return_value=""), mock.patch.object(setup_env, "module_check", return_value=(False, "not installed")):
                report = setup_env.doctor(config)
            self.assertFalse(report["setup_ready"])
            self.assertTrue(any(item["name"] == "config" and item["status"] == "error" for item in report["checks"]))


if __name__ == "__main__":
    unittest.main()
