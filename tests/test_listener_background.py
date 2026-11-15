"""index.html: reproduccion en segundo plano (pantalla apagada). Comprobaciones estaticas, sin navegador.
El comportamiento real se probo en Chromium con el stream en vivo (ver el patch)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")


def script() -> str:
    return re.search(r"<script>(.*)</script>", HTML, re.S).group(1)


def test_script_is_valid_js(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node no disponible")
    f = tmp_path / "a.js"
    f.write_text(script(), encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_mobile_bypasses_webaudio_and_desktop_keeps_it():
    js = script()
    assert "var USE_WEBAUDIO" in js and "IS_MOBILE" in js
    assert "?webaudio=1" in js.replace("[?&]webaudio=1", "?webaudio=1")
    # todo uso del grafo WebAudio esta protegido por USE_WEBAUDIO
    init = js[js.index("function initAudio()"): js.index("// Rocket Radio: publica el nivel")]
    assert init.count("if (USE_WEBAUDIO)") == 2
    assert init.index("createMediaElementSource") > init.index("if (USE_WEBAUDIO)")
    assert "audioEl.volume" in init
    assert "var ctxOk = !USE_WEBAUDIO ||" in js          # el aviso de «tocá para escuchar» no depende del AudioContext


def test_media_session_wake_lock_and_recovery_present():
    js = script()
    for needle in ("navigator.mediaSession", "setActionHandler('play'", "setActionHandler('pause'", "MediaMetadata",
                   "navigator.wakeLock.request('screen')", "visibilitychange", "function resumeLive", "fetch('/status'"):
        assert needle in js, needle
    assert 'id="btn-wake"' in HTML and "hidden" in re.search(r'<button[^>]*id="btn-wake"[^>]*>', HTML).group(0)


def test_rocket_radio_sync_still_published_without_analyser():
    js = script()
    assert "(!rrAnalyser && !audioEl)" in js and "pos: audioEl ? audioEl.currentTime" in js
