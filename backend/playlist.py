"""AETHERYON RADIO: cola de noticias (playlist dinámica) con persistencia y política de retención.

Un servicio externo (p. ej. TerraGazette) envía audios ya generados; este módulo los registra, evita
duplicados, decide cuál sigue, lleva el estado de reproducción y aplica la política de conservación de
los archivos. No sabe nada de ffmpeg ni de WebSockets: el reproductor (`playout.py`) lo consulta.

Estados: queued → playing → played | skipped | failed ; queued → expired | removed.
"""
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("aetheryon-radio.playlist")

QUEUED, PLAYING, PLAYED, SKIPPED, EXPIRED, FAILED, REMOVED = (
    "queued", "playing", "played", "skipped", "expired", "failed", "removed")
TERMINAL = {PLAYED, SKIPPED, EXPIRED, FAILED, REMOVED}
KINDS = ("news", "spot")
PRIORITIES = ("normal", "high")
MAX_ATTEMPTS = 3           # reintentos de un ítem cortado (p. ej. por un DJ en vivo) antes de darlo por fallido
MAX_RECORDS = 500          # registros terminados que se conservan en el índice (sin archivo, solo estado)
DUP_WINDOW_S = 48 * 3600   # ventana en la que un mismo contenido (sha256) se considera duplicado


class QueueFull(Exception):
    pass


class PolicyError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def default_policy() -> dict:
    after = os.getenv("NEWS_AFTER_PLAY", "delete").strip().lower()
    return {"after_play": after if after in ("delete", "keep") else "delete",
            "keep_hours": max(0, _env_int("NEWS_KEEP_HOURS", 24)),
            "max_storage_mb": max(1, _env_int("NEWS_MAX_STORAGE_MB", 200))}


def validate_policy(patch: dict, current: dict) -> dict:
    out = dict(current)
    if "after_play" in patch:
        if patch["after_play"] not in ("delete", "keep"):
            raise PolicyError("after_play debe ser 'delete' o 'keep'")
        out["after_play"] = patch["after_play"]
    for key, lo, hi in (("keep_hours", 0, 24 * 90), ("max_storage_mb", 1, 100_000)):
        if key in patch:
            try:
                val = int(patch[key])
            except (TypeError, ValueError):
                raise PolicyError(f"{key} debe ser un entero")
            if not lo <= val <= hi:
                raise PolicyError(f"{key} fuera de rango ({lo}-{hi})")
            out[key] = val
    unknown = set(patch) - {"after_play", "keep_hours", "max_storage_mb"}
    if unknown:
        raise PolicyError(f"campos desconocidos: {', '.join(sorted(unknown))}")
    return out


