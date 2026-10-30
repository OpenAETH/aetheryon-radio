"""AETHERYON RADIO: API HTTP de la playlist dinámica (servicio→servicio).

    PUT    /api/playlist/items/{external_id}   sube un audio ya generado y lo encola (idempotente)
    GET    /api/playlist/items/{id}            estado de un ítem (acepta id interno o external_id)
    DELETE /api/playlist/items/{id}            retira de la cola / corta si está al aire
    GET    /api/playlist                       cola, en emisión, recientes, política y motor
    GET|PATCH /api/playlist/policy             política de conservación de archivos
    GET    /api/playlist/capabilities          qué sabe hacer esta radio (p. ej. TTS propio)
    POST   /api/playlist/tts                   síntesis en Radio (501 hasta registrar un proveedor TTS)

Autenticación: `Authorization: Bearer <RADIO_API_TOKEN>`. Sin token configurado la API queda deshabilitada (503):
nunca abierta por omisión. El cuerpo del PUT es el audio crudo (sin multipart: no agrega dependencias).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from playlist import KINDS, PRIORITIES, PLAYING, QUEUED, PlaylistStore, PolicyError, QueueFull

logger = logging.getLogger("aetheryon-radio.playlist-api")

EXT_BY_FORMAT = (("mp3", "mp3"), ("wav", "wav"), ("ogg", "ogg"), ("flac", "flac"), ("matroska", "webm"),
                 ("webm", "webm"), ("mov", "m4a"), ("mp4", "m4a"), ("aac", "aac"))
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
TtsProvider = Callable[[str, str], Awaitable[bytes]]       # (texto, voz) -> bytes de audio


class Settings:
    def __init__(self) -> None:
        self.token = os.getenv("RADIO_API_TOKEN", "").strip()
        self.max_upload = int(float(os.getenv("NEWS_MAX_UPLOAD_MB", "25")) * 1_000_000)
        self.max_duration_s = float(os.getenv("NEWS_MAX_DURATION_S", "600"))
        self.default_ttl_s = int(os.getenv("NEWS_DEFAULT_TTL_S", str(6 * 3600)))
        self.max_ttl_s = 7 * 24 * 3600


def make_router(store: PlaylistStore, engine_status: Callable[[], dict], request_skip: Callable[[str], bool],
                settings: Optional[Settings] = None, probe=None,
                injection_enabled: Callable[[], bool] = lambda: True) -> APIRouter:
    cfg = settings or Settings()
    router = APIRouter(prefix="/api/playlist", tags=["playlist"])
    tts_providers: Dict[str, TtsProvider] = {}
    router.tts_providers = tts_providers          # punto de extensión: Edge TTS se registra aquí en el futuro

    async def ffprobe(path: Path) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_type",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(422, "no se pudo analizar el audio (timeout)")
        try:
            return json.loads(out or b"{}")
        except ValueError:
            return {}

    probe_fn = probe or ffprobe

    def auth(request: Request) -> None:
        if not cfg.token:
            raise HTTPException(503, "API de playlist deshabilitada: definir RADIO_API_TOKEN")
        header = request.headers.get("authorization", "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(given.strip().encode(), cfg.token.encode()):
            raise HTTPException(401, "token inválido", headers={"WWW-Authenticate": "Bearer"})

    def find(ref: str) -> dict:
        item = store.get(ref) or store.by_external(ref)
        if item is None:
            raise HTTPException(404, "ítem no encontrado")
        return item

    def view(item: dict) -> dict:
        return store.public(item)

    async def ingest(external_id: str, tmp: Path, *, sha: str, size: int, title: str, kind: str, priority: str,
                     source: str, ttl_s: int, metadata: dict) -> JSONResponse:
        """Valida el audio del archivo temporal `tmp` y lo encola. Siempre limpia `tmp` si no queda en uso."""
        try:
            info = await probe_fn(tmp)
            fmt = (info.get("format") or {})
            streams = [s.get("codec_type") for s in info.get("streams", [])]
            if "audio" not in streams:
                raise HTTPException(415, "el archivo no contiene audio reproducible")
            try:
                duration = float(fmt.get("duration") or 0)
            except ValueError:
                duration = 0.0
            if duration < 0.5:
                raise HTTPException(422, "audio vacío o demasiado corto")
            if duration > cfg.max_duration_s:
                raise HTTPException(413, f"audio demasiado largo (máx {int(cfg.max_duration_s)} s)")
            names = str(fmt.get("format_name", ""))
            ext = next((e for key, e in EXT_BY_FORMAT if key in names), None)
            if ext is None:
                raise HTTPException(415, f"formato no soportado: {names or 'desconocido'}")
            # Sin awaits desde aquí hasta add(): la comprobación de duplicados y el alta son atómicas.
            same_ext = store.by_external(external_id)
            if same_ext is not None:
                if same_ext.get("sha256") != sha:
                    raise HTTPException(409, "external_id ya existe con otro contenido")
                return JSONResponse({**view(same_ext), "duplicate": True}, status_code=200)
            same_content = store.find_duplicate_content(sha)
            if same_content is not None:
                return JSONResponse({**view(same_content), "duplicate": True, "duplicate_of": same_content["id"]},
                                    status_code=200)
            item_id = store.new_id()
            try:
                item = store.add(item_id=item_id, external_id=external_id, title=title, kind=kind,
                                 priority=priority, source=source, sha256=sha, size_bytes=size, ext=ext,
                                 duration_s=duration, expires_at=store.clock() + timedelta(seconds=ttl_s),
                                 metadata=metadata)
            except QueueFull as exc:
                raise HTTPException(429, str(exc), headers={"Retry-After": "60"})
            os.replace(tmp, store.audio_dir / item["file"])
            logger.info("Noticia encolada %s (%s, %.1fs, %s)", item_id, external_id, duration, priority)
            return JSONResponse({**view(item), "duplicate": False}, status_code=201)
        finally:
            tmp.unlink(missing_ok=True)

    @router.put("/items/{external_id}", dependencies=[Depends(auth)])
    async def put_item(external_id: str, request: Request, title: str = "", kind: str = "news",
                       priority: str = "normal", source: str = "", expires_in_s: Optional[int] = None):
        if not injection_enabled():
            raise HTTPException(503, "el reproductor activo no admite inyección de noticias (PLAYOUT_ENGINE=continuous)")
        if not EXTERNAL_ID_RE.match(external_id):
            raise HTTPException(422, "external_id inválido (1-128 de A-Z a-z 0-9 . _ : -)")
        if kind not in KINDS or priority not in PRIORITIES:
            raise HTTPException(422, f"kind ∈ {KINDS}, priority ∈ {PRIORITIES}")
        ttl = cfg.default_ttl_s if expires_in_s is None else expires_in_s
        if not 30 <= ttl <= cfg.max_ttl_s:
            raise HTTPException(422, f"expires_in_s fuera de rango (30-{cfg.max_ttl_s})")
        metadata: dict = {}
        raw_meta = request.headers.get("x-metadata")
        if raw_meta:
            try:
                metadata = json.loads(raw_meta)
                if not isinstance(metadata, dict) or len(raw_meta) > 2048:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, "X-Metadata debe ser un objeto JSON de hasta 2 KB")
        claimed = request.headers.get("x-content-sha256", "").strip().lower()
        existing = store.by_external(external_id)
        if existing is not None and claimed and existing.get("sha256") == claimed:
            return JSONResponse({**view(existing), "duplicate": True}, status_code=200)   # sin recibir el cuerpo
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cfg.max_upload:
            raise HTTPException(413, f"audio demasiado grande (máx {cfg.max_upload // 1_000_000} MB)")
        tmp = store.audio_dir / f"upload_{secrets.token_hex(6)}.part"
        digest, size = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cfg.max_upload:
                        raise HTTPException(413, f"audio demasiado grande (máx {cfg.max_upload // 1_000_000} MB)")
                    digest.update(chunk)
                    fh.write(chunk)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(422, "cuerpo vacío: enviar el audio como cuerpo de la petición")
        sha = digest.hexdigest()
        if claimed and claimed != sha:
            tmp.unlink(missing_ok=True)
            raise HTTPException(422, "X-Content-SHA256 no coincide con el contenido recibido")
        return await ingest(external_id, tmp, sha=sha, size=size, title=title[:200] or external_id, kind=kind,
                            priority=priority, source=source[:60], ttl_s=ttl, metadata=metadata)

    @router.get("/items/{ref}", dependencies=[Depends(auth)])
    async def get_item(ref: str):
        return view(find(ref))

    @router.delete("/items/{ref}", dependencies=[Depends(auth)])
    async def delete_item(ref: str):
        item = find(ref)
        if item["status"] == QUEUED:
            store.remove(item["id"])
            return view(item)
        if item["status"] == PLAYING:
            if request_skip(item["id"]):
                return JSONResponse({**view(item), "note": "corte solicitado; se aplica en el próximo bloque"},
                                    status_code=202)
            raise HTTPException(409, "el ítem está al aire pero el reproductor no puede cortarlo ahora")
        raise HTTPException(409, f"el ítem ya terminó ({item['status']})")

    @router.get("", dependencies=[Depends(auth)])
    async def get_playlist(recent: int = 20):
        store.janitor()
        return {"summary": store.summary(), "engine": engine_status(), "policy": store.policy,
                "queue": [view(i) for i in store.queued()],
                "playing": view(store.playing()) if store.playing() else None,
                "recent": [view(i) for i in store.recent(max(0, min(recent, 100)))]}

    @router.get("/policy", dependencies=[Depends(auth)])
    async def get_policy():
        return store.policy

    @router.patch("/policy", dependencies=[Depends(auth)])
    async def patch_policy(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "se esperaba un objeto JSON")
        try:
            return store.set_policy(body)
        except PolicyError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/capabilities", dependencies=[Depends(auth)])
    async def capabilities():
        return {"audio_ingest": {"formats": sorted({e for _, e in EXT_BY_FORMAT}), "max_upload_mb": cfg.max_upload // 1_000_000,
                                 "max_duration_s": cfg.max_duration_s, "idempotent": True, "dedupe": ["external_id", "sha256"]},
                "priorities": list(PRIORITIES), "kinds": list(KINDS),
                "tts": {"available": bool(tts_providers), "engines": sorted(tts_providers)},
                "playback_confirmation": "status=played tras entrar el último bloque al encoder (precisión < 2 s)"}

    @router.post("/tts", dependencies=[Depends(auth)])
    async def tts(request: Request):
        """Síntesis dentro de Radio. Hoy no hay motor registrado → 501; al registrar uno (p. ej. Edge TTS) en
        `router.tts_providers` el audio generado entra por el mismo camino de validación/encolado que el PUT."""
        if not tts_providers:
            raise HTTPException(501, "Radio no tiene TTS propio; enviar el audio ya generado con PUT /items/{id}")
        try:
            body = await request.json()
            engine = body.get("engine") or sorted(tts_providers)[0]
            text, external_id = str(body["text"]).strip(), str(body["external_id"])
            voice = str(body.get("voice", ""))
            provider = tts_providers[engine]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(422, "se esperaba {external_id, text, [voice], [engine], [title], [priority]}")
        if not text or not EXTERNAL_ID_RE.match(external_id):
            raise HTTPException(422, "texto o external_id inválidos")
        if store.by_external(external_id):
            return JSONResponse({**view(store.by_external(external_id)), "duplicate": True})
        data = await provider(text, voice)
        tmp = store.audio_dir / f"upload_{secrets.token_hex(6)}.part"
        tmp.write_bytes(data)
        return await ingest(external_id, tmp, sha=hashlib.sha256(data).hexdigest(), size=len(data),
                            title=str(body.get("title") or external_id)[:200], kind=body.get("kind", "news"),
                            priority=body.get("priority", "normal"), source=f"tts:{engine}",
                            ttl_s=cfg.default_ttl_s, metadata={"text_chars": len(text), "voice": voice})

    return router
