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


def _stream():
    from test_webm_framer import element
    cluster = bytes.fromhex("1f43b675") + b"\x01\xff\xff\xff\xff\xff\xff\xff"
    blocks = b"".join(element(b"\xa3", bytes([i]) * (3 + i)) for i in range(1, 9))
    return cluster + element(b"\xe7", b"\x00") + blocks + cluster + element(b"\xe7", b"\x05") + blocks


def test_aligner_never_emits_partial_elements_whatever_the_blob_boundaries():
    wire = _stream()
    for cut in range(1, len(wire)):
        al, out = main.WebMAligner(), b""
        for part in (wire[:cut], wire[cut:]):
            out += al.push(part)
            # cada tramo emitido es parseable de punta a punta (sin elementos cortados)
            pos = 0
            while pos < len(out):
                ident, payload, size, unknown = main._element(out, pos)
                pos = payload if ident == main.WebMFramer.CLUSTER else payload + size
            assert pos == len(out), f"corte en {cut}"
        out += al.push(b"")
        assert wire.startswith(out) and len(wire) - len(out) <= 12   # solo puede quedar retenido el ultimo elemento
    al = main.WebMAligner()
    assert al.push(wire[:5]) == b"" and al.push(wire[5:]) != b""


def test_aligner_passes_garbage_through_instead_of_dropping_audio():
    al = main.WebMAligner()
    junk = b"\x00\x00not-ebml"
    assert al.push(junk) == junk and al.broken
    assert al.push(b"more") == b"more"


def test_listener_joining_mid_dj_stream_only_receives_whole_elements():
    cluster_hdr = bytes.fromhex("1f43b675") + b"\x01\xff\xff\xff\xff\xff\xff\xff"
    head = EBML + bytes.fromhex("18538067ff") + bytes.fromhex("1549a966") + b"\x84info" + cluster_hdr
    c = TestClient(main.app)
    with c.websocket_connect("/broadcast") as dj:
        dj.send_bytes(head)
        wire = _stream()
        with c.websocket_connect("/listen") as lst:
            assert lst.receive_json()["type"] == "stream_start"
            lst.receive_bytes()                                  # init
            for part in (wire[:7], wire[7:40], wire[40:]):       # blobs cortados a mitad de elemento
                dj.send_bytes(part)
            got = b""
            for _ in range(40):                                  # acotado: nunca cuelga la suite
                got += lst.receive_bytes()
                if len(got) >= len(wire) - 12:
                    break
            pos = 0
            while pos < len(got):
                ident, payload, size, unknown = main._element(got, pos)
                pos = payload if ident == main.WebMFramer.CLUSTER else payload + size
            assert pos == len(got) and got.startswith(cluster_hdr)   # el oyente tardio arranca en un Cluster


def test_late_listener_waits_for_a_cluster_boundary_and_early_ones_do_not():
    from test_webm_framer import element
    cluster_hdr = bytes.fromhex("1f43b675") + b"\x01\xff\xff\xff\xff\xff\xff\xff"
    head = EBML + bytes.fromhex("18538067ff") + bytes.fromhex("1549a966") + b"\x84info" + cluster_hdr
    blk = element(b"\xa3", b"\x01" * 6)
    c = TestClient(main.app)
    with c.websocket_connect("/broadcast") as dj:
        dj.send_bytes(head)
        with c.websocket_connect("/listen") as late:
            assert late.receive_json()["type"] == "stream_start"
            late.receive_bytes()                                 # init
            dj.send_bytes(blk + blk)                             # sin Cluster: no debe llegarle nada util
            dj.send_bytes(cluster_hdr + blk)                     # primer Cluster: arranca aqui
            first = late.receive_bytes()
            assert first.startswith(cluster_hdr)
