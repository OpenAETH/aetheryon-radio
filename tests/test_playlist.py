import json
from datetime import datetime, timedelta, timezone

import pytest

from playlist import (EXPIRED, FAILED, MAX_ATTEMPTS, PLAYED, QUEUED, REMOVED, PolicyError, PlaylistStore,
                      QueueFull, validate_policy)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("NEWS_AFTER_PLAY", "NEWS_KEEP_HOURS", "NEWS_MAX_STORAGE_MB"):
        monkeypatch.delenv(k, raising=False)


def add(store, n, *, priority="normal", ttl=3600, size=10, sha=None):
    item_id = store.new_id()
    (store.audio_dir / f"{item_id}.wav").write_bytes(b"x" * size)
    return store.add(item_id=item_id, external_id=f"ext-{n}", title=f"t{n}", kind="news", priority=priority,
                     source="test", sha256=sha or f"sha{n}", size_bytes=size, ext="wav", duration_s=1.0,
                     expires_at=store.clock() + timedelta(seconds=ttl), metadata={})


def test_order_is_high_priority_then_fifo(tmp_path):
    clock = Clock()
    s = PlaylistStore(tmp_path, clock=clock)
    a = add(s, 1)
    clock.advance(seconds=1)
    b = add(s, 2)
    clock.advance(seconds=1)
    c = add(s, 3, priority="high")
    assert [i["id"] for i in s.queued()] == [c["id"], a["id"], b["id"]]
    assert s.next_news(include_normal=False)["id"] == c["id"]
    s.mark_playing(c["id"])
    s.mark_played(c["id"])
    assert s.next_news(include_normal=False) is None       # solo quedan normales
    assert s.next_news()["id"] == a["id"]


def test_played_item_is_deleted_by_default_and_leaves_record(tmp_path):
    s = PlaylistStore(tmp_path)
    it = add(s, 1)
    path = s.audio_dir / it["file"]
    s.mark_playing(it["id"])
    s.mark_played(it["id"])
    assert not path.exists()
    assert s.get(it["id"])["status"] == PLAYED and s.get(it["id"])["retained"] is False
    assert s.queued() == []


def test_keep_policy_retains_then_janitor_deletes_after_keep_hours(tmp_path):
    clock = Clock()
    s = PlaylistStore(tmp_path, clock=clock)
    s.set_policy({"after_play": "keep", "keep_hours": 2})
    it = add(s, 1)
    s.mark_playing(it["id"])
    s.mark_played(it["id"])
    assert (s.audio_dir / it["file"]).exists()
    clock.advance(hours=1)
    s.janitor()
    assert (s.audio_dir / it["file"]).exists()
    clock.advance(hours=2)
    assert s.janitor()["files_deleted"] == 1
    assert not (s.audio_dir / it["file"]).exists()


def test_unplayed_items_never_kept_even_with_keep_policy(tmp_path):
    s = PlaylistStore(tmp_path)
    s.set_policy({"after_play": "keep"})
    it = add(s, 1)
    s.remove(it["id"])
    assert s.get(it["id"])["status"] == REMOVED and not (s.audio_dir / it["file"]).exists()


def test_switching_policy_to_delete_purges_retained_files(tmp_path):
    s = PlaylistStore(tmp_path)
    s.set_policy({"after_play": "keep"})
    it = add(s, 1)
    s.mark_playing(it["id"])
    s.mark_played(it["id"])
    s.set_policy({"after_play": "delete"})
    assert not (s.audio_dir / it["file"]).exists()


def test_storage_cap_frees_oldest_finished_first(tmp_path):
    clock = Clock()
    s = PlaylistStore(tmp_path, clock=clock)
    s.set_policy({"after_play": "keep", "max_storage_mb": 1})
    ids = []
    for n in range(3):
        it = add(s, n, size=600_000)
        s.mark_playing(it["id"])
        s.mark_played(it["id"])
        ids.append(it["id"])
        clock.advance(minutes=1)
    s.janitor()
    assert s.storage_bytes() <= 1_000_000
    assert s.get(ids[0])["retained"] is False and s.get(ids[2])["retained"] is True


def test_expiry_discards_stale_items_and_their_files(tmp_path):
    clock = Clock()
    s = PlaylistStore(tmp_path, clock=clock)
    it = add(s, 1, ttl=60)
    clock.advance(seconds=61)
    assert s.next_news() is None
    assert s.get(it["id"])["status"] == EXPIRED and not (s.audio_dir / it["file"]).exists()


def test_queue_cap_raises(tmp_path):
    s = PlaylistStore(tmp_path, max_queue=2)
    add(s, 1)
    add(s, 2)
    with pytest.raises(QueueFull):
        add(s, 3)


def test_requeue_until_max_attempts_then_fail(tmp_path):
    s = PlaylistStore(tmp_path)
    it = add(s, 1)
    for n in range(MAX_ATTEMPTS):
        s.mark_playing(it["id"])
        s.requeue(it["id"], "dj en vivo")
        expected = QUEUED if n < MAX_ATTEMPTS - 1 else FAILED
        assert s.get(it["id"])["status"] == expected


def test_crash_recovery_requeues_playing_and_fails_missing_files(tmp_path):
    s = PlaylistStore(tmp_path)
    a, b = add(s, 1), add(s, 2)
    s.mark_playing(a["id"])
    (s.audio_dir / b["file"]).unlink()
    s2 = PlaylistStore(tmp_path)                       # "reinicio"
    assert s2.get(a["id"])["status"] == QUEUED and s2.get(a["id"])["attempts"] == 1
    assert s2.get(b["id"])["status"] == FAILED


def test_duplicate_detection_by_content_and_window(tmp_path):
    clock = Clock()
    s = PlaylistStore(tmp_path, clock=clock)
    it = add(s, 1, sha="abc")
    assert s.find_duplicate_content("abc")["id"] == it["id"]
    s.mark_playing(it["id"])
    s.mark_played(it["id"])
    assert s.find_duplicate_content("abc") is not None          # recién emitido: sigue siendo duplicado
    clock.advance(hours=49)
    assert s.find_duplicate_content("abc") is None              # fuera de ventana


def test_corrupt_index_is_preserved_and_store_starts_clean(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "index.json").write_text("{no json")
    s = PlaylistStore(tmp_path)
    assert s.items == {} and (tmp_path / "index.json.bad").exists()


def test_orphan_old_files_are_cleaned(tmp_path):
    import os, time
    (tmp_path / "audio").mkdir(parents=True)
    orphan = tmp_path / "audio" / "huerfano.wav"
    orphan.write_bytes(b"x")
    os.utime(orphan, (time.time() - 7200,) * 2)
    PlaylistStore(tmp_path)
    assert not orphan.exists()


def test_policy_validation():
    cur = {"after_play": "delete", "keep_hours": 24, "max_storage_mb": 200}
    assert validate_policy({"keep_hours": "5"}, cur)["keep_hours"] == 5
    for bad in ({"after_play": "x"}, {"keep_hours": -1}, {"max_storage_mb": 0}, {"nope": 1}, {"keep_hours": "a"}):
        with pytest.raises(PolicyError):
            validate_policy(bad, cur)


def test_policy_defaults_from_env_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_AFTER_PLAY", "keep")
    s = PlaylistStore(tmp_path)
    assert s.policy["after_play"] == "keep"
    s.set_policy({"keep_hours": 3})
    assert PlaylistStore(tmp_path).policy["keep_hours"] == 3
    assert json.loads((tmp_path / "index.json").read_text())["policy"]["keep_hours"] == 3
