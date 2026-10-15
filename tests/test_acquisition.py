"""Recipient acquisition/resume checks. Synthetic bytes only; no VOD/model network."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import download
import transcribe
import workflow
import twitchcut
from common import write_json, read_json


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / ".test-work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="acquisition-", dir=scratch)
        self.root = Path(self.temp.name)
        self.settings = {"schema": 1, "_config_path": str(self.root / "config.json"),
                         "workspace": "./workspace", "profile": {"twitch_login": "recipient"}}
        self.url = "https://www.twitch.tv/videos/123456789"
        self.metadata = {"data": {"video": {"title": "Synthetic fixture", "createdAt": "2026-10-01T00:00:00Z",
                         "lengthSeconds": 60, "owner": {"login": "recipient", "displayName": "Recipient"}}}}
        self.chat = {"video": {"id": "123456789", "start": 0, "end": 60}, "streamer": {"login": "recipient"}, "comments": []}
        self.media = {"format": {"duration": "60"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    def tearDown(self):
        self.temp.cleanup()

    def simulated_download(self, args, log):
        target = Path(args[args.index("--output") + 1])
        if args[1] == "videodownload":
            target.write_bytes(b"synthetic media bytes, not a playable VOD")
        else:
            write_json(target, self.chat)
        Path(log).write_text("Synthetic download completed\n", encoding="utf-8")

    def mocked_io(self):
        stack = ExitStack()
        stack.enter_context(patch.object(download, "tool", side_effect=lambda settings, name: name))
        stack.enter_context(patch.object(download, "probe_media", return_value=self.media))
        stack.enter_context(patch.object(download, "run_logged", side_effect=lambda args, log: Path(log).write_text("", encoding="utf-8")))
        stack.enter_context(patch.object(download.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(self.metadata) + "\n#EXTM3U\n", stderr="")))
        stack.enter_context(patch.object(download, "downloader_run", side_effect=self.simulated_download))
        return stack

    def make_job(self):
        with self.mocked_io():
            return download.download(self.settings, self.url)

    def test_twitch_only_download_reuse_and_later_youtube_profile(self):
        result = self.make_job()
        job = workflow.job_at(result["job"])
        self.assertEqual(job["profile"]["youtube_channel_id"], "")
        with patch.object(download.subprocess, "run", side_effect=AssertionError("Unexpected network")):
            reused = download.download(self.settings, self.url)
        self.assertTrue(reused["reused"])
        self.settings["profile"]["youtube_channel_id"] = "UC" + "x" * 22
        twitchcut.sync_empty_profile(self.settings, result["job"])
        self.assertEqual(workflow.job_at(result["job"])["profile"]["youtube_channel_id"], "UC" + "x" * 22)

    def test_resume_after_one_final_rename_does_not_redownload(self):
        original = Path.rename

        def interrupted_rename(path, target):
            if path.name == "chat.partial.json":
                raise OSError("simulated power interruption")
            return original(path, target)

        with self.mocked_io(), patch.object(Path, "rename", interrupted_rename):
            with self.assertRaisesRegex(OSError, "interruption"):
                download.download(self.settings, self.url)
        job_path = self.root / "workspace/editing/archives/twitch_123456789/job.json"
        job = workflow.job_at(job_path)
        self.assertTrue((Path(job["source_dir"]) / "source.mp4").exists())
        with self.mocked_io(), patch.object(download.subprocess, "run", side_effect=AssertionError("Unexpected metadata request")), patch.object(download, "downloader_run", side_effect=AssertionError("Unexpected redownload")):
            result = download.download(self.settings, self.url)
        self.assertTrue(result["resumed_validation"])
        self.assertTrue((Path(job["source_dir"]) / "chat.json").exists())
        self.assertEqual(workflow.job_at(job_path)["state"], "source_verified")

    def test_partial_network_attempt_is_preserved_and_needs_explicit_retry(self):
        def fail(args, log):
            self.simulated_download(args, log)
            raise RuntimeError("synthetic interrupted network")

        with self.mocked_io(), patch.object(download, "downloader_run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "interrupted network"):
                download.download(self.settings, self.url)
        source_dir = self.root / "workspace/VOD/twitch_123456789"
        first = next(source_dir.glob("attempt-*/source.partial.mp4"))
        preserved = first.read_bytes()
        with self.assertRaisesRegex(ValueError, "--retry"):
            download.download(self.settings, self.url)
        with self.mocked_io():
            result = download.download(self.settings, self.url, retry=True)
        self.assertFalse(result["reused"])
        self.assertEqual(first.read_bytes(), preserved)
        self.assertEqual(len(list(source_dir.glob("attempt-*"))), 2)

    def test_review_cannot_reuse_source_outside_job(self):
        result = self.make_job()
        review = read_json(result["source_review"])
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"external fixture")
        review["video"]["path"] = str(outside)
        write_json(result["source_review"], review)
        with self.assertRaisesRegex(ValueError, "inside job"):
            download.download(self.settings, self.url)

    def test_nan_chat_end_is_rejected(self):
        self.chat["video"]["end"] = float("nan")
        with self.assertRaisesRegex(ValueError, "complete VOD"):
            download.validate_chat(self.chat, "123456789", "recipient", 60)

    def test_metadata_owner_mismatch_stops_before_media_download(self):
        self.metadata["data"]["video"]["owner"]["login"] = "someone_else"
        with self.mocked_io(), patch.object(download, "downloader_run", side_effect=AssertionError("Unexpected media download")):
            with self.assertRaisesRegex(ValueError, "VOD owner"):
                download.download(self.settings, self.url)

    def test_incomplete_model_can_be_repaired_and_asr_chunks_reused(self):
        result = self.make_job()
        model_dir = self.root / ".tools/models/faster-whisper-large-v3"
        model_dir.mkdir(parents=True)
        model_bytes = b"synthetic model weights"
        (model_dir / "model.bin").write_bytes(model_bytes)
        model = Mock()
        model.transcribe.return_value = ([{"start": 0.5, "end": 1.5, "text": "合成テスト", "words": []}], SimpleNamespace(language_probability=1.0))
        factory = Mock(return_value=model)

        def snapshot(**kwargs):
            for name in transcribe.MODEL_FILES:
                target = Path(kwargs["local_dir"]) / name
                if not target.exists():
                    target.write_text("{}", encoding="utf-8")

        def extract(args, log):
            Path(args[-1]).write_bytes(b"synthetic wave")
            Path(log).write_text("synthetic extract", encoding="utf-8")

        with patch.object(transcribe, "REPO", self.root), patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory), "huggingface_hub": SimpleNamespace(snapshot_download=snapshot)}), patch.object(transcribe, "MODEL_SHA256", hashlib.sha256(model_bytes).hexdigest()), patch.object(transcribe.package_metadata, "version", return_value="fixture"), patch.object(transcribe, "tool", return_value="ffmpeg"), patch.object(transcribe, "run_logged", side_effect=extract), patch.dict(transcribe.os.environ, {}, clear=False):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                transcribe.transcribe(self.settings, result["job"])
            first = transcribe.transcribe(self.settings, result["job"], download_model=True)
            second = transcribe.transcribe(self.settings, result["job"])
        self.assertEqual(first["segments"], 1)
        self.assertEqual(second["segments"], 1)
        factory.assert_called_once()
        self.assertFalse(any(Path(workflow.job_at(result["job"])["work"]).glob("asr/*.wav")))


if __name__ == "__main__":
    unittest.main()
