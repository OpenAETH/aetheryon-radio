"""Reproductor continuo con ffmpeg real: el stream WebM resultante se decodifica y se verifican las señales."""
import asyncio
import subprocess
import time
from datetime import timedelta


from conftest import make_audio, needs_ffmpeg
from main import WebMFramer
from playlist import FAILED, PLAYED, QUEUED, SKIPPED, PlaylistStore
from playout import PCMQueue, PlayoutConfig, PlayoutEngine, ramp, silence

pytestmark = needs_ffmpeg


class Rig:
    """Arnés: engine real + fan-out simulado que captura el WebM emitido."""

    def __init__(self, tmp_path, tracks, **cfg):
        self.store = PlaylistStore(tmp_path / "news")
        self.tracks = tracks
        self.dj = False
        self.session = None
        self.sessions = []
        self.out = bytearray()
        self.inits = 0
        self.engine = PlayoutEngine(
            self.store, load_tracks=lambda: self.tracks, begin_stream=self._begin, publish=self._publish,
            new_framer=WebMFramer, is_dj_live=lambda: self.dj, get_session=lambda: self.session,
            config=PlayoutConfig(restart_delay=0.2, pad_ms=100, **cfg))

    async def _begin(self, source):
        self.session = f"s{len(self.sessions)}"
        self.sessions.append(source)
        return self.session

    async def _publish(self, session, framer, data):
        if session != self.session:
            return
        init, media = framer.feed(data)
        if init is not None:
            self.inits += 1
            self.out += init
        self.out += media

    def enqueue(self, path, n, priority="normal", ttl=600):
        item_id = self.store.new_id()
        dest = self.store.audio_dir / f"{item_id}.{path.suffix[1:]}"
        dest.write_bytes(path.read_bytes())
        return self.store.add(item_id=item_id, external_id=f"e{n}", title=f"noticia {n}", kind="news",
                              priority=priority, source="t", sha256=f"h{n}", size_bytes=dest.stat().st_size,
                              ext=path.suffix[1:], duration_s=1.0,
                              expires_at=self.store.clock() + timedelta(seconds=ttl), metadata={})

    async def wait_for(self, cond, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return False

    def decode(self):
        """Decodifica lo emitido a PCM mono 16 kHz (verifica que el stream es WebM/Opus válido)."""
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
                            "-f", "s16le", "pipe:1"], input=bytes(self.out), capture_output=True)
        assert r.returncode == 0, r.stderr.decode()
        return r.stdout


def run(coro):
    return asyncio.run(coro)


def test_pcm_helpers():
    a = (b"\x00\x40" * 8)                                  # muestras constantes 16384
    out = ramp(a, 1.0, 0.0)
    first = int.from_bytes(out[:2], "little", signed=True)
    last = int.from_bytes(out[-2:], "little", signed=True)
    assert first == 16384 and 0 <= last < 3000
    assert silence(10) == bytes(len(silence(10))) and len(silence(10)) % 4 == 0


def test_pcmqueue_bounded_and_fade_out():
    async def go():
        q = PCMQueue(maxsize=2)
        await q.put(("pcm", b"\x00\x40" * 100, None))
        await q.put(("pcm", b"\x00\x40" * 100, None))
        blocked = asyncio.create_task(q.put(("end", None, {})))
        await asyncio.sleep(0.05)
        assert not blocked.done()                          # acotada: el productor espera
        q.fade_out_pending()
        e1 = await q.get()
        await blocked
        assert int.from_bytes(e1[1][:2], "little", signed=True) == 16384
        e2 = await q.get()
        assert int.from_bytes(e2[1][-2:], "little", signed=True) < 400
        assert await PCMQueue().get(timeout=0.05) is None
    run(go())


def test_music_plays_continuously_with_single_stream_session(tmp_path):
    t1 = make_audio(tmp_path / "m1.wav", 1.0, 300)
    t2 = make_audio(tmp_path / "m2.wav", 1.0, 500)

    async def go():
        rig = Rig(tmp_path, [t1, t2])
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: len(rig.out) > 30_000 and rig.engine.now_playing and rig.engine.now_playing["title"] == "m2")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig
    rig = run(go())
    assert rig.sessions == ["autodj"] and rig.inits == 1   # un solo init: el stream nunca se reinicia
    assert len(rig.decode()) > 16000 * 2 * 1.2             # >1,2 s de audio decodificable


def test_news_injected_into_running_stream_without_restart(tmp_path):
    music = make_audio(tmp_path / "m.wav", 3.0, 300)
    news = make_audio(tmp_path / "n.wav", 1.0, 1000)

    async def go():
        rig = Rig(tmp_path, [music])
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.engine.running and rig.engine.now_playing)
        restarts, session = rig.engine.restarts, rig.session
        item = rig.enqueue(news, 1)                        # llega con la radio ya al aire
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == PLAYED, 25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig, item, restarts, session
    rig, item, restarts, session = run(go())
    rec = rig.store.get(item["id"])
    assert rec["status"] == PLAYED and rec["started_at"] and rec["finished_at"]
    assert rig.session == session and rig.engine.restarts == restarts and rig.inits == 1
    assert not (rig.store.audio_dir / f"{item['id']}.wav").exists()          # política por defecto: borrar
    assert len(rig.decode()) > 0


