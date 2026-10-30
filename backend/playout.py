"""AETHERYON RADIO: reproductor continuo con inyección de noticias (playlist dinámica).

Reemplaza al AutoDJ de `ffmpeg -f concat` (lista fija leída una sola vez) por un único encoder Opus persistente
alimentado con PCM. Así se pueden insertar noticias en cualquier momento sin reiniciar la emisión: el stream
WebM sigue siendo uno solo (un init, clusters continuos) y los oyentes no tienen que re-sincronizar.

    productor ──► PCMQueue (~1,3 s) ──► consumidor ──► ffmpeg (encoder, stdin) ──► WebM ──► fan-out
    (decide qué suena y decodifica)      (ritmo y confirmaciones)

El encoder consume con `-re`, lo que da el ritmo de tiempo real; la cola acota cuánto se decide por adelantado.
La reproducción se confirma cuando el último bloque de una noticia entra al encoder (≈0,3-1,6 s antes de
sonar): precisión de confirmación < 2 s.
"""
import asyncio
import logging
import sys
import time
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from playlist import PlaylistStore

logger = logging.getLogger("aetheryon-radio.playout")

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2          # s16le estéreo
CHUNK = 8192                                           # múltiplo de 4 (una muestra estéreo)
QUEUE_CHUNKS = 30                                      # ≈1,3 s de audio decidido por adelantado
FADE_IN_S = 0.3


def ramp(data: bytes, g0: float, g1: float) -> bytes:
    """Rampa lineal de ganancia g0→g1 sobre PCM s16le (sin dependencias: `array`)."""
    a = array("h")
    a.frombytes(data)
    if sys.byteorder == "big":
        a.byteswap()
    n = len(a)
    if n:
        step = (g1 - g0) / n
        for i in range(n):
            a[i] = int(a[i] * (g0 + step * i))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def silence(ms: int) -> bytes:
    n = int(BYTES_PER_SECOND * max(0, ms) / 1000)
    return bytes(n - n % 4)


class PCMQueue:
    """Cola acotada de entradas (tipo, dato, meta). Permite desvanecer lo pendiente al interrumpir."""

    def __init__(self, maxsize: int = QUEUE_CHUNKS) -> None:
        self._dq: deque = deque()
        self._max = maxsize
        self._not_full = asyncio.Event()
        self._not_empty = asyncio.Event()
        self._not_full.set()

    def __len__(self) -> int:
        return len(self._dq)

    async def put(self, entry: tuple) -> None:
        while len(self._dq) >= self._max:
            self._not_full.clear()
            await self._not_full.wait()
        self._dq.append(entry)
        self._not_empty.set()

    async def get(self, timeout: Optional[float] = None) -> Optional[tuple]:
        while not self._dq:
            self._not_empty.clear()
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout)
            except asyncio.TimeoutError:
                return None
        entry = self._dq.popleft()
        self._not_full.set()
        return entry

    def fade_out_pending(self) -> None:
        """Desvanece a cero todo el audio pendiente (corte de una pista por una noticia urgente)."""
        pcm = [i for i, e in enumerate(self._dq) if e[0] == "pcm"]
        total = sum(len(self._dq[i][1]) for i in pcm)
        done = 0
        for i in pcm:
            kind, data, meta = self._dq[i]
            g0, g1 = 1 - done / total, 1 - (done + len(data)) / total
            self._dq[i] = (kind, ramp(data, g0, g1), meta)
            done += len(data)


@dataclass
class PlayoutConfig:
    min_music_tracks: int = 1      # pistas de música que deben sonar entre dos noticias (la prioridad alta no espera)
    pad_ms: int = 400              # silencio antes y después de cada noticia
    loudnorm: bool = True          # normaliza la sonoridad de las noticias (la música no se toca)
    restart_delay: float = 3.0
    bitrate: str = "96k"


Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


