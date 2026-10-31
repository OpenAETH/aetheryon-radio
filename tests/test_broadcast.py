"""Endpoint /broadcast: reemplazo de DJ, formato no soportado y keepalive."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import main  # noqa: E402

EBML = bytes.fromhex("1a45dfa3") + b"\x84" + b"\x42\x86\x81\x01"
WEBM_HEAD = (EBML + bytes.fromhex("18538067ff") + bytes.fromhex("1549a966") + b"\x84info"
             + bytes.fromhex("1f43b675") + b"\x84data")


@pytest.fixture(autouse=True)
def clean_globals():
    main.dj_ws, main.dj_connected = None, False
    yield
    main.dj_ws, main.dj_connected = None, False


def test_dj_stream_ping_and_disconnect_resumes_autodj():
    c = TestClient(main.app)
    with c.websocket_connect("/broadcast") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        ws.send_bytes(WEBM_HEAD)
        assert c.get("/status").json()["dj_online"] is True
    assert main.dj_connected is False and main.dj_ws is None


def test_second_dj_replaces_first_and_first_close_does_not_drop_the_live_one():
    c = TestClient(main.app)
    with c.websocket_connect("/broadcast") as first:
        first.send_text("ping")
        assert first.receive_text() == "pong"
        with c.websocket_connect("/broadcast") as second:
            with pytest.raises(WebSocketDisconnect) as e:
                first.receive_text()
            assert e.value.code == 1008                     # el panel lo usa para no reconectar en bucle
            second.send_text("ping")
            assert second.receive_text() == "pong"
            assert c.get("/status").json()["dj_online"] is True
        # al cerrarse el segundo, el estado vuelve a offline
    assert main.dj_connected is False


def test_non_webm_audio_is_rejected_with_explicit_error_and_code():
    c = TestClient(main.app)
    with c.websocket_connect("/broadcast") as ws:
        ws.send_bytes(b"OggS" + b"\x00" * 32)                  # Firefox: ogg
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["reason"] == "unsupported_format"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 1003
    assert main.dj_connected is False


def test_panel_guards_against_duplicate_sockets_and_reconnects():
    html = (Path(__file__).resolve().parents[1] / "frontend" / "dj.html").read_text(encoding="utf-8")
    assert "if (sock !== ws) return;" in html                  # eventos de sockets viejos se ignoran
    assert "WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;" in html
    assert "ev.code === 1008" in html and "wsRetryTimer = setTimeout(connectWS" in html
    assert "location.host" in html                              # URL por defecto derivada del origen