def test_news_tone_actually_present_in_output(tmp_path):
    """Confirmación efectiva: la frecuencia de la noticia (2 kHz) aparece en el audio emitido."""
    music = make_audio(tmp_path / "m.wav", 1.0, 200)
    news = make_audio(tmp_path / "n.wav", 1.5, 2000)

    async def go():
        rig = Rig(tmp_path, [music])
        item = rig.enqueue(news, 1)
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == PLAYED, 25)
        await asyncio.sleep(3.5)                           # la música vuelve y sale del encoder
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig
    rig = run(go())
    pcm = rig.decode()
    from array import array
    samples = array("h")
    samples.frombytes(pcm)
    # cruces por cero ≈ 2*f*t: busca una ventana de 0,25 s cercana a 2 kHz y otra cercana a 200 Hz
    win = 4000
    rates = []
    for i in range(0, len(samples) - win, win):
        seg = samples[i:i + win]
        z = sum(1 for a, b in zip(seg, seg[1:]) if (a < 0) != (b < 0))
        rates.append(z / 2 / (win / 16000))
    assert any(1700 < r < 2300 for r in rates) and any(150 < r < 260 for r in rates)


def test_high_priority_interrupts_music_normal_waits_for_track_end(tmp_path):
    music = make_audio(tmp_path / "m.wav", 8.0, 300)
    news = make_audio(tmp_path / "n.wav", 0.8, 1200)

    async def go():
        rig = Rig(tmp_path, [music], min_music_tracks=1)
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.engine.now_playing and rig.engine.now_playing["kind"] == "music")
        # la noticia normal inicial puede salir antes de la primera pista; esperamos a que suene música
        t0 = time.monotonic()
        normal = rig.enqueue(news, 1)
        await asyncio.sleep(2.0)
        assert rig.store.get(normal["id"])["status"] == QUEUED       # normal: espera al final de la pista de 8 s
        high = rig.enqueue(make_audio(tmp_path / "h.wav", 0.8, 1500), 2, priority="high")
        assert await rig.wait_for(lambda: rig.store.get(high["id"])["status"] == PLAYED, 10)
        elapsed = time.monotonic() - t0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig, normal, high, elapsed
    rig, normal, high, elapsed = run(go())
    assert elapsed < 7.5                                    # no esperó los 8 s de la pista


def test_dj_takeover_requeues_news_and_resumes_after(tmp_path):
    music = make_audio(tmp_path / "m.wav", 4.0, 300)
    news = make_audio(tmp_path / "n.wav", 6.0, 1000)

    async def go():
        rig = Rig(tmp_path, [music])
        item = rig.enqueue(news, 1, priority="high")
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == "playing")
        rig.dj = True                                       # entra un DJ en vivo a mitad de la noticia
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == QUEUED, 10)
        assert rig.store.get(item["id"])["attempts"] == 1   # no se pierde ni se da por emitida
        await asyncio.sleep(0.5)
        assert rig.engine.running is False
        rig.dj = False                                      # el DJ se va: vuelve el reproductor y emite la noticia
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == PLAYED, 30)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig
    rig = run(go())
    assert rig.sessions == ["autodj", "autodj"]


def test_skip_current_news(tmp_path):
    music = make_audio(tmp_path / "m.wav", 2.0, 300)
    news = make_audio(tmp_path / "n.wav", 8.0, 1000)

    async def go():
        rig = Rig(tmp_path, [music])
        item = rig.enqueue(news, 1, priority="high")
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == "playing")
        assert rig.engine.request_skip(item["id"]) is True
        assert rig.engine.request_skip("otro") is False
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == SKIPPED, 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig
    run(go())


def test_corrupt_news_fails_without_stopping_the_radio(tmp_path):
    music = make_audio(tmp_path / "m.wav", 1.0, 300)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF....no es audio" * 20)

    async def go():
        rig = Rig(tmp_path, [music])
        item = rig.enqueue(bad, 1, priority="high")
        ok = rig.enqueue(make_audio(tmp_path / "ok.wav", 0.8, 900), 2, priority="high")
        task = asyncio.create_task(rig.engine.run())
        # Ambas deben llegar a un estado final (el orden entre ellas no importa para lo que se prueba).
        assert await rig.wait_for(lambda: rig.store.get(ok["id"])["status"] == PLAYED
                                  and rig.store.get(item["id"])["status"] == FAILED, 25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig, item
    rig, item = run(go())
    assert rig.store.get(item["id"])["status"] == FAILED and rig.store.get(item["id"])["error"]
    assert rig.engine.restarts == 0


def test_news_only_radio_without_music(tmp_path):
    news = make_audio(tmp_path / "n.wav", 0.8, 1000)

    async def go():
        rig = Rig(tmp_path, [])
        task = asyncio.create_task(rig.engine.run())
        await asyncio.sleep(0.3)
        assert rig.engine.running is False                  # sin nada que emitir no arranca
        rig.engine.cfg.restart_delay = 0.1
        item = rig.enqueue(news, 1)
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == PLAYED, 40)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(go())


def test_keep_policy_retains_played_file(tmp_path):
    music = make_audio(tmp_path / "m.wav", 1.0, 300)
    news = make_audio(tmp_path / "n.wav", 0.8, 1000)

    async def go():
        rig = Rig(tmp_path, [music])
        rig.store.set_policy({"after_play": "keep", "keep_hours": 1})
        item = rig.enqueue(news, 1)
        task = asyncio.create_task(rig.engine.run())
        assert await rig.wait_for(lambda: rig.store.get(item["id"])["status"] == PLAYED, 25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rig, item
    rig, item = run(go())
    assert (rig.store.audio_dir / f"{item['id']}.wav").exists()
