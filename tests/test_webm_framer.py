import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from main import WebMFramer


def vint_size(n):
    # One-byte EBML size for these small synthetic test elements.
    assert 0 <= n < 127
    return bytes([0x80 | n])


def element(ident: bytes, payload: bytes) -> bytes:
    return ident + vint_size(len(payload)) + payload


def test_init_is_split_at_ebml_cluster_not_transport_chunk():
    ebml = element(bytes.fromhex("1a45dfa3"), b"\x42\x86\x81\x01")
    # Unknown-sized Segment header: 18 53 80 67 + FF.
    segment_header = bytes.fromhex("18538067ff")
    info = element(bytes.fromhex("1549a966"), b"info")
    tracks = element(bytes.fromhex("1654ae6b"), b"tracks")
    cluster = bytes.fromhex("1f43b675") + b"\x80" + b"payload"
    wire = ebml + segment_header + info + tracks + cluster
    framer = WebMFramer()
    init, media = framer.feed(wire[:9])
    assert init is None and media == b""
    init, media = framer.feed(wire[9:])
    assert init == ebml + segment_header + info + tracks
    assert media == cluster
    assert framer.feed(b"next-cluster") == (None, b"next-cluster")