class PlayoutEngine:
    def __init__(self, store: PlaylistStore, *, load_tracks: Callable[[], List[Path]],
                 begin_stream: Callable[[str], Awaitable[str]],
                 publish: Callable[[str, object, bytes], Awaitable[None]],
                 new_framer: Callable[[], object], is_dj_live: Callable[[], bool],
                 get_session: Callable[[], Optional[str]], config: Optional[PlayoutConfig] = None,
                 spawn: Spawn = asyncio.create_subprocess_exec) -> None:
        self.store = store
        self.load_tracks = load_tracks
        self.begin_stream = begin_stream
        self.publish = publish
        self.new_framer = new_framer
        self.is_dj_live = is_dj_live
        self.get_session = get_session
        self.cfg = config or PlayoutConfig()
        self.spawn = spawn
        self.running = False
        self.restarts = 0
        self.now_playing: Optional[dict] = None
        self._buf: Optional[PCMQueue] = None
        self._skip_id: Optional[str] = None
        self._current_news: Optional[str] = None
        self._last_idle_log = 0.0

    # ── API para el resto del servidor ──────────────────────────────────
    def status(self) -> dict:
        return {"engine": "playout", "running": self.running, "restarts": self.restarts,
                "now_playing": self.now_playing, "buffered_s": round(len(self._buf) * CHUNK / BYTES_PER_SECOND, 2) if self._buf else 0.0}

    def request_skip(self, item_id: str) -> bool:
        """Corta la noticia en emisión (si es esa); devuelve False si no está sonando."""
        if self._current_news == item_id and self.running:
            self._skip_id = item_id
            return True
        return False

    # ── comandos ffmpeg ─────────────────────────────────────────────────
    def _encoder_cmd(self) -> List[str]:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "s16le", "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS), "-i", "pipe:0", "-c:a", "libopus", "-b:a", self.cfg.bitrate,
                "-application", "audio", "-cluster_time_limit", "1000", "-live", "1", "-f", "webm", "pipe:1"]

    def _decoder_cmd(self, path: Path, news: bool) -> List[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-vn",
               "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE)]
        if news and self.cfg.loudnorm:
            cmd += ["-af", "loudnorm=I=-16:LRA=11:TP=-1.5"]
        return cmd + ["-f", "s16le", "pipe:1"]

    # ── supervisor ──────────────────────────────────────────────────────
    def _has_content(self) -> bool:
        return bool(self.load_tracks()) or self.store.next_news() is not None

    async def run(self) -> None:
        while True:
            if self.is_dj_live():
                await asyncio.sleep(0.5)
                continue
            if not self._has_content():
                if time.monotonic() - self._last_idle_log > 30:
                    logger.error("Playout: sin pistas ni noticias que emitir")
                    self._last_idle_log = time.monotonic()
                await asyncio.sleep(max(self.cfg.restart_delay, 5))
                continue
            session = await self.begin_stream("autodj")
            framer = self.new_framer()
            failed, enc = True, None
            try:
                logger.info("Playout iniciado (pistas=%d, noticias en cola=%d)", len(self.load_tracks()),
                            len(self.store.queued()))
                enc = await self.spawn(*self._encoder_cmd(), stdin=asyncio.subprocess.PIPE,
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                self.running = True
                failed = await self._pump(session, framer, enc)
            except FileNotFoundError:
                logger.exception("FFmpeg no está instalado; el reproductor no puede iniciar")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("El reproductor falló; se reinicia")
            finally:
                self.running = False
                self._abort_current("reproductor detenido")
                await self._stop_proc(enc)
                self.now_playing = None
                self._buf = None
            if not self.is_dj_live():
                self.restarts += 1
                await asyncio.sleep(self.cfg.restart_delay if failed else 0)

    async def _pump(self, session: str, framer: object, enc: asyncio.subprocess.Process) -> bool:
        buf = self._buf = PCMQueue()
        tasks = [asyncio.create_task(self._producer(buf)), asyncio.create_task(self._consumer(buf, enc)),
                 asyncio.create_task(self._read_encoder(session, framer, enc)),
                 asyncio.create_task(self._watch(session))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # corte intencional (DJ en vivo / otra sesión) = no es fallo; cualquier otra salida sí
        return not (tasks[3] in done and tasks[3].exception() is None)

    async def _watch(self, session: str) -> None:
        while not self.is_dj_live() and self.get_session() == session:
            await asyncio.sleep(0.2)

    async def _read_encoder(self, session: str, framer: object, enc: asyncio.subprocess.Process) -> None:
        while True:
            chunk = await enc.stdout.read(8192)
            if not chunk:
                await enc.wait()
                return
            await self.publish(session, framer, chunk)

    async def _stop_proc(self, proc: Optional[asyncio.subprocess.Process]) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    def _abort_current(self, reason: str) -> None:
        if self._current_news:
            self.store.requeue(self._current_news, reason)
            self._current_news = None

    # ── consumidor: ritmo hacia el encoder y confirmaciones ─────────────
    async def _consumer(self, buf: PCMQueue, enc: asyncio.subprocess.Process) -> None:
        while True:
            entry = await buf.get(timeout=0.25)
            if entry is None:                       # el productor se retrasó: silencio para no cortar el stream
                enc.stdin.write(silence(100))
                await enc.stdin.drain()
                continue
            kind, data, meta = entry
            if kind == "pcm":
                enc.stdin.write(data)
                await enc.stdin.drain()
            elif kind == "start":
                self.now_playing = {"kind": meta["kind"], "title": meta["title"], "id": meta.get("id")}
                if meta.get("id"):
                    self.store.mark_playing(meta["id"])
            elif kind == "end":
                self.now_playing = None
                if meta.get("id"):
                    self._finish_news(meta)

    def _finish_news(self, meta: dict) -> None:
        item_id = meta["id"]
        self._current_news = None
        if meta["outcome"] == "played":
            self.store.mark_played(item_id)
            logger.info("Noticia emitida: %s (%s)", meta["title"], item_id)
        elif meta["outcome"] == "skipped":
            self.store.mark_skipped(item_id, "omitida durante la emisión")
        else:
            self.store.mark_failed(item_id, meta.get("error") or "error de decodificación")
            logger.error("Noticia fallida %s: %s", item_id, meta.get("error"))

    # ── productor: qué suena ────────────────────────────────────────────
    async def _producer(self, buf: PCMQueue) -> None:
        music_i = 0
        since_news = self.cfg.min_music_tracks          # permite una noticia al arrancar
        while True:
            tracks = self.load_tracks()
            gap_ok = since_news >= self.cfg.min_music_tracks or not tracks
            item = self.store.next_news(include_normal=gap_ok)
            if item is not None:
                await self._play_news(buf, item)
                since_news = 0
                continue
            if not tracks:
                await asyncio.sleep(1.0)
                continue
            track = tracks[music_i % len(tracks)]
            music_i += 1
            if await self._play_file(buf, track, kind="music", title=track.stem):
                since_news += 1

    async def _play_news(self, buf: PCMQueue, item: dict) -> None:
        path = self.store.audio_path(item)
        if path is None:
            self.store.mark_failed(item["id"], "archivo de audio no disponible")
            return
        self._current_news = item["id"]
        self._skip_id = None
        await self._play_file(buf, path, kind=item["kind"], title=item["title"] or item["external_id"],
                              item_id=item["id"])

    async def _play_file(self, buf: PCMQueue, path: Path, *, kind: str, title: str,
                         item_id: Optional[str] = None) -> bool:
        """Decodifica `path` hacia la cola. Devuelve True si se reprodujo completo."""
        news = item_id is not None
        meta = {"kind": kind, "title": title, "id": item_id}
        proc = await self.spawn(*self._decoder_cmd(path, news), stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE)
        outcome, error, produced, fade_in_left = "played", "", 0, int(FADE_IN_S * BYTES_PER_SECOND)
        carry = b""
        try:
            await buf.put(("start", None, meta))
            if news:
                await buf.put(("pcm", silence(self.cfg.pad_ms), None))
            while True:
                data = carry + await proc.stdout.read(CHUNK)
                if not data or len(data) == len(carry):       # EOF
                    break
                cut = len(data) - len(data) % 4
                data, carry = data[:cut], data[cut:]
                if not data:
                    continue
                if news and self._skip_id == item_id:
                    outcome = "skipped"
                    break
                if not news and self.store.has_high():        # noticia urgente: la música se desvanece
                    buf.fade_out_pending()
                    outcome = "interrupted"
                    break
                if news and fade_in_left > 0:
                    n = min(len(data), fade_in_left)
                    done = int(FADE_IN_S * BYTES_PER_SECOND) - fade_in_left
                    total = int(FADE_IN_S * BYTES_PER_SECOND)
                    data = ramp(data[:n], done / total, (done + n) / total) + data[n:]
                    fade_in_left -= n
                produced += len(data)
                await buf.put(("pcm", data, None))
            rc = await proc.wait() if proc.returncode is None and outcome == "played" else proc.returncode
            if outcome == "played" and (rc not in (0, None) or produced == 0):
                outcome = "failed"
                err = (await proc.stderr.read(400)).decode("utf-8", "replace").strip()
                error = err or f"ffmpeg terminó con código {rc}"
        except asyncio.CancelledError:
            raise
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if news:
            await buf.put(("pcm", silence(self.cfg.pad_ms), None))
        if outcome == "failed" and not news:
            logger.warning("Pista ignorada (no se pudo decodificar) %s: %s", path.name, error)
            await asyncio.sleep(1.0)                          # evita un bucle caliente con pistas rotas
        await buf.put(("end", None, {**meta, "outcome": outcome, "error": error}))
        return outcome == "played"
