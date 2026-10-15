"""Read recipient setup without changing it; synthetic tests use their own workspace."""
from pathlib import Path
import unittest

from common import config_path, load_settings, tool
from setup_env import find_japanese_font, find_tool

ROOT = Path(__file__).resolve().parents[1]


def render_environment():
    """Prefer actual local configuration, then portable installation/PATH/system fonts."""
    try:
        import PIL
        import numpy
    except ImportError:
        raise unittest.SkipTest("Pillow and NumPy are required; run setup.cmd first")
    config = ROOT / "twitchcut.local.json"
    settings = load_settings(config) if config.is_file() else None
    binaries = []
    for name in ("ffmpeg", "ffprobe"):
        if settings and settings.get("tools", {}).get(name):
            # Invalid explicit configuration must surface instead of silently testing another binary.
            binary = tool(settings, name)
        else:
            binary = find_tool(name)
        if not binary or not Path(binary).is_file():
            raise unittest.SkipTest(f"{name} is unavailable in local config, .tools or PATH; run setup.cmd")
        binaries.append(str(Path(binary).resolve()))
    profile = settings.get("render", {}) if settings else {}
    if profile.get("font"):
        font = config_path(settings, profile["font"])
        family = profile.get("font_name", "")
        if not font.is_file():
            raise ValueError(f"Configured render.font does not exist: {font}. Run doctor.")
        if not family:
            from PIL import ImageFont
            family = ImageFont.truetype(str(font), 20).getname()[0]
    else:
        filename, family = find_japanese_font()  # Respects WINDIR, including non-C Windows installs.
        font = Path(filename) if filename else None
        if font is None:
            # Retain the existing Linux smoke-test fallback; production setup is Windows-focused.
            candidate = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
            font, family = (candidate, "DejaVu Sans") if candidate.is_file() else (None, "")
        if font is None or not font.is_file():
            raise unittest.SkipTest("No configured or supported system font found; run setup.cmd")
    return binaries[0], binaries[1], font.resolve(), family
