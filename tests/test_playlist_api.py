import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import needs_ffmpeg
from playlist import PlaylistStore
from playlist_api import Settings, make_router

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def build(tmp_path, token=TOKEN, skip=lambda i: False, **env):
    cfg = Settings()
    cfg.token = token
    for k, v in env.items():
        setattr(cfg, k, v)
    store = PlaylistStore(tmp_path / "news", max_queue=3)
    app = FastAPI()
    router = make_router(store, lambda: {"engine": "fake"}, skip, cfg)
    app.include_router(router)
    return TestClient(app), store, router


def wav(tone, name="a.wav", seconds=1.0, freq=440):
    return tone(name, seconds, freq).read_bytes()


pytestmark = needs_ffmpeg


def test_auth_required_and_disabled_without_token(tmp_path, tone):
    c, _, _ = build(tmp_path)
    assert c.get("/api/playlist").status_code == 401
    assert c.get("/api/playlist", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.put("/api/playlist/items/x", content=b"1").status_code == 401
    c2, _, _ = build(tmp_path / "b", token="")
    assert c2.get("/api/playlist", headers=AUTH).status_code == 503


def test_upload_enqueue_and_status(tmp_path, tone):
    c, store, _ = build(tmp_path)
    data = wav(tone)
    r = c.put("/api/playlist/items/tg-1?title=Titular&priority=high&source=terragazette", content=data,
              headers={**AUTH, "X-Metadata": '{"sector":"tecnologia"}'})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued" and body["position"] == 1 and body["priority"] == "high"
    assert body["sha256"] == hashlib.sha256(data).hexdigest() and body["duplicate"] is False
    assert "file" not in body and body["metadata"] == {"sector": "tecnologia"}
    assert (store.audio_dir / f"{body['id']}.wav").is_file()
    assert c.get(f"/api/playlist/items/{body['id']}", headers=AUTH).json()["status"] == "queued"
    assert c.get("/api/playlist/items/tg-1", headers=AUTH).json()["id"] == body["id"]     # por external_id
    listing = c.get("/api/playlist", headers=AUTH).json()
    assert [i["id"] for i in listing["queue"]] == [body["id"]] and listing["engine"] == {"engine": "fake"}


def test_idempotent_by_external_id_and_by_content(tmp_path, tone):
    c, store, _ = build(tmp_path)
    data = wav(tone)
    first = c.put("/api/playlist/items/a", content=data, headers=AUTH).json()
    again = c.put("/api/playlist/items/a", content=data, headers=AUTH)
    assert again.status_code == 200 and again.json()["id"] == first["id"] and again.json()["duplicate"] is True
    other = c.put("/api/playlist/items/b", content=data, headers=AUTH)             # mismo audio, otro id
    assert other.status_code == 200 and other.json()["duplicate_of"] == first["id"]
    clash = c.put("/api/playlist/items/a", content=wav(tone, "z.wav", freq=880), headers=AUTH)
    assert clash.status_code == 409
    assert len(store.queued()) == 1


def test_sha_header_early_dedupe_and_mismatch(tmp_path, tone):
    c, _, _ = build(tmp_path)
    data = wav(tone)
    sha = hashlib.sha256(data).hexdigest()
    c.put("/api/playlist/items/a", content=data, headers=AUTH)
    early = c.put("/api/playlist/items/a", content=b"", headers={**AUTH, "X-Content-SHA256": sha})
    assert early.status_code == 200 and early.json()["duplicate"] is True          # no hizo falta el cuerpo
    bad = c.put("/api/playlist/items/new", content=data, headers={**AUTH, "X-Content-SHA256": "0" * 64})
    assert bad.status_code == 422


def test_validation_errors(tmp_path, tone):
    c, store, _ = build(tmp_path, max_upload=2_000, max_duration_s=5)
    ok = wav(tone)
    assert c.put("/api/playlist/items/a%20b", content=ok, headers=AUTH).status_code == 422
    assert c.put("/api/playlist/items/x?kind=video", content=ok, headers=AUTH).status_code == 422
    assert c.put("/api/playlist/items/x?priority=urgent", content=ok, headers=AUTH).status_code == 422
    assert c.put("/api/playlist/items/x?expires_in_s=5", content=ok, headers=AUTH).status_code == 422
    assert c.put("/api/playlist/items/x", content=b"", headers=AUTH).status_code == 422
    assert c.put("/api/playlist/items/x", content=b"no es audio" * 10, headers=AUTH).status_code in (415, 422)
    assert c.put("/api/playlist/items/x", content=b"A" * 5_000, headers=AUTH).status_code == 413
    assert c.put("/api/playlist/items/x", content=ok, headers={**AUTH, "X-Metadata": "[1]"}).status_code == 422
    assert store.queued() == []
    assert list(store.audio_dir.iterdir()) == []                                   # no quedan temporales


def test_too_long_audio_rejected(tmp_path, tone):
    c, store, _ = build(tmp_path, max_duration_s=1.5)
    assert c.put("/api/playlist/items/long", content=wav(tone, seconds=3), headers=AUTH).status_code == 413
    assert list(store.audio_dir.iterdir()) == []


def test_queue_full_returns_429_and_cleans_temp(tmp_path, tone):
    c, store, _ = build(tmp_path)
    for n in range(3):
        assert c.put(f"/api/playlist/items/q{n}", content=wav(tone, f"{n}.wav", freq=300 + n * 50), headers=AUTH).status_code == 201
    r = c.put("/api/playlist/items/q9", content=wav(tone, "9.wav", freq=900), headers=AUTH)
    assert r.status_code == 429 and r.headers["retry-after"]
    assert len(list(store.audio_dir.iterdir())) == 3


def test_delete_queued_playing_and_finished(tmp_path, tone):
    skipped = []
    c, store, _ = build(tmp_path, skip=lambda i: skipped.append(i) or True)
    a = c.put("/api/playlist/items/a", content=wav(tone), headers=AUTH).json()
    assert c.delete(f"/api/playlist/items/{a['id']}", headers=AUTH).json()["status"] == "removed"
    assert not (store.audio_dir / f"{a['id']}.wav").exists()
    assert c.delete(f"/api/playlist/items/{a['id']}", headers=AUTH).status_code == 409
    b = c.put("/api/playlist/items/b", content=wav(tone, freq=500), headers=AUTH).json()
    store.mark_playing(b["id"])
    assert c.delete("/api/playlist/items/b", headers=AUTH).status_code == 202 and skipped == [b["id"]]
    assert c.get("/api/playlist/items/nope", headers=AUTH).status_code == 404


def test_policy_endpoints(tmp_path):
    c, _, _ = build(tmp_path)
    assert c.get("/api/playlist/policy", headers=AUTH).json()["after_play"] == "delete"
    r = c.patch("/api/playlist/policy", json={"after_play": "keep", "keep_hours": 6}, headers=AUTH)
    assert r.status_code == 200 and r.json()["after_play"] == "keep" and r.json()["keep_hours"] == 6
    assert c.patch("/api/playlist/policy", json={"after_play": "nunca"}, headers=AUTH).status_code == 422
    assert c.patch("/api/playlist/policy", content=b"[1]", headers=AUTH).status_code == 422


def test_capabilities_and_tts_extension_point(tmp_path, tone):
    c, store, router = build(tmp_path)
    caps = c.get("/api/playlist/capabilities", headers=AUTH).json()
    assert caps["tts"] == {"available": False, "engines": []} and "wav" in caps["audio_ingest"]["formats"]
    assert c.post("/api/playlist/tts", json={"text": "hola", "external_id": "t1"}, headers=AUTH).status_code == 501
    audio = wav(tone, "tts.wav", freq=700)

    async def fake_engine(text, voice):
        assert (text, voice) == ("hola mundo", "es-AR")
        return audio

    router.tts_providers["fake"] = fake_engine
    assert c.get("/api/playlist/capabilities", headers=AUTH).json()["tts"]["engines"] == ["fake"]
    r = c.post("/api/playlist/tts", json={"text": "hola mundo", "external_id": "t1", "voice": "es-AR"}, headers=AUTH)
    assert r.status_code == 201 and r.json()["source"] == "tts:fake" and store.by_external("t1")
    assert c.post("/api/playlist/tts", json={"text": "hola mundo", "external_id": "t1"}, headers=AUTH).json()["duplicate"] is True
    assert c.post("/api/playlist/tts", json={"text": ""}, headers=AUTH).status_code == 422


def test_injection_rejected_when_player_cannot_play_it(tmp_path, tone):
    cfg = Settings()
    cfg.token = TOKEN
    store = PlaylistStore(tmp_path / "news")
    app = FastAPI()
    app.include_router(make_router(store, lambda: {}, lambda i: False, cfg, injection_enabled=lambda: False))
    r = TestClient(app).put("/api/playlist/items/a", content=wav(tone), headers=AUTH)
    assert r.status_code == 503 and store.queued() == []
