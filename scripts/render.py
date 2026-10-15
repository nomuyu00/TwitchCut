#!/usr/bin/env python3
"""Render a reviewed source-time EDL without archive-specific code or audio gain."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from common import config_path, load_settings, workspace, tool, read_json, write_json, run_logged
from media_hash_cache import MediaHashCache

VERSION = 1
FPS = 30


def contained(root: Path, value: str | Path) -> Path:
    path = Path(value)
    path = (root / path if not path.is_absolute() else path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path must stay inside the configured workspace: {path}")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    result = digest.hexdigest()
    return result


def probe(ffprobe: str, path: Path) -> dict:
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True)
    return json.loads(result.stdout)


def finite(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def rectangle(value: dict, width: int, height: int, label: str) -> None:
    for key in ("x", "y", "w", "h"):
        if type(value.get(key)) is not int:
            raise ValueError(f"{label}.{key} must be an integer in original source pixels")
    if value["x"] < 0 or value["y"] < 0 or min(value["w"], value["h"]) < 2:
        raise ValueError(f"Invalid {label} rectangle")
    if value["x"] + value["w"] > width or value["y"] + value["h"] > height:
        raise ValueError(f"{label} extends beyond the source frame")
    if any(value[k] % 2 for k in ("x", "y", "w", "h")):
        raise ValueError(f"{label} coordinates and dimensions must be even for YUV420")


def source_geometry(info: dict) -> tuple[int, int, float]:
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("Source has no video stream")
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise ValueError("Source has no audio stream; a conversation clip requires source sound")
    if int(video.get("tags", {}).get("rotate", 0)) or any(
        abs(float(s.get("rotation", 0))) > .01 for s in video.get("side_data_list", [])
    ):
        raise ValueError("Rotated source needs a separately reviewed orientation-normalized copy")
    parts = video.get("sample_aspect_ratio", "1:1").split(":")
    sar = float(parts[0]) / float(parts[1]) if len(parts) == 2 and float(parts[1]) else 1.
    return int(video["width"]), int(video["height"]), sar


def validate_edl(edl: dict, info: dict) -> tuple[int, int]:
    if type(edl.get("schema")) is not int or edl["schema"] != 1:
        raise ValueError("EDL schema must be 1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", str(edl.get("id", ""))):
        raise ValueError("id must be a portable filename: letters, numbers, _ and -")
    if edl.get("kind") not in ("short", "topic", "digest"):
        raise ValueError("kind must be short, topic or digest; full archives are not supported")
    if not isinstance(edl.get("title"), str) or not edl["title"].strip():
        raise ValueError("title is required")
    date.fromisoformat(edl.get("source_date", ""))
    width, height, sar = source_geometry(info)
    duration = float(info["format"]["duration"])
    cuts = edl.get("cuts")
    if not isinstance(cuts, list) or not cuts:
        raise ValueError("At least one reviewed cut is required")
    for index, cut in enumerate(cuts):
        start, end = finite(cut.get("start"), "cut.start"), finite(cut.get("end"), "cut.end")
        if start < 0 or end <= start or end > duration + .005:
            raise ValueError(f"Cut {index + 1} is outside the source or empty")
        if end - start < 1 / FPS:
            raise ValueError("A cut must last at least one output frame")
        if "crop" in cut:
            rectangle(cut["crop"], width, height, f"cut {index + 1} crop")
        if edl["kind"] == "short" and "crop" not in cut and abs(width * sar / height - 9 / 16) > .01:
            raise ValueError("Every landscape-to-short cut needs an explicitly reviewed crop")
    if edl["kind"] == "short":
        geometry = (1080, 1920)
    else:
        geometry = (1920, 1080)
        for cut in cuts:
            crop = cut.get("crop", {"w": width, "h": height})
            if abs(crop["w"] * sar / crop["h"] / (16 / 9) - 1) > .01:
                raise ValueError("Topics/digest require an explicitly reviewed 16:9 crop for a non-16:9 source (include source SAR)")
    for collection in ("captions", "blur_regions"):
        if not isinstance(edl.get(collection, []), list):
            raise ValueError(f"{collection} must be an array")
        for cue in edl.get(collection, []):
            start, end = finite(cue.get("start"), collection), finite(cue.get("end"), collection)
            if start < 0 or end <= start or end > duration + .005:
                raise ValueError(f"Invalid source-time interval in {collection}")
            if collection == "blur_regions":
                rectangle(cue, width, height, "blur")
            else:
                if cue.get("speaker") not in ("streamer", "comment"):
                    raise ValueError("Each caption needs speaker: streamer or comment")
                if not isinstance(cue.get("text"), str) or not cue["text"].strip():
                    raise ValueError("Empty caption")
                # Do not silently render only half a sentence at an edit boundary.
                for cut in cuts:
                    if start < cut["end"] and end > cut["start"]:
                        if start < cut["start"] - .005 or end > cut["end"] + .005:
                            raise ValueError("A cut splits a caption; review the speech boundary and edit the EDL")
    mapped = mapped_captions(edl)
    if any(b["start"] < a["end"] - .005 for a, b in zip(mapped, mapped[1:])):
        raise ValueError("Captions overlap after mapping; choose the main utterance or revise cues")
    if "outro" in edl:
        outro = edl["outro"]
        if not isinstance(outro, dict) or not outro.get("image"):
            raise ValueError("outro requires an image path and duration")
        if not 0 < finite(outro.get("duration", 3), "outro.duration") <= 30:
            raise ValueError("outro.duration must be between 0 and 30 seconds")
    return geometry


def mapped_captions(edl: dict) -> list[dict]:
    result, offset = [], 0.
    for cut in edl["cuts"]:
        for cue in edl.get("captions", []):
            start, end = max(cue["start"], cut["start"]), min(cue["end"], cut["end"])
            if end > start:
                result.append({**cue, "start": offset + start - cut["start"],
                               "end": offset + end - cut["start"]})
        offset += cut["end"] - cut["start"]
    return sorted(result, key=lambda c: (c["start"], c["end"]))


def timestamp(seconds: float, ass: bool = False) -> str:
    resolution = 100 if ass else 1000
    number = round(seconds * resolution)
    hours, rest = divmod(number, resolution * 3600)
    minutes, rest = divmod(rest, resolution * 60)
    whole, fraction = divmod(rest, resolution)
    if ass:
        return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}"
    return f"{hours:02}:{minutes:02}:{whole:02},{fraction:03}"


def caption_layout(text: str, font: Path, width: int, height: int, short: bool) -> tuple[str, int]:
    from PIL import ImageFont
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = text.split("\n")
    if len(lines) > 2:
        raise ValueError("Captions may have at most two lines; split this cue")
    size = round((80 if short else 72) * width / (1080 if short else 1920))
    minimum = round(size * .85)
    available = width * .88
    for candidate_size in range(size, minimum - 1, -1):
        face = ImageFont.truetype(str(font), candidate_size)
        if max(face.getlength(line) for line in lines) <= available:
            return "\n".join(lines), candidate_size
        if len(lines) == 1:
            # Break at authored spaces/punctuation. Japanese phrase boundaries can be authored as \n.
            candidates = []
            for index in range(1, len(text)):
                if text[index - 1] not in " 、。！？!?，,；;：:":
                    continue
                left, right = text[:index].strip(), text[index:].strip()
                if not left or not right:
                    continue
                longest = max(face.getlength(left), face.getlength(right))
                if longest <= available:
                    candidates.append((longest, left, right))
            if candidates:
                _, left, right = min(candidates)
                return left + "\n" + right, candidate_size
    raise ValueError(f"Caption is too wide at a readable font size; add a natural line break or split cue: {text}")


def write_subtitles(edl: dict, directory: Path, geometry: tuple[int, int], font: Path,
                    font_name: str) -> list[dict]:
    width, height = geometry
    if any(c in font_name for c in ",\r\n"):
        raise ValueError("Invalid font family name")
    cues = mapped_captions(edl)
    size = round((80 if edl["kind"] == "short" else 72) * width / (1080 if edl["kind"] == "short" else 1920))
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
              "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
    for speaker, color in (("streamer", "&H00FF7000"), ("comment", "&H00F86AF7")):
        header += (f"Style: {speaker},{font_name},{size},&H00FFFFFF,&H00FFFFFF,{color},&H80000000,"
                   f"-1,0,0,0,100,100,0,0,1,{max(2, round(size * .07))},1,2,"
                   f"{round(width * .06)},{round(width * .06)},{round(height * .08)},1\n")
    ass = header + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    srt = []
    for index, cue in enumerate(cues, 1):
        display, actual_size = caption_layout(cue["text"], font, width, height, edl["kind"] == "short")
        cue["display_text"], cue["font_size"] = display, actual_size
        # ASS metacharacters must never become override tags supplied by a transcript.
        escaped = display.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")
        ass += (f"Dialogue: 0,{timestamp(cue['start'], True)},{timestamp(cue['end'], True)},"
                f"{cue['speaker']},,0,0,0,,{{\\fs{actual_size}}}{escaped}\n")
        srt.append(f"{index}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{display}\n")
    (directory / "subtitles.ass").write_text(ass, encoding="utf-8-sig")
    (directory / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8-sig")
    return cues


def cut_graph(edl: dict, cut: dict, geometry: tuple[int, int], frames: int | None = None) -> str:
    width, height = geometry
    graph = "[0:v:0]setpts=PTS-STARTPTS[v0];"
    active = [r for r in edl.get("blur_regions", []) if r["start"] < cut["end"] and r["end"] > cut["start"]]
    for index, region in enumerate(active):
        x, y, w, h = (region[k] for k in ("x", "y", "w", "h"))
        start, end = max(0, region["start"] - cut["start"]), min(cut["end"], region["end"]) - cut["start"]
        smallw, smallh = max(2, w // 16 // 2 * 2), max(2, h // 16 // 2 * 2)
        graph += (f"[v{index}]split[base{index}][sample{index}];"
                  f"[sample{index}]crop={w}:{h}:{x}:{y},scale={smallw}:{smallh}:flags=area,"
                  f"gblur=sigma=4,scale={w}:{h}:flags=bilinear[blur{index}];"
                  f"[base{index}][blur{index}]overlay={x}:{y}:enable='gte(t,{start:.6f})*lt(t,{end:.6f})'[v{index + 1}];")
    crop = cut.get("crop")
    tail = f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}," if crop else ""
    # Normalize display aspect before scaling. Never stretch one axis to fill a panel.
    tail += (f"scale=trunc(iw*sar/2)*2:ih,setsar=1,"
             f"scale={width}:{height}:force_original_aspect_ratio=increase,"
             f"crop={width}:{height},setsar=1,fps={FPS},format=yuv420p")
    if frames is not None:
        tail += f",tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame={frames}"
    graph += f"[v{len(active)}]{tail}[video];[0:a:0]asetpts=PTS-STARTPTS,aresample=48000[audio]"
    return graph


def encoder_args(name: str) -> list[str]:
    if name == "libx264":
        return ["-c:v", name, "-preset", "medium", "-crf", "19"]
    if name == "h264_nvenc":
        return ["-c:v", name, "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    raise ValueError("render.encoder must be auto, libx264 or h264_nvenc")


def choose_encoder(ffmpeg: str, requested: str, directory: Path) -> str:
    if requested == "auto":
        # CPU is the portable default; an explicit GPU choice gets a real hardware test.
        return "libx264"
    encoder_args(requested)
    if requested == "h264_nvenc":
        run_logged([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                    "color=s=320x180:r=30", "-frames:v", "1", *encoder_args(requested),
                    "-f", "null", "-"], directory / "encoder-test.log")
    return requested


def audio_comparison(ffmpeg: str, reference: Path, final: Path, directory: Path,
                     source_seconds: float) -> dict:
    """Compare decoded AAC to the retained source-cut PCM, with no gain adjustment."""
    import numpy as np
    source_energy = output_energy = error_energy = dot = 0.
    samples = 0
    gains = []
    sustained_gains = []
    sizes = [0, 0]
    processes = []
    # Bounded one-second buffers avoid multi-GB temporary raw files for a long digest.
    with ExitStack() as stack:
        try:
            for source, label in ((reference, "reference"), (final, "final")):
                log = stack.enter_context((directory / f"audio-{label}.log").open("wb"))
                processes.append(subprocess.Popen(
                    [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(source),
                     "-map", "0:a:0", "-t", f"{source_seconds:.9f}", "-ac", "2", "-ar", "48000",
                     "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE, stderr=log))
            while True:
                blocks = [p.stdout.read(48000 * 2 * 4) for p in processes]
                for index, block in enumerate(blocks):
                    sizes[index] += len(block)
                if not any(blocks):
                    break
                a, b = (np.frombuffer(block, dtype="<f4").astype(np.float64) for block in blocks)
                count = min(a.size, b.size)
                if not count:
                    continue
                a, b = a[:count], b[:count]
                ae, be = float(np.dot(a, a)), float(np.dot(b, b))
                source_energy += ae
                output_energy += be
                difference = a - b
                error_energy += float(np.dot(difference, difference))
                dot += float(np.dot(a, b))
                samples += count
                if ae / count > 1e-6 and be:
                    gain = 10 * math.log10(be / ae)
                    gains.append(gain)
                    if count >= 24000:
                        sustained_gains.append(gain)
                elif ae / count > 1e-6 and not be:
                    raise RuntimeError("Final audio introduces silence into an audible source interval")
            for process in processes:
                if process.wait() != 0:
                    raise RuntimeError(f"Audio comparison decode failed; inspect {directory}")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if process.stdout:
                    process.stdout.close()
    if abs(sizes[0] - sizes[1]) > 48000 * 2 * 4 * .08 or not samples:
        raise RuntimeError("Final AAC duration differs from source-cut PCM by more than 80 ms")
    gain_db = 10 * math.log10(output_energy / source_energy) if source_energy and output_energy else None
    if source_energy > 1e-9 and output_energy < source_energy * 1e-6:
        raise RuntimeError("Final audio is silent while the source-cut PCM is audible")
    if gain_db is not None and abs(gain_db) > .6:
        raise RuntimeError(f"Unexpected audio level change: {gain_db:.3f} dB")
    if sustained_gains and max(abs(gain) for gain in sustained_gains) > 1.5:
        raise RuntimeError("Unexpected audio level change exceeds 1.5 dB in an audible interval")
    return {"policy": "source_preserve_v1", "reference": str(reference), "samples_compared": samples,
            "gain_db": gain_db, "window_gain_db_min": min(gains) if gains else None,
            "window_gain_db_max": max(gains) if gains else None,
            "residual_rms": math.sqrt(error_energy / samples) if samples else None,
            "residual_relative_rms": math.sqrt(error_energy / source_energy) if source_energy else None,
            "correlation": dot / math.sqrt(source_energy * output_energy) if source_energy and output_energy else None,
            "aac_is_lossy": True, "listening_review": "pending"}


def render(edl_path: Path, settings: dict, output: str | Path | None = None, force: bool = False) -> dict:
    root = workspace(settings).resolve()
    edl_path = edl_path.resolve()
    edl = read_json(edl_path)
    source = contained(root, edl["source"])
    if not source.is_file():
        raise FileNotFoundError(f"Source not found: {source}")
    ffmpeg, ffprobe = tool(settings, "ffmpeg"), tool(settings, "ffprobe")
    info = probe(ffprobe, source)
    geometry = validate_edl(edl, info)
    destination = contained(root, output or edl.get("output") or f"outputs/{edl['id']}.mp4")
    if destination.suffix.lower() != ".mp4" or destination in (source, edl_path):
        raise ValueError("Output must be a new .mp4 path, separate from the source and EDL")
    profile = settings.get("render", {})
    font = config_path(settings, profile.get("font", ""))
    if not font.is_file():
        raise ValueError("Set render.font to an installed font file; run setup first")
    font = font.resolve()
    font_name = profile.get("font_name", "Yu Gothic")
    cache = MediaHashCache(root)
    source_sha = cache.hash(source)
    edl_sha = sha256(edl_path)
    outro = contained(root, edl["outro"]["image"]) if edl.get("outro") else None
    fingerprint = {"renderer_version": VERSION, "renderer_sha256": sha256(Path(__file__)),
                   "source_sha256": source_sha, "edl_sha256": edl_sha,
                   "font_sha256": sha256(font), "font_name": font_name,
                   "encoder_requested": profile.get("encoder", "auto"),
                   "outro_sha256": cache.hash(outro) if outro else None}
    fingerprint["ffmpeg_version"] = subprocess.check_output(
        [ffmpeg, "-version"], text=True, encoding="utf-8", errors="replace").splitlines()[0]
    result_path = destination.with_suffix(".result.json")
    if destination.exists():
        if result_path.exists():
            previous = read_json(result_path)
            if (previous.get("fingerprint") == fingerprint and previous.get("decode_verified") is True
                    and previous.get("sha256") == cache.hash(destination)
                    and all(destination.with_suffix(suffix).is_file()
                            and previous.get("sidecar_sha256", {}).get(suffix) == sha256(destination.with_suffix(suffix))
                            for suffix in (".ass", ".srt"))):
                return {**previous, "reused": True}
        if not force:
            raise FileExistsError(f"Output exists with different or unverified content: {destination}. Use --force to archive and replace it.")
    identity = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16]
    work = root / ".work" / "renders" / edl["id"] / identity
    work.mkdir(parents=True, exist_ok=True)
    write_json(work / "edl.snapshot.json", edl)
    cues = write_subtitles(edl, work, geometry, font, font_name)
    font_dir = work / "fonts"
    font_dir.mkdir(exist_ok=True)
    # Local font copy pins exactly which glyphs were measured. Never distributed with the repository.
    shutil.copy2(font, font_dir / font.name)
    encoder = choose_encoder(ffmpeg, profile.get("encoder", "auto"), work)
    parts, pcm_parts, elapsed = [], [], 0.
    for index, cut in enumerate(edl["cuts"]):
        part = work / f"cut-{index:04}.mkv"
        pcm = work / f"cut-{index:04}.wav"
        seconds = cut["end"] - cut["start"]
        frames = round((elapsed + seconds) * FPS) - round(elapsed * FPS)
        elapsed += seconds
        graph = cut_graph(edl, cut, geometry, frames)
        (work / f"cut-{index:04}.filter.txt").write_text(graph, encoding="utf-8")
        run_logged([ffmpeg, "-hide_banner", "-y", "-noautorotate", "-ss", str(cut["start"]),
                    "-t", str(seconds), "-i", str(source),
                    "-filter_complex_script", str(work / f"cut-{index:04}.filter.txt"),
                    "-map", "[video]", *encoder_args(encoder), "-bf", "0", "-an", str(part),
                    "-map", "[audio]", "-vn", "-c:a", "pcm_s24le", "-ar", "48000", "-t", str(seconds),
                    "-rf64", "auto", str(pcm)], work / f"cut-{index:04}.log")
        parts.append(part)
        pcm_parts.append(pcm)
    source_seconds = sum(c["end"] - c["start"] for c in edl["cuts"])
    if outro:
        duration = edl["outro"].get("duration", 3)
        width, height = geometry
        part = work / "outro.mkv"
        pcm = work / "outro.wav"
        source_audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        channels = source_audio["channels"]
        layout = source_audio.get("channel_layout") or ({1: "mono", 2: "stereo"}.get(channels))
        if not layout:
            raise ValueError("Outro requires an identified source audio channel layout")
        # Outro is intentional design, so proportional contain on a black canvas is appropriate.
        run_logged([ffmpeg, "-hide_banner", "-y", "-loop", "1", "-i", str(outro), "-f", "lavfi",
                    "-i", f"anullsrc=r=48000:cl={layout}", "-t", str(duration), "-map", "0:v:0", "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p",
                    *encoder_args(encoder), "-bf", "0", "-an", str(part), "-map", "1:a:0", "-vn", "-t", str(duration),
                    "-c:a", "pcm_s24le", "-ar", "48000", "-rf64", "auto", str(pcm)], work / "outro.log")
        parts.append(part)
        pcm_parts.append(pcm)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    video_master = work / "cuts-video.mkv"
    run_logged([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
                "-map", "0:v:0", "-c", "copy", str(video_master)], work / "concat.log", cwd=work)
    audio_listing = work / "concat-audio.txt"
    audio_listing.write_text("".join(f"file '{p.name}'\n" for p in pcm_parts), encoding="utf-8")
    master = work / "source-cuts-pcm.wav"
    run_logged([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "1", "-i", str(audio_listing),
                "-map", "0:a:0", "-c:a", "copy", "-rf64", "auto", str(master)], work / "concat-audio.log", cwd=work)
    staged = work / "final.mp4"
    run_logged([ffmpeg, "-hide_banner", "-y", "-i", str(video_master), "-i", str(master), "-map", "0:v:0", "-map", "1:a:0",
                "-vf", "ass=filename=subtitles.ass:fontsdir=fonts", *encoder_args(encoder),
                "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart",
                "-metadata", f"title={edl['title']}", "-metadata", f"date={edl['source_date']}",
                str(staged)], work / "final.log", cwd=work)
    final_info = probe(ffprobe, staged)
    video = next(s for s in final_info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in final_info["streams"] if s["codec_type"] == "audio")
    expected_duration = source_seconds + (edl.get("outro", {}).get("duration", 3) if outro else 0)
    actual_duration = float(final_info["format"]["duration"])
    if abs(actual_duration - expected_duration) > max(.15, len(edl["cuts"]) / FPS + .08):
        raise RuntimeError(f"Duration mismatch: expected {expected_duration}, found {actual_duration}")
    if (video["width"], video["height"]) != geometry or video["codec_name"] != "h264" or audio["codec_name"] != "aac":
        raise RuntimeError("Final media geometry/codec did not match render specification")
    run_logged([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(staged),
                "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], work / "decode.log")
    audio_qa = audio_comparison(ffmpeg, master, staged, work, source_seconds)
    if cache.hash(source) != source_sha or sha256(edl_path) != edl_sha:
        raise RuntimeError("Source or EDL changed during rendering; final was not installed")
    result = {"schema": 1, "id": edl["id"], "kind": edl["kind"], "title": edl["title"],
              "file": str(destination),
              "source": str(source), "source_sha256": source_sha, "edl_sha256": edl_sha,
              "duration": actual_duration, "width": geometry[0], "height": geometry[1], "fps": FPS,
              "encoder": encoder, "fingerprint": fingerprint, "decode_verified": True,
              "audio_qa": audio_qa, "captions": cues, "work": str(work),
              "human_qa": {"status": "pending", "required": ["speech boundaries and context", "listen to all cuts",
                            "subtitle accuracy, speaker, line breaks and font", "crop, heads/faces and aspect ratio",
                            "blur coverage at start/end and intervening frames"]},
              "created_utc": datetime.now(timezone.utc).isoformat(), "reused": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        revision = destination.parent / "revisions" / edl["id"] / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        revision.mkdir(parents=True)
        for suffix in (".mp4", ".ass", ".srt", ".result.json"):
            old = destination.with_suffix(suffix)
            if old.exists():
                shutil.copy2(old, revision / old.name)
    os.replace(staged, destination)
    result["sha256"] = cache.hash(destination)
    for suffix in (".ass", ".srt"):
        shutil.copy2(work / f"subtitles{suffix}", destination.with_suffix(suffix))
    result["sidecar_sha256"] = {suffix: sha256(destination.with_suffix(suffix)) for suffix in (".ass", ".srt")}
    write_json(result_path, result)
    return result


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edl", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true", help="Back up an existing different final before replacing it")
    args = parser.parse_args(argv)
    try:
        result = render(args.edl, load_settings(args.config), args.output, args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Render failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
