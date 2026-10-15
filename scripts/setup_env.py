"""Recipient configuration and read-only environment checks (standard library).

`init` never overwrites a file. `doctor` returns 0 for a ready base environment,
1 for environment/configuration errors; missing recipient profile or an unchecked
browser is reported separately and never represented as production readiness.
`--require-asr` makes a missing ASR runtime an environment error.

FFmpeg's official download page links gyan.dev as a Windows binary provider:
https://ffmpeg.org/download.html
Pinned archive/hash verified on 2026-10-16 from:
https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_VERSION = "9.0.2"
FFMPEG_NAME = f"ffmpeg-{FFMPEG_VERSION}-essentials_build"
FFMPEG_URL = f"https://www.gyan.dev/ffmpeg/builds/packages/{FFMPEG_NAME}.zip"
FFMPEG_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
MODEL = "Systran/faster-whisper-large-v3"
MODEL_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"


def local_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def path_value(path: Path, base: Path) -> str:
    try:
        return "./" + path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def find_tool(name: str, configured: str = "", base: Path = ROOT) -> str:
    executable = "TwitchDownloaderCLI" if name == "twitchdownloader" else name
    if configured:
        if not any(char in configured for char in ("/", "\\", ":")):
            return shutil.which(configured) or str(local_path(configured, base))
        return str(local_path(configured, base))
    suffix = ".exe" if os.name == "nt" else ""
    portable = ROOT / ".tools" / ("TwitchDownloaderCLI" if name == "twitchdownloader" else "ffmpeg")
    portable = portable / (executable + suffix) if name == "twitchdownloader" else portable / "bin" / (executable + suffix)
    if portable.is_file():
        return str(portable)
    return shutil.which(executable) or ""


def find_japanese_font() -> tuple[str, str]:
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    # Existing system fonts are used in place; no licensed font is redistributed.
    for filename, family in (("meiryo.ttc", "Meiryo"), ("YuGothM.ttc", "Yu Gothic"), ("msgothic.ttc", "MS Gothic")):
        if (fonts / filename).is_file():
            return str(fonts / filename), family
    return "", ""


def initial_config(base: Path) -> dict:
    font, family = find_japanese_font()
    binaries = {name: find_tool(name) for name in ("ffmpeg", "ffprobe", "twitchdownloader")}
    return {
        "schema": 1,
        "workspace": "./workspace",
        "profile": {"twitch_login": "", "twitch_url": "", "youtube_channel_id": "", "youtube_channel_url": "", "youtube_channel_name": "", "browser_profile": "", "verified_related_links": []},
        "tools": {name: path_value(Path(value), base) if value else "" for name, value in binaries.items()},
        "render": {"encoder": "auto", "font": path_value(Path(font), base) if font else "", "font_name": family},
        "asr": {"model": MODEL, "revision": MODEL_REVISION, "device": "cpu", "compute_type": "int8"},
    }


def init_config(config: Path) -> bool:
    """Create only. Exclusive open also protects against concurrent setup runs."""
    config = config.resolve()
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(initial_config(config.parent), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return True
    except FileExistsError:
        return False


def probe(executable: str, args: list[str], expected: str, codes: tuple[int, ...] = (0,)) -> tuple[bool, str]:
    if not executable:
        return False, "Not found. Run setup.cmd or set tools paths in twitchcut.local.json."
    try:
        env = os.environ.copy()
        # .NET single-file apps need a writable extraction directory, including
        # when Codex runs inside a restricted host. This is a local runtime cache.
        if "twitchdownloader" in Path(executable).name.lower():
            env.setdefault("DOTNET_BUNDLE_EXTRACT_BASE_DIR", str(ROOT / ".setup" / "dotnet"))
        result = subprocess.run([executable, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, shell=False, env=env)
        output = result.stdout + "\n" + result.stderr
        valid = result.returncode in codes and expected.lower() in output.lower()
        detail = output.strip().splitlines()[0][:220] if output.strip() else "No output"
        return valid, f"{executable}: {detail}" if valid else f"Probe failed ({result.returncode}): {detail}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{executable}: {exc}"


def module_check(module: str) -> tuple[bool, str]:
    try:
        imported = importlib.import_module(module)
        return True, str(getattr(imported, "__version__", "import succeeded"))
    except Exception as exc:
        return False, f"Import failed: {exc}"


def validate_config(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["The config root must be a JSON object."]
    errors = []
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    if not isinstance(data.get("workspace"), str) or not data.get("workspace"):
        errors.append("workspace must be a nonempty path")
    for section in ("profile", "tools", "render", "asr"):
        if not isinstance(data.get(section), dict):
            errors.append(f"{section} must be an object")
    if errors:
        return errors
    for key in ("twitch_login", "twitch_url", "youtube_channel_id", "youtube_channel_url", "youtube_channel_name", "browser_profile"):
        if not isinstance(data["profile"].get(key), str):
            errors.append(f"profile.{key} must be a string")
    if not isinstance(data["profile"].get("verified_related_links"), list):
        errors.append("profile.verified_related_links must be an array")
    for section, keys in (("tools", ("ffmpeg", "ffprobe", "twitchdownloader")), ("render", ("font", "font_name", "encoder")), ("asr", ("model", "revision", "device", "compute_type"))):
        for key in keys:
            if not isinstance(data[section].get(key), str):
                errors.append(f"{section}.{key} must be a string")
    if data["render"].get("encoder") not in ("auto", "cpu", "libx264", "h264_nvenc"):
        errors.append("render.encoder must be auto, cpu, libx264, or h264_nvenc")
    if data["asr"].get("device") not in ("cpu", "cuda"):
        errors.append("asr.device must be cpu or cuda")
    return errors


def doctor(config: Path, require_asr: bool = False) -> dict:
    config = config.resolve()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, required: bool = True):
        checks.append({"name": name, "status": "ok" if ok else ("error" if required else "pending"), "required": required, "detail": detail})

    add("python", sys.version_info[:2] in ((3, 11), (3, 12)) and struct.calcsize("P") == 8, f"{platform.python_version()} / {struct.calcsize('P') * 8}-bit: {sys.executable}")
    add("platform", os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64"), f"{platform.system()} {platform.machine()}; Windows x64 is the supported setup target")
    data = None
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        errors = validate_config(data)
        add("config", not errors, "; ".join(errors) if errors else str(config))
        if errors:
            data = None
    except (OSError, ValueError) as exc:
        add("config", False, f"{exc}; create with: python scripts/setup_env.py init")
    for module in ("PIL", "numpy"):
        ok, detail = module_check(module)
        add(module, ok, detail)
    resolved = {}
    for name in ("ffmpeg", "ffprobe", "twitchdownloader"):
        resolved[name] = find_tool(name, data["tools"].get(name, "") if data else "", config.parent)
        td = name == "twitchdownloader"
        ok, detail = probe(resolved[name], ["--version" if td else "-version"], "TwitchDownloaderCLI" if td else name, (0, 1) if td else (0,))
        add(name, ok, detail)
        if td and ok:
            compatible, help_detail = probe(resolved[name], ["videodownload", "--help"], "--ffmpeg-path", (0, 1))
            add("twitchdownloader_options", compatible, "Required video download options are available" if compatible else help_detail)
    if resolved["ffmpeg"]:
        ok, detail = probe(resolved["ffmpeg"], ["-hide_banner", "-encoders"], "libx264")
        add("cpu_encoder", ok, "libx264 is available" if ok else detail)
        ok, detail = probe(resolved["ffmpeg"], ["-hide_banner", "-filters"], "subtitles")
        add("subtitle_filter", ok, "libass subtitles filter is available" if ok else detail)
    font = data["render"].get("font", "") if data else ""
    family = data["render"].get("font_name", "") if data else ""
    try:
        if not font or not family:
            raise ValueError("Set render.font and render.font_name to an installed Japanese font.")
        from PIL import ImageFont
        selected = ImageFont.truetype(str(local_path(font, config.parent)), 24)
        actual_family = selected.getname()[0]
        matches = actual_family.casefold().replace(" ", "") == family.casefold().replace(" ", "")
        add("font", matches, f"{actual_family} ({font}); configured ASS family: {family}" + ("" if matches else "; set font_name to the actual family above"))
    except Exception as exc:
        add("font", False, str(exc))
    workspace = local_path(data["workspace"] if data else "./workspace", config.parent)
    existing = workspace
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free_gib = shutil.disk_usage(existing).free / 1024 ** 3
        add("disk", free_gib >= 10, f"{free_gib:.1f} GiB free for {workspace}; full VODs plus renders often require 100+ GiB. Check actual VOD size before download.")
    except OSError as exc:
        add("disk", False, str(exc))
    asr_ok, asr_detail = module_check("faster_whisper")
    add("asr_runtime", asr_ok, asr_detail + ("; models are downloaded separately on first use" if asr_ok else "; run setup.cmd -WithAsr"), require_asr)
    add("asr_model", False, "Not loaded by doctor. Before a long job, download the pinned model and run a short transcription.", False)
    profile = data["profile"] if data else {}
    required_profile = ("twitch_login", "youtube_channel_id")
    missing = [key for key in required_profile if not isinstance(profile.get(key), str) or not profile.get(key, "").strip()]
    profile_errors = [f"missing {key}" for key in missing]
    if profile.get("youtube_channel_id") and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", profile["youtube_channel_id"]):
        profile_errors.append("youtube_channel_id must be an actual UC channel ID")
    add("recipient_profile", not profile_errors, "; ".join(profile_errors) if profile_errors else "Profile is populated; verify it against the actual channel before upload.", False)
    add("browser_upload", False, "Codex must check the browser connection, logged-in account, upload channel, and private visibility in YouTube Studio. No credentials were read.", False)
    ready = all(item["status"] == "ok" for item in checks if item["required"])
    return {"config": str(config), "setup_ready": ready, "asr_runtime_ready": asr_ok, "profile_ready": not profile_errors, "production_ready": False, "upload_ready": False, "checks": checks}


def install_ffmpeg(config: Path | None = None) -> Path:
    """Install a pinned, hash-checked archive inside the repo; never replace tools."""
    if os.name != "nt":
        raise RuntimeError("Automatic FFmpeg installation supports Windows x64 only.")
    selected = {}
    if config and config.is_file():
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("tools", {}), dict):
            raise ValueError("Existing config has invalid tools settings; it was preserved.")
        selected = data.get("tools", {})
    base = config.resolve().parent if config else ROOT
    ffmpeg = find_tool("ffmpeg", selected.get("ffmpeg", ""), base)
    ffprobe = find_tool("ffprobe", selected.get("ffprobe", ""), base)
    if ffmpeg and ffprobe:
        for binary, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
            ok, detail = probe(binary, ["-version"], name)
            if not ok:
                raise RuntimeError(detail)
        return Path(ffmpeg)
    destination = ROOT / ".tools" / "ffmpeg"
    if destination.exists():
        raise RuntimeError(f"Incomplete existing portable FFmpeg preserved at {destination}; inspect it before retrying.")
    downloads = ROOT / ".setup" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"{FFMPEG_NAME}.zip"
    if not archive.exists():
        partial = archive.with_suffix(".zip.part")
        if partial.exists():
            raise RuntimeError(f"Previous partial download preserved at {partial}; inspect/remove it before retrying.")
        request = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "TwitchCut-Setup/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("xb") as stream:
            shutil.copyfileobj(response, stream, 1024 * 1024)
        partial.replace(archive)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != FFMPEG_SHA256:
        raise RuntimeError(f"FFmpeg SHA256 mismatch. Archive preserved, not extracted: {archive}")
    stage = Path(tempfile.mkdtemp(prefix="ffmpeg-", dir=downloads))
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 1024 ** 3:
            raise RuntimeError("Unexpected FFmpeg archive size.")
        for entry in entries:
            parts = entry.filename.replace("\\", "/").rstrip("/").split("/")
            if not parts or parts[0] != FFMPEG_NAME or any(part in ("", ".", "..") or ":" in part or part.endswith((".", " ")) for part in parts):
                raise RuntimeError(f"Unsafe FFmpeg ZIP member: {entry.filename}")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Symlinks are not supported in the FFmpeg archive.")
            target = stage.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    extracted = stage / FFMPEG_NAME
    for name in ("ffmpeg", "ffprobe"):
        ok, detail = probe(str(extracted / "bin" / f"{name}.exe"), ["-version"], f"{name} version {FFMPEG_VERSION}")
        if not ok:
            raise RuntimeError(detail)
    destination.parent.mkdir(parents=True, exist_ok=True)
    extracted.rename(destination)
    stage.rmdir()
    return destination / "bin" / "ffmpeg.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "doctor"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--config", type=Path, default=ROOT / "twitchcut.local.json")
        if command == "doctor":
            command_parser.add_argument("--json", action="store_true")
            command_parser.add_argument("--require-asr", action="store_true")
    installer = sub.add_parser("install-ffmpeg")
    installer.add_argument("--config", type=Path, default=ROOT / "twitchcut.local.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            made = init_config(args.config)
            print(("Created: " if made else "Preserved existing config: ") + str(args.config.resolve()))
            return 0
        if args.command == "install-ffmpeg":
            print("FFmpeg available: " + str(install_ffmpeg(args.config)))
            return 0
        report = doctor(args.config, args.require_asr)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for check in report["checks"]:
                print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
            print("Base setup: " + ("READY" if report["setup_ready"] else "INCOMPLETE"))
            print("Production/upload: PENDING (model test and browser/channel verification required)")
        return 0 if report["setup_ready"] else 1
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
