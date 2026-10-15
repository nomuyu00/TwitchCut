#!/usr/bin/env python3
"""TwitchCut: recipient setup and explicit production stages driven by Codex."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8')
sys.path.insert(0, str(REPO / "scripts"))
from common import load_settings, workspace, recipient_profile, write_json, read_json, tool
import workflow


def configure(settings, args):
    p = settings.setdefault("profile", {})
    if args.twitch_login is not None:
        p["twitch_login"] = args.twitch_login.strip().lower()
    if args.youtube_channel_id is not None:
        p["youtube_channel_id"] = args.youtube_channel_id.strip()
    if args.browser_profile is not None:
        p["browser_profile"] = args.browser_profile
    profile = recipient_profile(settings)
    settings["profile"] = profile
    config = settings.pop("_config_path")
    write_json(config, settings)
    return {"config": config, "profile": profile, "authenticated": False,
            "next": "ブラウザのYouTube Studioで恒久チャンネルIDと投稿権限を実確認してください。"}


def sync_empty_profile(settings, job_path):
    j = workflow.job_at(job_path)
    current = recipient_profile(settings, require_youtube=True)
    for name in ("twitch_login", "youtube_channel_id"):
        if j["profile"].get(name) and j["profile"][name] != current[name]:
            raise ValueError("Job identity differs from current settings; do not switch source/channel within an existing job")
    if not j["profile"].get("youtube_channel_id"):
        if (Path(j["work"]) / "publication.json").exists():
            raise ValueError("Cannot change a prepared publication profile")
        j["profile"] = current
        workflow.write(job_path, j)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Recipient local configuration JSON (default: twitchcut.local.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Check this PC; does not log in or upload")
    doctor.add_argument("--json", action="store_true")
    cfg = sub.add_parser("configure", help="Store recipient account identifiers, never credentials")
    cfg.add_argument("--twitch-login")
    cfg.add_argument("--youtube-channel-id")
    cfg.add_argument("--browser-profile")
    dl = sub.add_parser("download", help="Download and verify full VOD + chat")
    dl.add_argument("url"); dl.add_argument("--retry", action="store_true")
    trans = sub.add_parser("transcribe", help="Local full-VOD ASR")
    trans.add_argument("--job", required=True)
    trans.add_argument("--download-model", action="store_true")
    trans.add_argument("--force", action="store_true")
    render = sub.add_parser("render", help="Render an authored short/topic/digest EDL")
    render.add_argument("--edl", required=True); render.add_argument("--output")
    render.add_argument("--force", action="store_true")
    prepare = sub.add_parser("prepare", help="Validate QA and prepare publication materials (no upload)")
    prepare.add_argument("--job", required=True); prepare.add_argument("--plan", required=True)
    begin = sub.add_parser("begin", help="Record upload intent after real channel verification (no upload)")
    begin.add_argument("--job", required=True); begin.add_argument("--asset", required=True)
    begin.add_argument("--channel", required=True); begin.add_argument("--evidence", required=True)
    record = sub.add_parser("record", help="Record actual Studio readback and video ID")
    record.add_argument("--job", required=True); record.add_argument("--asset", required=True)
    record.add_argument("--result", "--receipt", dest="receipt", required=True)
    status = sub.add_parser("status"); status.add_argument("--job", required=True)
    inspect = sub.add_parser("inspect", help="Read only a selected transcript interval")
    inspect.add_argument("--job", required=True); inspect.add_argument("--start", type=float, required=True)
    inspect.add_argument("--end", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            command = [sys.executable, "-X", "utf8", str(REPO / "scripts/setup_env.py"), "doctor"]
            if args.config: command += ["--config", args.config]
            if args.json: command += ["--json"]
            return subprocess.call(command)
        settings = load_settings(args.config)
        if hasattr(args, "job"):
            j = workflow.job_at(args.job)
            if Path(j["root"]).resolve() != workspace(settings):
                raise ValueError("Job belongs to a different configured workspace")
        if args.command == "configure":
            result = configure(settings, args)
        elif args.command == "download":
            from download import download
            result = download(settings, args.url, args.retry)
        elif args.command == "transcribe":
            from transcribe import transcribe
            result = transcribe(settings, args.job, args.download_model, args.force)
        elif args.command == "render":
            command = [sys.executable, "-X", "utf8", str(REPO / "scripts/render.py"), "--edl", args.edl,
                       "--config", settings["_config_path"]]
            if args.output: command += ["--output", args.output]
            if args.force: command += ["--force"]
            return subprocess.call(command)
        elif args.command == "inspect":
            if not 0 <= args.start < args.end:
                raise ValueError("Specify a positive start/end interval")
            transcript = read_json(Path(j["work"]) / "asr/transcript.json")
            result = {"segments": [s for s in transcript["segments"] if s["source_end"] > args.start and s["source_start"] < args.end]}
        else:
            if args.command == "prepare":
                sync_empty_profile(settings, args.job)
                args.ffprobe = tool(settings, "ffprobe")
            result = getattr(workflow, args.command)(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
