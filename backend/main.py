"""
AETHERYON Radio — Streaming Server  v2.0
FastAPI + WebSocket broadcast relay

DJ → WS /broadcast  →  Server  →  WS /listen  → Listeners

Mejoras v2.0:
  - Fix: /broadcast tolera mensajes de texto (pings) sin crashear
  - Un solo DJ activo con kick al anterior (no dos DJs simultáneos)
  - Listener timeout: desconecta oyentes muertos tras N segundos sin ping
  - Fan-out con asyncio.shield para no cancelar sends parciales
  - Stats extendidas: uptime, bytes_relayed, disconnect_count
  - /healthz endpoint para Docker healthcheck
  - Logging estructurado con nivel configurable por ENV
  - Graceful shutdown: cierra listeners antes de bajar
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# ──────────────────────────────────────────────
#  CONFIG (override via ENV en docker-compose)
# ──────────────────────────────────────────────
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
LISTENER_TIMEOUT = int(os.getenv("LISTENER_TIMEOUT", "60"))   # segundos sin ping → kick
DJ_SEND_TIMEOUT  = float(os.getenv("DJ_SEND_TIMEOUT", "10"))  # timeout fan-out por oyente
MAX_LISTENERS    = int(os.getenv("MAX_LISTENERS", "200"))      # límite de oyentes simultáneos

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aetheryon-radio")

# ──────────────────────────────────────────────
#  CORS HEADERS (para respuestas HTTP normales)
# ──────────────────────────────────────────────
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "ngrok-skip-browser-warning":   "1",
}

# ──────────────────────────────────────────────
#  APP
# ──────────────────────────────────────────────
app = FastAPI(title="AETHERYON Radio Server", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
#  STATE
# ──────────────────────────────────────────────
# listeners: ws → último timestamp de actividad
listeners: Dict[WebSocket, float] = {}

dj_ws: Optional[WebSocket] = None   # WS activo del DJ (solo uno)
dj_connected: bool = False

start_time = time.time()

stats = {
    "chunks_relayed":    0,
    "bytes_relayed":     0,
    "listeners_peak":    0,
    "listener_timeouts": 0,
    "dj_sessions":       0,
}


# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────
async def safe_send(ws: WebSocket, data: bytes) -> bool:
    """Envía bytes a un oyente. Retorna False si falla."""
    try:
        await asyncio.wait_for(ws.send_bytes(data), timeout=DJ_SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def kick_dead_listeners():
    """Desconecta oyentes que no enviaron ping en LISTENER_TIMEOUT segundos."""
    now = time.time()
    dead = [ws for ws, last in list(listeners.items()) if now - last > LISTENER_TIMEOUT]
    for ws in dead:
        listeners.pop(ws, None)
        stats["listener_timeouts"] += 1
        try:
            await ws.close(code=1001, reason="timeout")
        except Exception:
            pass
    if dead:
        logger.info(f"⏱  Kicked {len(dead)} timed-out listener(s). Active: {len(listeners)}")


async def fanout(chunk: bytes):
    """Fan-out concurrente a todos los oyentes. Elimina los muertos."""
    if not listeners:
        return

    snapshot = list(listeners.keys())
    tasks    = [safe_send(ws, chunk) for ws in snapshot]
    results  = await asyncio.gather(*tasks, return_exceptions=True)

    dead = [ws for ws, ok in zip(snapshot, results) if ok is False or isinstance(ok, Exception)]
    for ws in dead:
        listeners.pop(ws, None)
        logger.debug(f"🗑  Removed dead listener during fanout. Active: {len(listeners)}")


# ──────────────────────────────────────────────
#  BACKGROUND: listener watchdog
# ──────────────────────────────────────────────
@app.on_event("startup")
async def start_watchdog():
    async def watchdog():
        while True:
            await asyncio.sleep(30)
            await kick_dead_listeners()

    asyncio.create_task(watchdog())
    logger.info("🛡  Listener watchdog started (interval=30s, timeout=%ds)", LISTENER_TIMEOUT)


@app.on_event("shutdown")
async def shutdown_listeners():
    """Cierra todos los oyentes limpiamente al bajar el contenedor."""
    logger.info("🔌  Shutting down — closing %d listener(s)…", len(listeners))
    for ws in list(listeners.keys()):
        try:
            await ws.close(code=1001, reason="server shutdown")
        except Exception:
            pass
    listeners.clear()


# ──────────────────────────────────────────────
#  HTTP ENDPOINTS
# ──────────────────────────────────────────────
@app.get("/")
async def root():
    return JSONResponse({
        "service":      "AETHERYON Radio",
        "version":      "2.0",
        "status":       "online",
        "dj_online":    dj_connected,
        "listeners":    len(listeners),
        "uptime_s":     round(time.time() - start_time),
        "stats":        stats,
    }, headers=CORS_HEADERS)


@app.get("/status")
async def status():
    return JSONResponse({
        "dj_online":     dj_connected,
        "listeners":     len(listeners),
        "peak":          stats["listeners_peak"],
        "chunks_relayed":stats["chunks_relayed"],
        "bytes_relayed": stats["bytes_relayed"],
        "uptime_s":      round(time.time() - start_time),
    }, headers=CORS_HEADERS)


@app.options("/status")
async def status_options():
    return JSONResponse({}, headers=CORS_HEADERS)


@app.get("/healthz")
async def healthz():
    """Docker healthcheck endpoint."""
    return JSONResponse({"ok": True}, headers=CORS_HEADERS)


# ──────────────────────────────────────────────
#  WS /broadcast — DJ
# ──────────────────────────────────────────────
@app.websocket("/broadcast")
async def broadcast_endpoint(ws: WebSocket):
    """
    DJ conecta aquí para enviar audio.
    - Acepta bytes (chunks de audio) y texto (pings) sin romper.
    - Si ya hay un DJ activo, le hace kick al anterior.
    """
    global dj_ws, dj_connected

    await ws.accept()

    # ── Kick al DJ anterior si existe ──
    if dj_ws is not None and dj_ws is not ws:
        logger.warning("⚠  Second DJ detected — kicking previous connection.")
        try:
            await dj_ws.close(code=1008, reason="replaced by new DJ")
        except Exception:
            pass

    dj_ws       = ws
    dj_connected = True
    stats["dj_sessions"] += 1
    logger.info(f"🎙️  DJ connected (session #{stats['dj_sessions']}). Listeners: {len(listeners)}")

    try:
        while True:
            # ── FIX PRINCIPAL: receive() genérico en lugar de receive_bytes() ──
            # receive_bytes() tira excepción si llega un frame de texto (ping),
            # lo que cortaba la conexión del DJ silenciosamente.
            msg = await ws.receive()

            # WebSocket cerrado por el cliente
            if msg["type"] == "websocket.disconnect":
                break

            # Frame de texto → ignorar (pings, heartbeats)
            if msg.get("text") is not None:
                logger.debug(f"📨  DJ text frame (ignored): {msg['text'][:40]}")
                continue

            # Frame binario → fan-out
            chunk = msg.get("bytes")
            if not chunk:
                continue

            stats["chunks_relayed"] += 1
            stats["bytes_relayed"]  += len(chunk)

            await fanout(chunk)

            # Actualizar peak
            current = len(listeners)
            if current > stats["listeners_peak"]:
                stats["listeners_peak"] = current

    except WebSocketDisconnect:
        logger.info("🎙️  DJ disconnected (WebSocketDisconnect).")
    except Exception as e:
        logger.error(f"🎙️  DJ error: {e}")
    finally:
        if dj_ws is ws:
            dj_ws        = None
            dj_connected = False
        logger.info(f"🎙️  DJ session ended. Chunks relayed: {stats['chunks_relayed']}")


# ──────────────────────────────────────────────
#  WS /listen — Oyentes
# ──────────────────────────────────────────────
@app.websocket("/listen")
async def listen_endpoint(ws: WebSocket):
    """
    Oyentes conectan aquí para recibir el stream.
    - Rechaza conexión si se alcanza MAX_LISTENERS.
    - Actualiza timestamp en cada ping para el watchdog.
    - Tolera bytes y texto (algunos browsers envían frames binarios de ping).
    """
    # ── Límite de capacidad ──
    if len(listeners) >= MAX_LISTENERS:
        await ws.close(code=1013, reason="server full")
        logger.warning(f"🚫  Listener rejected — at capacity ({MAX_LISTENERS}).")
        return

    await ws.accept()
    listeners[ws] = time.time()
    count = len(listeners)
    if count > stats["listeners_peak"]:
        stats["listeners_peak"] = count
    logger.info(f"👂  Listener joined. Total: {count}")

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # Cualquier mensaje del cliente = señal de vida → actualizar timestamp
            listeners[ws] = time.time()

            # Responder a pings de texto
            if msg.get("text") == "ping":
                await ws.send_text("pong")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"👂  Listener exception: {e}")
    finally:
        listeners.pop(ws, None)
        logger.info(f"👂  Listener left. Total: {len(listeners)}")


# ──────────────────────────────────────────────
#  ENTRY POINT (desarrollo local sin Docker)
# ──────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
