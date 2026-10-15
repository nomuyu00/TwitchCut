"""Local, resumable, source-aligned Japanese ASR. Never sends media to an API."""
from __future__ import annotations

import dataclasses
import copy
import hashlib
from importlib import metadata as package_metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from common import REPO, workspace, tool, read_json, write_json, run_logged, recipient_profile
from media_hash_cache import MediaHashCache
import workflow

MODEL_REPO = "Systran/faster-whisper-large-v3"
MODEL_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
MODEL_SHA256 = "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.json", "preprocessor_config.json")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def owned_segments(segments, offset, start, end):
    """Overlap avoids edge truncation; midpoint assigns each segment to one chunk."""
    result = []
    for segment in segments:
        item = dataclasses.asdict(segment) if dataclasses.is_dataclass(segment) else copy.deepcopy(dict(segment))
        a, b = offset + item["start"], offset + item["end"]
        if start <= (a + b) / 2 < end:
            item.update(source_start=a, source_end=b)
            for word in item.get("words") or []:
                word["source_start"] = offset + word["start"]
                word["source_end"] = offset + word["end"]
            result.append(item)
    return result


def transcribe(settings, job_path, download_model=False, force=False):
    j = workflow.job_at(job_path)
    if Path(j["root"]).resolve() != workspace(settings):
        raise ValueError("Job is outside the configured workspace")
    if j["profile"]["twitch_login"] != recipient_profile(settings)["twitch_login"]:
        raise ValueError("Job belongs to another configured Twitch account")
    review_path = Path(j["work"]) / "source-review.json"
    review = read_json(review_path)
    checks = ("duration_and_streams", "chat_identity_and_range", "decode")
    mode = review.get("acquisition_mode", "fresh_download")
    if mode == "legacy_revalidated" and j.get("adopted_config"):
        checks += ("provenance", "timeline_integrity")
    elif mode == "fresh_download":
        checks += ("download_logs",)
    else:
        raise ValueError("Unknown or unattached source acquisition mode")
    if review.get("vod_id") != j["vod_id"] or not all(review.get("checks", {}).get(name, {}).get("passed") is True for name in checks):
        raise ValueError("Source review is incomplete or belongs to another VOD")
    source = (Path(j["root"]) / "VOD" / review["video"]["path"]).resolve()
    if not source.is_relative_to(Path(j["source_dir"]).resolve()):
        raise ValueError("Unexpected source path")
    cache = MediaHashCache(j["root"])
    source_sha = cache.hash(source)
    if source_sha != review["video"]["sha256"]:
        raise ValueError("Source changed since verification")
    config = settings.get("asr", {})
    repo = config.get("model", MODEL_REPO)
    revision = config.get("revision", MODEL_REVISION)
    device = config.get("device", "cpu")
    compute = config.get("compute_type", "int8" if device == "cpu" else "float16")
    if device not in ("cpu", "cuda"):
        raise ValueError("ASR device must be cpu or cuda")
    if repo != MODEL_REPO or revision != MODEL_REVISION:
        raise ValueError("This release verifies the pinned large-v3 model. Change the pin with its verification data, not just settings.")
    model_dir = REPO / ".tools/models/faster-whisper-large-v3"
    os.environ.update(HF_HOME=str(REPO / ".tools/huggingface"), HF_HUB_DISABLE_TELEMETRY="1",
                      HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ValueError("ASR dependencies missing. Run setup.ps1 -WithAsr, or install requirements-asr.txt into .venv.") from exc
    missing_files = [name for name in MODEL_FILES if not (model_dir / name).is_file()]
    if missing_files:
        if not download_model:
            raise ValueError("ASR model is missing or incomplete (" + ", ".join(missing_files) + "). Retry transcribe with --download-model (one-time large download; media stays local).")
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo, revision=revision, local_dir=model_dir,
                          allow_patterns=list(MODEL_FILES))
        if any(not (model_dir / name).is_file() for name in MODEL_FILES):
            raise ValueError("ASR model download is incomplete. Retry with --download-model; completed files are reused.")
    model_sha = MediaHashCache(REPO / ".tools").hash(model_dir / "model.bin")
    if model_sha != MODEL_SHA256:
        raise ValueError("ASR model hash mismatch; preserving files for inspection")
    os.environ["HF_HUB_OFFLINE"] = "1"
    duration = float(review["duration_seconds"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid source duration")
    out = Path(j["work"]) / "asr"
    out.mkdir(exist_ok=True)
    manifest = {"source_sha256": source_sha, "model": repo, "revision": revision, "model_sha256": model_sha,
                "runtime_versions": {name: package_metadata.version(name) for name in ("faster-whisper", "ctranslate2")},
                "device": device, "compute_type": compute, "chunk_seconds": 600, "overlap_seconds": 1,
                "language": "ja", "beam_size": 5, "word_timestamps": True, "vad_filter": True,
                "vad_parameters": {"min_silence_duration_ms": 700, "speech_pad_ms": 250}, "schema": 1}
    signature = fingerprint(manifest)
    model = None
    all_segments = []
    chunk_summary = []
    for i, start in enumerate(range(0, math.ceil(duration), 600)):
        end = min(duration, start + 600)
        offset, limit = max(0, start - 1), min(duration, end + 1)
        chunk_file = out / f"chunk-{i:04d}.json"
        if chunk_file.is_file() and not force:
            stored = read_json(chunk_file)
            if stored.get("signature") != signature:
                raise ValueError("ASR settings/source changed; inspect existing output and use --force to recompute")
            if stored.get("source_start") != start or stored.get("source_end") != end:
                raise ValueError("Cached ASR chunk belongs to a different source interval; inspect before using --force")
        else:
            audio = out / f"chunk-{i:04d}.wav"
            run_logged([tool(settings, "ffmpeg"), "-hide_banner", "-y", "-ss", str(offset), "-i", str(source),
                        "-t", str(limit - offset), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio)],
                       out / f"chunk-{i:04d}.log")
            if model is None:
                model = WhisperModel(str(model_dir), device=device, compute_type=compute,
                                     cpu_threads=max(1, min(8, os.cpu_count() or 4)), local_files_only=True)
            print(f"ASR {i + 1}/{math.ceil(duration / 600)} ({start:.0f}–{end:.0f}s), {device}", flush=True)
            segments, info = model.transcribe(str(audio), language="ja", beam_size=5, word_timestamps=True,
                                             vad_filter=True, vad_parameters=manifest["vad_parameters"],
                                             condition_on_previous_text=True)
            stored = {"signature": signature, "source_start": start, "source_end": end,
                      "language_probability": info.language_probability,
                      "segments": owned_segments(segments, offset, start, end)}
            write_json(chunk_file, stored)
            audio.unlink()  # Reproducible 10-minute scratch audio only; original VOD is retained.
        all_segments.extend(stored["segments"])
        chunk_summary.append({"file": str(chunk_file), "start": start, "end": end, "segments": len(stored["segments"])})
    write_json(out / "transcript.json", {**manifest, "signature": signature, "segments": all_segments,
                                         "human_corrected": False})
    with (out / "transcript.txt").open("w", encoding="utf-8") as text:
        for s in all_segments:
            text.write(f"[{s['source_start']:.3f} --> {s['source_end']:.3f}] {s['text'].strip()}\n")
    write_json(out / "index.json", {"signature": signature, "duration_seconds": duration, "chunks": chunk_summary,
                                    "human_corrected": False})
    return {"transcript": str(out / "transcript.json"), "index": str(out / "index.json"),
            "segments": len(all_segments), "human_corrected": False}
