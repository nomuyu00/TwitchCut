"""Offline regressions for identity, source integrity, resume and publication gates."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workflow
from common import recipient_profile, write_json, load_settings, workspace
from download import validate_chat
from transcribe import owned_segments
from media_hash_cache import MediaHashCache


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="twitchcut-pipeline-")
        self.root = Path(self.temp.name)
        self.profile = {"twitch_login": "sample_streamer", "twitch_url": "https://www.twitch.tv/sample_streamer",
                        "youtube_channel_id": "UC" + "a" * 22, "youtube_channel_url": "https://www.youtube.com/channel/UC" + "a" * 22,
                        "verified_related_links": []}

    def tearDown(self):
        self.temp.cleanup()

    def init(self, profile=None):
        return workflow.init(SimpleNamespace(url="https://www.twitch.tv/videos/123456789", root=self.root,
                                             profile=profile or self.profile))

    def test_new_recipient_and_idempotent_job(self):
        result = self.init()
        job = workflow.job_at(result["job"])
        self.assertEqual(job["expected_kinds"], ["short", "topic", "digest"])
        self.assertEqual(job["profile"]["youtube_channel_id"], self.profile["youtube_channel_id"])
        self.assertTrue(self.init()["reused"])
        with self.assertRaisesRegex(ValueError, "another configured Twitch"):
            self.init({**self.profile, "twitch_login": "different_streamer"})

    def test_reject_non_vod_or_spoofed_url(self):
        for url in ("https://www.twitch.tv/someone", "https://twitch.tv.evil.test/videos/123",
                    "https://user:pass@twitch.tv/videos/123", "https://www.twitch.tv/videos/0", "file:///videos/123"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                workflow.vod_id(url)

    def test_no_provider_account_defaults_and_upload_requires_recipient(self):
        with self.assertRaises(ValueError):
            recipient_profile({"profile": {}})
        p = recipient_profile({"profile": {"twitch_login": "recipient"}})
        self.assertEqual(p["youtube_channel_id"], "")
        with self.assertRaises(ValueError):
            recipient_profile({"profile": p}, require_youtube=True)

    def test_config_paths_follow_config_location_not_cwd(self):
        path = self.root / "config" / "settings.json"
        write_json(path, {"schema": 1, "workspace": "../media"})
        self.assertEqual(workspace(load_settings(path)), self.root / "media")

    def test_chat_identity_range_and_empty_legitimate_chat(self):
        chat = {"video": {"id": "123", "start": 0, "end": 60}, "streamer": {"login": "recipient"}, "comments": []}
        self.assertEqual(validate_chat(chat, "123", "recipient", 60), 0)
        chat["comments"] = [{"content_offset_seconds": 70}]
        with self.assertRaises(ValueError):
            validate_chat(chat, "123", "recipient", 60)
        chat["comments"] = []
        with self.assertRaises(ValueError):
            validate_chat(chat, "456", "recipient", 60)

    def test_full_archive_not_new_delivery(self):
        job = workflow.job_at(self.init()["job"])
        with self.assertRaises(ValueError):
            workflow.delivery_scope(job, ["archive"])

    def test_prepare_cannot_use_blank_channel(self):
        result = self.init({**self.profile, "youtube_channel_id": ""})
        job = workflow.job_at(result["job"])
        job["metadata"] = {"owner_login": "sample_streamer"}
        workflow.write(result["job"], job)
        with self.assertRaisesRegex(ValueError, "recipient YouTube"):
            workflow.prepare(SimpleNamespace(job=result["job"], plan="does-not-exist.json"))

    def test_begin_duplicate_and_wrong_channel_are_blocked(self):
        result = self.init()
        job = workflow.job_at(result["job"])
        evidence = Path(job["work"]) / "channel.txt"
        evidence.write_text("Synthetic test evidence only", encoding="utf-8")
        asset = {"id": "clip", "kind": "short", "sha256": "a" * 64, "file": "test.mp4", "title": "test",
                 "description": "test", "thumbnail_sha256": None, "privacy_status": "private", "publish_at": None,
                 "expected_channel_id": self.profile["youtube_channel_id"]}
        args = SimpleNamespace(job=result["job"], asset="clip", channel=self.profile["youtube_channel_id"], evidence=evidence)
        with patch.object(workflow, "asset_at", return_value=asset):
            wrong = SimpleNamespace(**{**vars(args), "channel": "UC" + "b" * 22})
            with self.assertRaisesRegex(ValueError, "channel mismatch"):
                workflow.begin(wrong)
            self.assertFalse(workflow.begin(args)["uploaded"])
            with self.assertRaisesRegex(ValueError, "Existing upload"):
                workflow.begin(args)

    def test_hash_changes_when_content_changes(self):
        media = self.root / "file.bin"
        media.write_bytes(b"first")
        cache = MediaHashCache(self.root)
        before = cache.hash(media)
        self.assertEqual(cache.hash(media), before)
        media.write_bytes(b"other")
        self.assertNotEqual(cache.hash(media), before)

    def test_asr_overlap_has_single_chunk_owner_and_source_words(self):
        segment = {"start": 0.5, "end": 1.5, "text": "境界", "words": [{"start": 0.5, "end": 1.5, "word": "境界"}]}
        self.assertEqual(owned_segments([segment], 599, 0, 600), [])
        result = owned_segments([segment], 599, 600, 1200)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["words"][0]["source_start"], 599.5)


if __name__ == "__main__":
    unittest.main()
