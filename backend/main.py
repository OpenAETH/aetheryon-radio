"""
AETHERYON Radio — Streaming Server
FastAPI + WebSocket broadcast relay
DJ → WS /broadcast  →  Server  →  WS /listen  → Listeners
"""

import asyncio
import logging
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "ngrok-skip-browser-warning": "1",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aetheryon-radio")

app = FastAPI(title="AETHERYON Radio Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# State
listeners: Set[WebSocket] = set()
dj_connected: bool = False
broadcast_stats = {"chunks_relayed": 0, "listeners_peak": 0}


@app.get("/")
async def root():
    return JSONResponse({
        "service": "AETHERYON Radio",
        "status": "online",
        "dj_online": dj_connected,
        "listeners": len(listeners),
        "stats": broadcast_stats,
    }, headers=CORS_HEADERS)


@app.get("/status")
async def status():
    return JSONResponse({
        "dj_online": dj_connected,
        "listeners": len(listeners),
        "peak": broadcast_stats["listeners_peak"],
        "chunks_relayed": broadcast_stats["chunks_relayed"],
    }, headers=CORS_HEADERS)

@app.options("/status")
async def status_options():
    return JSONResponse({}, headers=CORS_HEADERS)


@app.websocket("/broadcast")
async def broadcast_endpoint(ws: WebSocket):
    """DJ connects here to push audio chunks."""
    global dj_connected
    await ws.accept()
    dj_connected = True
    logger.info(f"🎙️  DJ connected. Listeners: {len(listeners)}")

    try:
        while True:
            # Receive raw audio chunk (binary PCM/WebM/Opus from browser)
            chunk = await ws.receive_bytes()
            broadcast_stats["chunks_relayed"] += 1

            # Fan-out to all listeners concurrently
            if listeners:
                dead = set()
                results = await asyncio.gather(
                    *[l.send_bytes(chunk) for l in listeners],
                    return_exceptions=True,
                )
                for listener, result in zip(list(listeners), results):
                    if isinstance(result, Exception):
                        dead.add(listener)
                for d in dead:
                    listeners.discard(d)

    except WebSocketDisconnect:
        logger.info("🎙️  DJ disconnected.")
    finally:
        dj_connected = False


@app.websocket("/listen")
async def listen_endpoint(ws: WebSocket):
    """Listeners connect here to receive the stream."""
    await ws.accept()
    listeners.add(ws)
    count = len(listeners)
    broadcast_stats["listeners_peak"] = max(broadcast_stats["listeners_peak"], count)
    logger.info(f"👂  Listener joined. Total: {count}")

    try:
        # Keep alive — listener is passive, just receives
        while True:
            # Handle pings / disconnects from client side
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        listeners.discard(ws)
        logger.info(f"👂  Listener left. Total: {len(listeners)}")
    except Exception:
        listeners.discard(ws)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)