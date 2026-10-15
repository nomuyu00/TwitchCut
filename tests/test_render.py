"""Synthetic end-to-end regression tests; no real VOD, login or upload is used."""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render
from environment import render_environment


def fake_info():
    return {"format": {"duration": "6"}, "streams": [
        {"codec_type": "video", "width": 640, "height": 360, "sample_aspect_ratio": "1:1"},
        {"codec_type": "audio", "channels": 2}]}


def example(kind="topic"):
    return {"schema": 1, "id": "fixture_" + kind, "kind": kind, "title": "Synthetic sample",
            "source": "source.mkv", "source_date": "2026-01-01",
            "cuts": [{"start": .5, "end": 1.5}, {"start": 3, "end": 4}],
            "captions": [{"start": .7, "end": 1.3, "text": "最初の会話", "speaker": "streamer"},
                         {"start": 3.2, "end": 3.8, "text": "コメントです", "speaker": "comment"}],
            "blur_regions": [{"start": .6, "end": 1.4, "x": 280, "y": 40, "w": 60, "h": 60}]}


class ValidationTests(unittest.TestCase):
    def test_requires_intentional_short_crop(self):
        with self.assertRaisesRegex(ValueError, "explicitly reviewed crop"):
            render.validate_edl(example("short"), fake_info())

    def test_rejects_split_utterance_and_overlap(self):
        edl = example()
        edl["captions"][0]["end"] = 1.6
        with self.assertRaisesRegex(ValueError, "splits a caption"):
            render.validate_edl(edl, fake_info())
        edl = example()
        edl["captions"].append({"start": .8, "end": 1.2, "text": "Overlap", "speaker": "streamer"})
        with self.assertRaisesRegex(ValueError, "overlap"):
            render.validate_edl(edl, fake_info())

    def test_rejects_escape_and_invalid_geometry(self):
        with self.assertRaises(ValueError):
            render.contained(ROOT, "../outside.mp4")
        edl = example()
        edl["cuts"][0]["crop"] = {"x": 600, "y": 0, "w": 200, "h": 360}
        with self.assertRaisesRegex(ValueError, "beyond"):
            render.validate_edl(edl, fake_info())

    def test_hook_mapping_repeats_caption_in_cut_order(self):
        edl = example("digest")
        edl["cuts"] = [{"start": 3, "end": 4}, {"start": .5, "end": 1.5}, {"start": 3, "end": 4}]
        render.validate_edl(edl, fake_info())
        cues = render.mapped_captions(edl)
        self.assertEqual([c["speaker"] for c in cues], ["comment", "streamer", "comment"])
        for value, expected in zip([c["start"] for c in cues], [.2, 1.2, 2.2]):
            self.assertAlmostEqual(value, expected)

    def test_non_square_pixels_keep_display_aspect(self):
        info = fake_info()
        info["streams"][0]["sample_aspect_ratio"] = "3:4"
        with self.assertRaisesRegex(ValueError, "16:9 crop"):
            render.validate_edl(example(), info)
        edl = example()
        for cut in edl["cuts"]:
            cut["crop"] = {"x": 0, "y": 44, "w": 640, "h": 270}
        self.assertEqual(render.validate_edl(edl, info), (1920, 1080))


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe, font, family = render_environment()
        scratch = ROOT / ".test-work"
        scratch.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="render-", dir=scratch)
        cls.work = Path(cls.temp.name)
        cls.config = {"schema": 1, "_config_path": str(cls.work / "config.json"), "workspace": str(cls.work),
                      "tools": {"ffmpeg": cls.ffmpeg, "ffprobe": cls.ffprobe},
                      "render": {"encoder": "libx264", "font": str(font), "font_name": family}}
        # A changing image and steady sine make ordering, distortion and audio regressions detectable.
        subprocess.run([cls.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=30:duration=6", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=6", "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-c:a", "pcm_s24le",
                        str(cls.work / "source.mkv")], check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def perform_render(self, edl):
        path = self.work / f"{edl['id']}.json"
        path.write_text(json.dumps(edl, ensure_ascii=False), encoding="utf-8")
        return render.render(path, self.config), path

    def test_all_three_kinds_audio_subtitles_cache_and_no_overwrite(self):
        for kind in ("short", "topic", "digest"):
            with self.subTest(kind=kind):
                edl = example(kind)
                if kind == "short":
                    for cut in edl["cuts"]:
                        cut["crop"] = {"x": 140, "y": 0, "w": 360, "h": 360}
                if kind == "digest":
                    edl["cuts"].reverse()
                result, path = self.perform_render(edl)
                self.assertTrue(result["decode_verified"])
                self.assertEqual(result["human_qa"]["status"], "pending")
                self.assertAlmostEqual(result["duration"], 2, delta=.08)
                self.assertEqual((result["width"], result["height"]), (1080, 1920) if kind == "short" else (1920, 1080))
                audio = result["audio_qa"]
                self.assertLess(abs(audio["gain_db"]), .2)
                self.assertGreater(audio["correlation"], .99)
                self.assertLess(audio["residual_relative_rms"], .06)
                output = Path(result["file"])
                ass = output.with_suffix(".ass").read_text(encoding="utf-8-sig")
                srt = output.with_suffix(".srt").read_text(encoding="utf-8-sig")
                self.assertIn("&H00F86AF7", ass)
                self.assertIn("&H00FF7000", ass)
                self.assertIn("コメントです", srt)
                if kind == "short":
                    # Pixel comparison catches both stretched artwork and fake padding, not only SAR metadata.
                    from PIL import Image, ImageChops, ImageStat
                    def frame(file, when):
                        data = subprocess.check_output([self.ffmpeg, "-v", "error", "-ss", str(when),
                                                       "-i", str(file), "-frames:v", "1", "-f", "image2pipe",
                                                       "-c:v", "png", "pipe:1"])
                        return Image.open(BytesIO(data)).convert("RGB")
                    original = frame(self.work / "source.mkv", .5).crop((140, 0, 500, 360))
                    expected = original.resize((1920, 1920), Image.Resampling.BICUBIC).crop((420, 0, 1500, 1920))
                    actual = frame(output, 0)
                    error = ImageStat.Stat(ImageChops.difference(expected, actual)).mean
                    self.assertLess(sum(error) / 3, 5, error)
                self.assertTrue(render.render(path, self.config)["reused"])
                unchanged = output.stat().st_mtime_ns
                edl["title"] += " revised"
                path.write_text(json.dumps(edl), encoding="utf-8")
                with self.assertRaises(FileExistsError):
                    render.render(path, self.config)
                self.assertEqual(output.stat().st_mtime_ns, unchanged)
                if kind == "topic":
                    replaced = render.render(path, self.config, force=True)
                    self.assertFalse(replaced["reused"])
                    self.assertEqual(len(list((output.parent / "revisions" / edl["id"]).rglob("*.mp4"))), 1)

    def test_fractional_cuts_have_no_accumulated_audio_gaps(self):
        edl = example()
        edl.update(id="fractional", captions=[], blur_regions=[])
        edl["cuts"] = [{"start": index * .2, "end": index * .2 + .107} for index in range(8)]
        result, _ = self.perform_render(edl)
        pcm = render.probe(self.ffprobe, Path(result["audio_qa"]["reference"]))
        self.assertAlmostEqual(float(pcm["format"]["duration"]), .856, delta=1 / 48000)
        self.assertAlmostEqual(result["duration"], .856, delta=.04)

    def test_optional_outro_is_local_and_keeps_source_audio(self):
        from PIL import Image
        image = self.work / "recipient-outro.png"
        Image.new("RGB", (320, 180), (20, 80, 120)).save(image)
        edl = example()
        edl.update(id="with_outro", outro={"image": image.name, "duration": .2})
        result, _ = self.perform_render(edl)
        self.assertAlmostEqual(result["duration"], 2.2, delta=.08)
        self.assertGreater(result["audio_qa"]["correlation"], .99)

    def test_introduced_silence_fails_audio_verification(self):
        silent = self.work / "silent.wav"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anullsrc=r=48000:cl=mono", "-t", "2", "-c:a", "pcm_s24le", str(silent)], check=True)
        with self.assertRaisesRegex(RuntimeError, "silence|silent"):
            render.audio_comparison(self.ffmpeg, self.work / "source.mkv", silent, self.work, 2)


if __name__ == "__main__":
    unittest.main()
