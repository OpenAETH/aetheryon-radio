import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe no instalados")


def make_audio(path: Path, seconds: float = 1.0, freq: int = 440, fmt: str = None) -> Path:
    """Genera un tono con ffmpeg (el formato sale de la extensión)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
    if path.suffix == ".mp3":
        cmd += ["-c:a", "libmp3lame"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


@pytest.fixture
def tone(tmp_path):
    def _tone(name="t.wav", seconds=1.0, freq=440):
        return make_audio(tmp_path / name, seconds, freq)
    return _tone
