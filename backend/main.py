"""AETHERYON RADIO: WebSocket relay + server-side AutoDJ.

The stream protocol is synchronized per session:
  JSON stream_start -> binary WebM initialization segment -> binary media bytes.
The initialization segment is parsed from EBML structure, never guessed from a
WebSocket/MediaRecorder/pipe chunk boundary.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LISTENER_TIMEOUT = int(os.getenv("LISTENER_TIMEOUT", "60"))
DJ_SEND_TIMEOUT = float(os.getenv("DJ_SEND_TIMEOUT", "10"))
MAX_LISTENERS = int(os.getenv("MAX_LISTENERS", "200"))
AUTODJ_ENABLED = os.getenv("AUTODJ_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
AUTODJ_PLAYLIST = Path(os.getenv("AUTODJ_PLAYLIST", "/music/playlist.txt"))
AUTODJ_RESTART_DELAY = float(os.getenv("AUTODJ_RESTART_DELAY", "3"))
MUSIC_DIR = Path(os.getenv("MUSIC_DIR", "/music")).resolve()
STREAM_MIME = 'audio/webm;codecs="opus"'

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("aetheryon-radio")
CORS_HEADERS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"}
app = FastAPI(title="AETHERYON Radio Server", version="3.0-autodj")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

listeners: Dict[WebSocket, float] = {}
dj_ws: Optional[WebSocket] = None
dj_connected = False
active_source: Optional[str] = None
stream_session: Optional[str] = None
stream_init: Optional[bytes] = None
stream_mime = STREAM_MIME
stream_lock = asyncio.Lock()
autodj_process: Optional[asyncio.subprocess.Process] = None
autodj_task: Optional[asyncio.Task] = None
start_time = time.time()
stats = {"chunks_relayed": 0, "bytes_relayed": 0, "listeners_peak": 0,
         "listener_timeouts": 0, "dj_sessions": 0, "autodj_restarts": 0}


def _vint(data: bytes, offset: int, *, is_size: bool) -> Optional[Tuple[int, int, bool]]:
    """Decode one EBML variable-length integer: (value, length, unknown-size)."""
    if offset >= len(data):
        return None
    first = data[offset]
    if first == 0:
        raise ValueError("Invalid EBML VINT: leading zero")
    mask = 0x80
    length = 1
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > (8 if is_size else 4):
            raise ValueError("Invalid EBML VINT length")
    if offset + length > len(data):
        return None
    value = first & (mask - 1) if is_size else first
    for b in data[offset + 1:offset + length]:
        value = (value << 8) | b
    unknown = is_size and value == (1 << (7 * length)) - 1
    return value, length, unknown


def _element(data: bytes, offset: int) -> Optional[Tuple[int, int, int, bool]]:
    """Return (element_id, payload_offset, payload_size, unknown_size)."""
    ident = _vint(data, offset, is_size=False)
    if ident is None:
        return None
    element_id, id_len, _ = ident
    size_info = _vint(data, offset + id_len, is_size=True)
    if size_info is None:
        return None
    size, size_len, unknown = size_info
    return element_id, offset + id_len + size_len, size, unknown


class WebMFramer:
    """Split an incoming byte stream at the first Segment-level Cluster element.

    WebSocket frames and stdout reads are arbitrary transport boundaries. This
    parser buffers until EBML says the initialization elements have ended.
    """
    EBML = 0x1A45DFA3
    SEGMENT = 0x18538067
    CLUSTER = 0x1F43B675

    def __init__(self):
        self.buffer = bytearray()
        self.initialized = False
        self.segment_payload_offset: Optional[int] = None

    def feed(self, data: bytes) -> Tuple[Optional[bytes], bytes]:
        if self.initialized:
            return None, data
        self.buffer.extend(data)
        buf = bytes(self.buffer)
        try:
            # EBML header is the first finite-size top-level element.
            first = _element(buf, 0)
            if first is None:
                return None, b""
            ident, payload, size, unknown = first
            if ident != self.EBML or unknown:
                raise ValueError("Stream does not begin with an EBML header")
            next_pos = payload + size
            segment = _element(buf, next_pos)
            if segment is None:
                return None, b""
            seg_id, seg_payload, _seg_size, _seg_unknown = segment
            if seg_id != self.SEGMENT:
                raise ValueError("EBML Segment element not found")
            pos = seg_payload
            while True:
                item = _element(buf, pos)
                if item is None:
                    return None, b""
                elem_id, elem_payload, elem_size, elem_unknown = item
                if elem_id == self.CLUSTER:
                    init = buf[:pos]
                    media = buf[pos:]
                    self.initialized = True
                    self.buffer.clear()
                    return init, media
                if elem_unknown:
                    # Unknown-sized elements before Cluster cannot be skipped
                    # safely, so fail closed instead of guessing a boundary.
                    raise ValueError(f"Unexpected unknown-sized EBML element 0x{elem_id:x} before Cluster")
                end = elem_payload + elem_size
                if end > len(buf):
                    return None, b""
                pos = end
        except ValueError:
            raise


async def _send_control(ws: WebSocket, payload: dict) -> bool:
    try:
        await asyncio.wait_for(ws.send_text(json.dumps(payload)), timeout=DJ_SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def _send_binary(ws: WebSocket, data: bytes) -> bool:
    try:
        await asyncio.wait_for(ws.send_bytes(data), timeout=DJ_SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def _fanout_binary(data: bytes, *, session: str) -> None:
    if not data:
        return
    async with stream_lock:
        if session != stream_session:
            return  # stale producer after a source switch
        snapshot = list(listeners)
        results = await asyncio.gather(*(_send_binary(ws, data) for ws in snapshot), return_exceptions=True)
        for ws, ok in zip(snapshot, results):
            if ok is not True:
                listeners.pop(ws, None)
        stats["chunks_relayed"] += 1
        stats["bytes_relayed"] += len(data)


async def _begin_stream(source: str) -> str:
    global active_source, stream_session, stream_init
    async with stream_lock:
        active_source = source
        stream_session = uuid.uuid4().hex
        stream_init = None
        session = stream_session
        payload = {"type": "stream_start", "session_id": session, "source": source, "mime": STREAM_MIME}
        snapshot = list(listeners)
        results = await asyncio.gather(*(_send_control(ws, payload) for ws in snapshot), return_exceptions=True)
        for ws, ok in zip(snapshot, results):
            if ok is not True:
                listeners.pop(ws, None)
        logger.info("New stream session source=%s id=%s listeners=%d", source, session[:8], len(listeners))
        return session


async def _publish_stream_data(session: str, framer: WebMFramer, data: bytes) -> None:
    global stream_init
    init, media = framer.feed(data)
    if init is not None:
        async with stream_lock:
            if session != stream_session:
                return
            stream_init = init
            snapshot = list(listeners)
            results = await asyncio.gather(*(_send_binary(ws, init) for ws in snapshot), return_exceptions=True)
            for ws, ok in zip(snapshot, results):
                if ok is not True:
                    listeners.pop(ws, None)
            logger.info("WebM init synchronized (%d bytes) session=%s", len(init), session[:8])
    if media:
        await _fanout_binary(media, session=session)


def _load_playlist() -> list[Path]:
    supported = {".mp3", ".wav", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".webm"}
    if not AUTODJ_PLAYLIST.is_file():
        # Zero-configuration fallback: play every supported audio file in order.
        return sorted((p.resolve() for p in MUSIC_DIR.iterdir()
                       if p.is_file() and p.suffix.lower() in supported), key=lambda p: p.name.lower())
    tracks = []
    for raw in AUTODJ_PLAYLIST.read_text(encoding="utf-8").splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        candidate = (MUSIC_DIR / name).resolve()
        if candidate.parent != MUSIC_DIR or not candidate.is_file():
            logger.warning("AutoDJ playlist entry ignored (missing or outside music dir): %s", name)
            continue
        tracks.append(candidate)
    if not tracks:
        # An empty/missing playlist should not prevent a simple music/ folder from working.
        tracks = sorted((p.resolve() for p in MUSIC_DIR.iterdir()
                         if p.is_file() and p.suffix.lower() in supported), key=lambda p: p.name.lower())
    return tracks


def _ffconcat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


async def _autodj_supervisor() -> None:
    global autodj_process
    while True:
        if dj_connected:
            await asyncio.sleep(0.5)
            continue
        tracks = _load_playlist()
        if not tracks:
            await asyncio.sleep(max(AUTODJ_RESTART_DELAY, 5))
            continue
        playlist_file = Path("/tmp/aetheryon-radio-playlist.ffconcat")
        content = "ffconcat version 1.0\n" + "".join(f"file '{_ffconcat_escape(p)}'\n" for p in tracks)
        playlist_file.write_text(content, encoding="utf-8")
        session = await _begin_stream("autodj")
        framer = WebMFramer()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-re",
               "-f", "concat", "-safe", "0", "-i", str(playlist_file), "-vn",
               "-c:a", "libopus", "-b:a", "96k", "-application", "audio",
               "-cluster_time_limit", "1000", "-live", "1", "-f", "webm", "pipe:1"]
        try:
            logger.info("AutoDJ starting with %d track(s)", len(tracks))
            autodj_process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                                  stderr=asyncio.subprocess.DEVNULL)
            while not dj_connected and session == stream_session:
                chunk = await autodj_process.stdout.read(8192)
                if not chunk:
                    break
                await _publish_stream_data(session, framer, chunk)
            if autodj_process.returncode is None:
                autodj_process.terminate()
                try:
                    await asyncio.wait_for(autodj_process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    autodj_process.kill()
                    await autodj_process.wait()
        except FileNotFoundError:
            logger.exception("FFmpeg is missing; AutoDJ cannot start")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            if autodj_process and autodj_process.returncode is None:
                autodj_process.terminate()
                await autodj_process.wait()
            raise
        except Exception:
            logger.exception("AutoDJ process failed; restarting")
        finally:
            autodj_process = None
        if not dj_connected:
            stats["autodj_restarts"] += 1
            await asyncio.sleep(AUTODJ_RESTART_DELAY)


async def _kick_dead_listeners() -> None:
    now = time.time()
    dead = [ws for ws, last in list(listeners.items()) if now - last > LISTENER_TIMEOUT]
    for ws in dead:
        listeners.pop(ws, None)
        stats["listener_timeouts"] += 1
        try:
            await ws.close(code=1001, reason="timeout")
        except Exception:
            pass


@app.on_event("startup")
async def startup() -> None:
    global autodj_task
    asyncio.create_task(_watchdog())
    if AUTODJ_ENABLED:
        autodj_task = asyncio.create_task(_autodj_supervisor())
        logger.info("AutoDJ supervisor enabled; playlist=%s", AUTODJ_PLAYLIST)


async def _watchdog() -> None:
    while True:
        await asyncio.sleep(30)
        await _kick_dead_listeners()


@app.on_event("shutdown")
async def shutdown() -> None:
    global autodj_task, autodj_process
    if autodj_task:
        autodj_task.cancel()
        try:
            await autodj_task
        except asyncio.CancelledError:
            pass
    if autodj_process and autodj_process.returncode is None:
        autodj_process.terminate()
        await autodj_process.wait()
    for ws in list(listeners):
        try:
            await ws.close(code=1001, reason="server shutdown")
        except Exception:
            pass
    listeners.clear()


@app.get("/")
async def root():
    return JSONResponse({"service": "AETHERYON Radio", "version": "3.0-autodj", "status": "online",
                         "source": active_source, "dj_online": dj_connected, "listeners": len(listeners),
                         "uptime_s": round(time.time() - start_time), "stats": stats}, headers=CORS_HEADERS)


@app.get("/status")
async def status():
    return JSONResponse({"dj_online": dj_connected, "source": active_source, "stream_ready": stream_init is not None,
                         "listeners": len(listeners), "peak": stats["listeners_peak"],
                         "chunks_relayed": stats["chunks_relayed"], "bytes_relayed": stats["bytes_relayed"],
                         "uptime_s": round(time.time() - start_time), "autodj_enabled": AUTODJ_ENABLED}, headers=CORS_HEADERS)


@app.options("/status")
async def status_options():
    return JSONResponse({}, headers=CORS_HEADERS)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "source": active_source, "stream_ready": stream_init is not None})


@app.websocket("/broadcast")
async def broadcast_endpoint(ws: WebSocket):
    global dj_ws, dj_connected
    await ws.accept()
    if dj_ws is not None and dj_ws is not ws:
        try:
            await dj_ws.close(code=1008, reason="replaced by new DJ")
        except Exception:
            pass
    dj_ws = ws
    dj_connected = True
    stats["dj_sessions"] += 1
    # Stop the AutoDJ process; its supervisor resumes it after the DJ disconnects.
    if autodj_process and autodj_process.returncode is None:
        autodj_process.terminate()
    session = await _begin_stream("live_dj")
    framer = WebMFramer()
    logger.info("Live DJ connected; session=%s", session[:8])
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                if msg["text"] == "ping":
                    await ws.send_text("pong")
                # DJ may send a metadata hint in future; text frames are not audio.
                continue
            chunk = msg.get("bytes")
            if chunk:
                await _publish_stream_data(session, framer, chunk)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Live DJ stream failed")
    finally:
        if dj_ws is ws:
            dj_ws = None
            dj_connected = False
            logger.info("Live DJ disconnected; AutoDJ will resume")


@app.websocket("/listen")
async def listen_endpoint(ws: WebSocket):
    if len(listeners) >= MAX_LISTENERS:
        await ws.close(code=1013, reason="server full")
        return
    await ws.accept()
    async with stream_lock:
        listeners[ws] = time.time()
        if stream_session:
            await _send_control(ws, {"type": "stream_start", "session_id": stream_session,
                                     "source": active_source, "mime": stream_mime})
            if stream_init is not None:
                await _send_binary(ws, stream_init)
        else:
            await _send_control(ws, {"type": "waiting", "message": "Waiting for audio source"})
        stats["listeners_peak"] = max(stats["listeners_peak"], len(listeners))
    logger.info("Listener joined; total=%d", len(listeners))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            listeners[ws] = time.time()
            if msg.get("text") == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Listener connection ended: %s", exc)
    finally:
        listeners.pop(ws, None)
        logger.info("Listener left; total=%d", len(listeners))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)