class PlaylistStore:
    def __init__(self, root: Path, *, max_queue: int = 50, clock: Callable[[], datetime] = utcnow,
                 autoload: bool = True) -> None:
        self.root = Path(root)
        self.audio_dir = self.root / "audio"
        self.index_path = self.root / "index.json"
        self.max_queue = max_queue
        self.clock = clock
        self.items: Dict[str, dict] = {}
        self.policy = default_policy()
        if autoload:
            self.load()

    # ── persistencia ────────────────────────────────────────────────────
    def load(self) -> None:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self.index_path.is_file():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                logger.error("index.json ilegible: se conserva como .bad y se arranca limpio")
                try:
                    os.replace(self.index_path, self.index_path.with_suffix(".json.bad"))
                except OSError:
                    pass
        self.items = {i["id"]: i for i in data.get("items", []) if isinstance(i, dict) and "id" in i}
        if isinstance(data.get("policy"), dict):
            try:
                self.policy = validate_policy(data["policy"], self.policy)
            except PolicyError:
                pass
        recovered = 0
        for item in self.items.values():
            if item["status"] == PLAYING:          # el proceso cayó a mitad de emisión: vuelve a la cola
                item["status"] = QUEUED
                item["attempts"] = item.get("attempts", 0) + 1
                item["started_at"] = ""
                recovered += 1
            if item["status"] == QUEUED and not self._file(item).is_file():
                self._finish(item, FAILED, "archivo de audio ausente al reiniciar", save=False)
        known = {i["file"] for i in self.items.values() if i.get("retained") and i.get("file")}
        for f in self.audio_dir.iterdir():         # huérfanos y subidas incompletas
            if f.is_file() and f.name not in known and time.time() - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)
        if recovered:
            logger.warning("Playlist: %d ítem(s) en emisión recuperados a la cola tras un reinicio", recovered)
        self.save()

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"items": list(self.items.values()), "policy": self.policy},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _file(self, item: dict) -> Path:
        return self.audio_dir / Path(item.get("file") or "_").name

    def audio_path(self, item: dict) -> Optional[Path]:
        p = self._file(item)
        return p if item.get("retained") and p.is_file() else None

    def new_id(self) -> str:
        while True:
            i = "n_" + secrets.token_hex(6)
            if i not in self.items:
                return i

    # ── consulta ────────────────────────────────────────────────────────
    def get(self, item_id: str) -> Optional[dict]:
        return self.items.get(item_id)

    def by_external(self, external_id: str) -> Optional[dict]:
        for item in self.items.values():
            if item["external_id"] == external_id:
                return item
        return None

    def find_duplicate_content(self, sha256: str) -> Optional[dict]:
        cutoff = self.clock() - timedelta(seconds=DUP_WINDOW_S)
        for item in self.items.values():
            if item.get("sha256") == sha256 and item["status"] not in (FAILED, REMOVED, EXPIRED):
                created = parse_iso(item["created_at"])
                if item["status"] in (QUEUED, PLAYING) or (created and created >= cutoff):
                    return item
        return None

    def queued(self) -> List[dict]:
        q = [i for i in self.items.values() if i["status"] == QUEUED]
        return sorted(q, key=lambda i: (0 if i["priority"] == "high" else 1, i["created_at"], i["id"]))

    def playing(self) -> Optional[dict]:
        return next((i for i in self.items.values() if i["status"] == PLAYING), None)

    def recent(self, limit: int = 20) -> List[dict]:
        done = [i for i in self.items.values() if i["status"] in TERMINAL]
        return sorted(done, key=lambda i: i.get("finished_at") or i["created_at"], reverse=True)[:limit]

    def position(self, item: dict) -> int:
        q = self.queued()
        return next((n + 1 for n, i in enumerate(q) if i["id"] == item["id"]), 0)

    def storage_bytes(self) -> int:
        return sum(i.get("size_bytes", 0) for i in self.items.values() if i.get("retained"))

    def public(self, item: dict) -> dict:
        out = {k: v for k, v in item.items() if k != "file"}
        out["position"] = self.position(item) if item["status"] == QUEUED else 0
        return out

    def summary(self) -> dict:
        counts: Dict[str, int] = {}
        for i in self.items.values():
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        now = self.playing()
        return {"queued": counts.get(QUEUED, 0), "counts": counts, "storage_mb": round(self.storage_bytes() / 1e6, 2),
                "now_playing": ({"id": now["id"], "title": now["title"], "kind": now["kind"]} if now else None)}

    # ── alta ────────────────────────────────────────────────────────────
    def add(self, *, item_id: str, external_id: str, title: str, kind: str, priority: str, source: str,
            sha256: str, size_bytes: int, ext: str, duration_s: float, expires_at: datetime,
            metadata: dict) -> dict:
        if len(self.queued()) >= self.max_queue:
            raise QueueFull(f"cola llena ({self.max_queue})")
        item = {"id": item_id, "external_id": external_id, "title": title, "kind": kind, "priority": priority,
                "status": QUEUED, "source": source, "sha256": sha256, "size_bytes": size_bytes, "ext": ext,
                "file": f"{item_id}.{ext}", "retained": True, "duration_s": round(duration_s, 2),
                "created_at": iso(self.clock()), "expires_at": iso(expires_at), "started_at": "",
                "finished_at": "", "attempts": 0, "error": "", "metadata": metadata}
        self.items[item_id] = item
        self.save()
        return item

    # ── selección y transiciones ────────────────────────────────────────
    def next_news(self, *, include_normal: bool = True) -> Optional[dict]:
        """Siguiente ítem a emitir (alta prioridad primero). Los vencidos se descartan al pasar."""
        self.expire_stale()
        for item in self.queued():
            if item["priority"] == "high" or include_normal:
                return item
        return None

    def has_high(self) -> bool:
        self.expire_stale()
        return any(i["priority"] == "high" for i in self.queued())

    def expire_stale(self) -> int:
        now, n = self.clock(), 0
        for item in list(self.queued()):
            exp = parse_iso(item["expires_at"])
            if exp is not None and exp <= now:
                self._finish(item, EXPIRED, "vencido antes de emitirse", save=False)
                n += 1
        if n:
            self.save()
        return n

    def mark_playing(self, item_id: str) -> None:
        item = self.items.get(item_id)
        if item and item["status"] == QUEUED:
            item["status"] = PLAYING
            item["started_at"] = iso(self.clock())
            self.save()

    def mark_played(self, item_id: str) -> None:
        item = self.items.get(item_id)
        if item and item["status"] == PLAYING:
            self._finish(item, PLAYED, "")

    def mark_skipped(self, item_id: str, reason: str = "omitido") -> Optional[dict]:
        item = self.items.get(item_id)
        if item and item["status"] in (QUEUED, PLAYING):
            self._finish(item, SKIPPED, reason)
        return item

    def mark_failed(self, item_id: str, error: str) -> None:
        item = self.items.get(item_id)
        if item and item["status"] in (QUEUED, PLAYING):
            self._finish(item, FAILED, error[:300])

    def remove(self, item_id: str) -> Optional[dict]:
        item = self.items.get(item_id)
        if item and item["status"] == QUEUED:
            self._finish(item, REMOVED, "retirado por el cliente")
        return item

    def requeue(self, item_id: str, reason: str = "") -> None:
        """Un ítem cortado a mitad (DJ en vivo, caída del encoder) vuelve a la cola si quedan intentos."""
        item = self.items.get(item_id)
        if not item or item["status"] != PLAYING:
            return
        item["attempts"] = item.get("attempts", 0) + 1
        if item["attempts"] >= MAX_ATTEMPTS:
            self._finish(item, FAILED, f"interrumpido {item['attempts']} veces ({reason})".strip())
            return
        item["status"] = QUEUED
        item["started_at"] = ""
        item["error"] = reason
        self.save()

    def _finish(self, item: dict, status: str, error: str, *, save: bool = True) -> None:
        item["status"] = status
        item["finished_at"] = iso(self.clock())
        item["error"] = error
        # Política de conservación: solo lo emitido puede conservarse; lo demás nunca fue al aire y se borra.
        if not (status == PLAYED and self.policy["after_play"] == "keep"):
            self._delete_file(item)
        if save:
            self.save()

    def _delete_file(self, item: dict) -> None:
        if item.get("retained"):
            self._file(item).unlink(missing_ok=True)
            item["retained"] = False

    # ── política ────────────────────────────────────────────────────────
    def set_policy(self, patch: dict) -> dict:
        self.policy = validate_policy(patch, self.policy)
        self.janitor()
        self.save()
        return dict(self.policy)

    def janitor(self) -> dict:
        """Vence lo que pasó de hora, aplica la retención de lo emitido y recorta el índice."""
        expired = self.expire_stale()
        now = self.clock()
        deleted = 0
        if self.policy["after_play"] == "delete":
            for item in self.items.values():
                if item["status"] in TERMINAL and item.get("retained"):
                    self._delete_file(item)
                    deleted += 1
        else:
            hours = self.policy["keep_hours"]
            for item in self.items.values():
                fin = parse_iso(item.get("finished_at") or "")
                if item.get("retained") and item["status"] in TERMINAL and hours and fin and now - fin > timedelta(hours=hours):
                    self._delete_file(item)
                    deleted += 1
        limit = self.policy["max_storage_mb"] * 1_000_000
        if self.storage_bytes() > limit:                 # sobre el tope: se libera lo más viejo ya terminado
            for item in sorted((i for i in self.items.values() if i.get("retained") and i["status"] in TERMINAL),
                               key=lambda i: i.get("finished_at") or ""):
                if self.storage_bytes() <= limit:
                    break
                self._delete_file(item)
                deleted += 1
        finished = sorted((i for i in self.items.values() if i["status"] in TERMINAL),
                          key=lambda i: i.get("finished_at") or i["created_at"])
        pruned = 0
        for item in finished[: max(0, len(finished) - MAX_RECORDS)]:
            self._delete_file(item)
            del self.items[item["id"]]
            pruned += 1
        if expired or deleted or pruned:
            self.save()
        return {"expired": expired, "files_deleted": deleted, "records_pruned": pruned}
