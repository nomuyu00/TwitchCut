"""Shared local settings and process helpers; no credentials or network services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

REPO = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_settings(path=None):
    path = Path(path or REPO / "twitchcut.local.json").resolve()
    if not path.is_file():
        raise ValueError("初期設定がありません。setup.cmd または scripts/setup_env.py init を実行してください。")
    data = read_json(path)
    if data.get("schema") != 1:
        raise ValueError("Unsupported settings schema")
    data["_config_path"] = str(path)
    return data


def config_path(settings, value):
    path = Path(value).expanduser()
    return (Path(settings["_config_path"]).parent / path).resolve() if not path.is_absolute() else path.resolve()


def workspace(settings):
    path = config_path(settings, settings.get("workspace", "./workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def tool(settings, name):
    value = settings.get("tools", {}).get(name)
    if value:
        explicit = config_path(settings, value)
        if explicit.is_file():
            return str(explicit)
        resolved = shutil.which(value)
        if resolved:
            return resolved
        raise ValueError(f"Configured {name} was not found: {explicit}. Run doctor.")
    names = ("TwitchDownloaderCLI",) if name == "twitchdownloader" else (name,)
    for candidate in names:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    if name == "twitchdownloader":
        candidate = REPO / ".tools/TwitchDownloaderCLI/TwitchDownloaderCLI.exe"
        if candidate.is_file():
            return str(candidate)
    elif name in ("ffmpeg", "ffprobe"):
        candidate = REPO / ".tools/ffmpeg/bin" / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    raise ValueError(f"{name} がありません。setup.cmd を実行し、doctor の案内を確認してください。")


def run_logged(args, log, cwd=None):
    """Stream output to disk; process success is never a human QA assertion."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run([str(x) for x in args], cwd=cwd, stdout=stream,
                                 stderr=subprocess.STDOUT, shell=False)
    if process.returncode:
        raise RuntimeError(f"Process exited {process.returncode}; inspect {log}")


def recipient_profile(settings, require_youtube=False):
    p = dict(settings.get("profile", {}))
    login = p.get("twitch_login", "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
        raise ValueError("対象Twitchを登録してください: python twitchcut.py configure --twitch-login LOGIN")
    channel = p.get("youtube_channel_id", "").strip()
    if channel and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel):
        raise ValueError("YouTubeの恒久チャンネルID（UCから始まる24文字）を指定してください。")
    if require_youtube and not channel:
        raise ValueError("投稿先未設定: configure --youtube-channel-id UC... を実行してください。")
    p.update(twitch_login=login, twitch_url=f"https://www.twitch.tv/{login}",
             youtube_channel_id=channel,
             youtube_channel_url=f"https://www.youtube.com/channel/{channel}" if channel else "",
             privacy_status="private", verified_related_links=p.get("verified_related_links", []))
    return p
