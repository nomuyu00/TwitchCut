"""Download a complete authorized Twitch VOD and chat using TwitchDownloader."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import uuid

from common import REPO, read_json, write_json, workspace, tool, recipient_profile, run_logged
from media_hash_cache import MediaHashCache
import workflow

BAD_LOG = ("using stubs instead", "will be missing from the finalized video",
           "failed to emit part stubs", "contains malformed packets")


def probe_media(path, ffprobe):
    return json.loads(subprocess.check_output(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        encoding="utf-8", timeout=60))


def validate_chat(chat, ident, owner, seconds):
    if str(chat.get("video", {}).get("id")) != ident:
        raise ValueError("Chat VOD ID mismatch")
    if chat.get("streamer", {}).get("login", "").lower() != owner.lower():
        raise ValueError("Chat streamer mismatch")
    comments = chat.get("comments")
    if not isinstance(comments, list):
        raise ValueError("Chat comments must be an array")
    video = chat["video"]
    start, end = float(video.get("start", -1)), float(video.get("end", 0))
    if not math.isfinite(start) or not math.isfinite(end) or start != 0 or end < seconds - 2:
        raise ValueError("Chat export does not cover the complete VOD")
    for c in comments:
        t = float(c["content_offset_seconds"])
        if not math.isfinite(t) or not 0 <= t <= seconds + 2:
            raise ValueError("Chat timestamp outside the source")
        if c.get("content_id") is not None and str(c["content_id"]) != ident:
            raise ValueError("Comment belongs to another VOD")
    return len(comments)


def downloader_run(args, log):
    """Do not persist signed query strings if an upstream failure prints a URL."""
    with Path(log).open("w", encoding="utf-8") as output:
        p = subprocess.Popen([str(x) for x in args], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=downloader_env())
        for line in p.stdout:
            line = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[query-redacted]', line)
            output.write(line)
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"TwitchDownloader failed ({p.returncode}); see {log}. Partial files were preserved.")
    with Path(log).open(encoding="utf-8") as stream:
        for line in stream:
            if any(marker in line.lower() for marker in BAD_LOG):
                raise ValueError(f"Possible missing/corrupt VOD segments; inspect {log}")


def downloader_env():
    env = os.environ.copy()
    env.setdefault("DOTNET_BUNDLE_EXTRACT_BASE_DIR", str(REPO / ".setup/dotnet"))
    return env


def download(settings, url, retry=False):
    root = workspace(settings)
    profile = recipient_profile(settings)
    created = workflow.init(SimpleNamespace(url=url, root=root, profile=profile))
    job_path = Path(created["job"])
    j = workflow.job_at(job_path)
    with workflow.locked(Path(j["work"]) / "acquisition.lock"):
        return _download_job(settings, job_path, profile, retry)


def _download_job(settings, job_path, profile, retry):
    j = workflow.job_at(job_path)
    root = Path(j["root"])
    work, source_dir = Path(j["work"]), Path(j["source_dir"])
    review_path = work / "source-review.json"
    cache = MediaHashCache(root)
    if review_path.exists():
        review = read_json(review_path)
        if review.get('vod_id') != j['vod_id'] or not all(review.get('checks', {}).get(k, {}).get('passed') is True
                for k in ('duration_and_streams', 'chat_identity_and_range', 'decode', 'download_logs')):
            raise ValueError('Existing source review is incomplete or belongs to another VOD')
        paths = {k: workflow.inside(root / "VOD", review[k]["path"]) for k in ("video", "chat")}
        if not all(path.is_relative_to(source_dir.resolve()) for path in paths.values()):
            raise ValueError("Source review points outside this VOD's source directory")
        if all(cache.hash(paths[k]) == review[k]["sha256"] for k in ("video", "chat")):
            if j["state"] in ("initialized", "metadata_saved"):
                j["state"] = "source_verified"
                j["metadata"]["duration_seconds"] = float(review["duration_seconds"])
                workflow.write(job_path, j)
            return {"job": str(job_path), "source_review": str(review_path), "reused": True}
        raise ValueError("Previously verified source changed; inspect the existing job before downloading again.")
    acquisition_path = work / "acquisition.json"
    if acquisition_path.exists():
        acquisition = read_json(acquisition_path)
        if acquisition.get("state") in ("downloaded", "validated_before_finalize"):
            return _finish_acquisition(settings, job_path, acquisition, resumed=True)
    if (source_dir / "source.mp4").exists() or (source_dir / "chat.json").exists():
        raise ValueError("Existing unverified final source files were preserved; inspect acquisition.json and revalidate before retry.")
    if list(source_dir.glob("attempt-*")) and not retry:
        raise ValueError("An earlier attempt exists. Inspect its logs, then use --retry for a new attempt (no automatic partial resume).")
    cli, ffmpeg, ffprobe = tool(settings, "twitchdownloader"), tool(settings, "ffmpeg"), tool(settings, "ffprobe")
    attempt = source_dir / ("attempt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6])
    attempt.mkdir()
    logs = work / "logs" / attempt.name
    logs.mkdir(parents=True)
    raw = subprocess.run([cli, "info", "--id", j["vod_id"], "--format", "Raw", "--use-utf8=true", "--log-level", "None"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=downloader_env())
    if raw.returncode or "#EXTM3U" not in raw.stdout:
        raise ValueError("VOD metadata/playback unavailable. Check whether the VOD exists and is accessible.")
    lines = [line for line in raw.stdout.splitlines() if line.strip()]
    data = json.loads(lines[0])
    video = (data.get("data") or {}).get("video")
    if not video or float(video.get("lengthSeconds", 0)) <= 0:
        raise ValueError("VOD metadata is empty or duration is not final")
    if (video.get("owner") or {}).get("login", "").lower() != profile["twitch_login"]:
        raise ValueError("The VOD owner differs from the configured Twitch account. Confirm the intended source before proceeding.")
    # Only metadata is persisted: Raw also contains HLS playback credentials.
    metadata_path = logs / "metadata.json"
    write_json(metadata_path, data)
    # workflow.metadata consumes the first JSON line.
    metadata_path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    workflow.metadata(SimpleNamespace(job=job_path, raw=metadata_path, requested_id=j["vod_id"]))
    source, chat_path = attempt / "source.partial.mp4", attempt / "chat.partial.json"
    downloader_run([cli, "videodownload", "--id", j["vod_id"], "--output", source,
                    "--threads", "4", "--ffmpeg-path", ffmpeg, "--temp-path", attempt / "cache",
                    "--collision", "Exit", "--log-level", "Info,Warning,Error,Ffmpeg"], logs / "video.log")
    downloader_run([cli, "chatdownload", "--id", j["vod_id"], "--output", chat_path,
                    "--threads", "4", "--compression", "None", "--temp-path", attempt / "cache",
                    "--collision", "Exit", "--log-level", "Info,Warning,Error"], logs / "chat.log")
    write_json(logs / "download-result.json", {"exit_code": 0, "missing_segment_markers": [], "attempt": str(attempt)})
    acquisition = {"attempt": str(attempt), "logs": str(logs), "state": "downloaded"}
    write_json(work / "acquisition.json", acquisition)
    return _finish_acquisition(settings, job_path, acquisition, resumed=False)


def _finish_acquisition(settings, job_path, acquisition, resumed=False):
    """Recover an interrupted validation/final rename without redownloading.

    Incomplete network attempts still need explicit --retry. Only an attempt with
    recorded successful video AND chat downloads reaches this recovery path.
    Validation is repeated if interrupted; source files are never overwritten.
    """
    j = workflow.job_at(job_path)
    root, work, source_dir = Path(j["root"]), Path(j["work"]), Path(j["source_dir"])
    attempt = workflow.inside(source_dir, acquisition["attempt"], exists=False)
    logs = workflow.inside(work / "logs", acquisition["logs"], exists=False)
    if not attempt.is_dir() or not logs.is_dir() or not re.fullmatch(r"attempt-[0-9TZ]+-[0-9a-f]{6}", attempt.name):
        raise ValueError("Invalid saved acquisition attempt")
    result = read_json(logs / "download-result.json")
    if result.get("exit_code") != 0 or result.get("missing_segment_markers") != [] or Path(result.get("attempt", "")).resolve() != attempt:
        raise ValueError("Download completion evidence is missing or belongs to another attempt")
    for log in (logs / "video.log", logs / "chat.log"):
        with log.open(encoding="utf-8") as stream:
            if any(any(marker in line.lower() for marker in BAD_LOG) for line in stream):
                raise ValueError(f"Possible missing/corrupt VOD segments; inspect {log}")
    final_source, final_chat = source_dir / "source.mp4", source_dir / "chat.json"
    source = final_source if final_source.is_file() else attempt / "source.partial.mp4"
    chat_path = final_chat if final_chat.is_file() else attempt / "chat.partial.json"
    if (final_source.exists() and (attempt / "source.partial.mp4").exists()) or (final_chat.exists() and (attempt / "chat.partial.json").exists()):
        raise ValueError("Both final and partial source files exist; inspect them before resuming")
    ffmpeg, ffprobe = tool(settings, "ffmpeg"), tool(settings, "ffprobe")
    info = probe_media(source, ffprobe)
    seconds = float(info["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0 or abs(seconds - float(j["metadata"]["duration_seconds"])) > 2:
        raise ValueError("Source duration differs from metadata; inspect for partial or still-recording VOD")
    if not {"video", "audio"}.issubset({s["codec_type"] for s in info["streams"]}):
        raise ValueError("Source video/audio stream missing")
    write_json(logs / "probe.json", info)
    count = validate_chat(read_json(chat_path), j["vod_id"], j["profile"]["twitch_login"], seconds)
    write_json(logs / "chat-validation.json", {"vod_id": j["vod_id"], "comments": count, "duration_seconds": seconds})
    decode = logs / "decode.log"
    run_logged([ffmpeg, "-v", "error", "-xerror", "-i", source, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], decode)
    # An empty ffmpeg error log is normal; keep explicit process-success evidence.
    write_json(logs / "decode-result.json", {"exit_code": 0, "completed_at": workflow.now(), "log": str(decode)})
    write_json(work / "acquisition.json", {"attempt": str(attempt), "logs": str(logs), "state": "validated_before_finalize"})
    if source != final_source:
        if final_source.exists():
            raise ValueError("Destination appeared during download; preserving attempt")
        source.rename(final_source)
    if chat_path != final_chat:
        if final_chat.exists():
            raise ValueError("Destination appeared during download; preserving attempt")
        chat_path.rename(final_chat)
    cache = MediaHashCache(root)
    evidence = {"duration_and_streams": "probe.json", "chat_identity_and_range": "chat-validation.json",
                "decode": "decode-result.json", "download_logs": "download-result.json"}
    review = {"vod_id": j["vod_id"], "duration_seconds": seconds, "acquisition_mode": "fresh_download",
              "video": {"path": str(final_source.relative_to(root / "VOD")), "sha256": cache.hash(final_source)},
              "chat": {"path": str(final_chat.relative_to(root / "VOD")), "sha256": cache.hash(final_chat)},
              "checks": {k: {"passed": True, "evidence": str((logs / v).relative_to(work))} for k, v in evidence.items()},
              "human_timeline_reviewed": False}
    review_path = work / "source-review.json"
    write_json(review_path, review)
    write_json(work / "acquisition.json", {"attempt": str(attempt), "logs": str(logs), "state": "verified",
                                          "source_sha256": review['video']['sha256'], "chat_sha256": review['chat']['sha256']})
    j = workflow.job_at(job_path)
    j["state"] = "source_verified"
    j["metadata"]["duration_seconds"] = seconds
    workflow.write(job_path, j)
    return {"job": str(job_path), "source_review": str(review_path), "reused": False, "duration_seconds": seconds,
            "comments": count, "human_timeline_reviewed": False, "resumed_validation": resumed}
