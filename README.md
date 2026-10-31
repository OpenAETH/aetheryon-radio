# AETHERYON RADIO

Radio online autonoma. El backend (FastAPI) emite una playlist en bucle (**Listener Loop**) y la distribuye por WebSocket; el cliente de escucha (`index.html`) se conecta solo a esa emision. Opcionalmente, un oyente puede sintonizar la URL publica de un DJ.

## Arquitectura

```
backend (FastAPI, desplegado)
  ├─ AutoDJ: ffmpeg lee music/playlist.txt en bucle -> WebM/Opus
  ├─ GET /          cliente de escucha (index.html), mismo origen que el stream
  ├─ WS  /listen    oyentes (fan-out en memoria)
  ├─ WS  /broadcast DJ en vivo (reemplaza al AutoDJ mientras esta conectado)
  ├─ GET /dj        panel DJ
  └─ GET /status, /healthz
```

## Comportamiento del cliente

1. Al abrir la URL publica se conecta a `wss://<mismo-host>/listen` (Listener Loop). No hay nada que configurar.
2. Si el navegador bloquea el autoplay, se muestra el boton **ESCUCHAR** resaltado; un toque inicia el audio.
3. **Opcional:** pegar la URL publica de un DJ (`wss://host/listen`, o solo `https://host`) y pulsar **SINTONIZAR**. Ese cliente reproduce al DJ en lugar de la radio propia (se recuerda en el navegador).
4. **VOLVER A RADIO** (o vaciar el campo y pulsar SINTONIZAR) restaura el Listener Loop.
5. Si la conexion se corta, el cliente reconecta con backoff.

## Playlist

Copiar los audios a `music/` y listar un archivo por linea en `music/playlist.txt`. La carpeta se incluye en la imagen Docker, por lo que el despliegue no depende de volumenes locales.

## Playlist dinámica (noticias inyectadas)

Un servicio externo (p. ej. TerraGazette) puede enviar audios ya generados y la radio los emite **dentro del mismo stream**, sin reiniciar la emisión, sin el panel DJ y sin reconfigurar nada. La música sigue siendo la cortina; las noticias entran entre pistas (o de inmediato con `priority=high`, con fundido de la música).

**Panel DJ estable.** `/dj` propone por defecto `ws(s)://<host de la pagina>/broadcast` (sirve con Docker `8001`, https y Render). Un solo socket a la vez; si la conexion cae sin que el DJ pulse DISCONNECT, reintenta con backoff (1-15 s) y reinicia la emision con una cabecera WebM nueva; envia `ping` cada 15 s. Si abris dos paneles, el nuevo toma la emision y el anterior queda en OFFLINE (codigo 1008, sin reconexion automatica para evitar un ping-pong). El audio debe ser WebM/Opus (Chrome/Edge); Firefox (ogg) o Safari (mp4) reciben un error explicito (1003). Los logs indican peer, codigo de cierre y si una conexion fue reemplazada.

**Reproductor.** `PLAYOUT_ENGINE=continuous` (por defecto) reemplaza el `ffmpeg concat` por un encoder Opus persistente alimentado con PCM (`backend/playout.py`): el stream WebM es siempre uno (un solo init), de modo que los oyentes no resincronizan. `PLAYOUT_ENGINE=legacy` restaura el AutoDJ original (no admite inyección: la API responde 503). Si entra un DJ en vivo, la noticia en emisión vuelve a la cola (hasta 3 intentos) y se emite cuando el DJ se va.

**Autenticación.** `RADIO_API_TOKEN` (Bearer). Sin token la API queda deshabilitada (503). Definirlo en Render/`.env`, nunca en el repo.

| Endpoint | Descripcion |
|---|---|
| `PUT /api/playlist/items/{external_id}?title=&kind=news\|spot&priority=normal\|high&source=&expires_in_s=` | Cuerpo = audio crudo (mp3/wav/ogg/flac/webm/m4a/aac). Cabeceras opcionales: `X-Content-SHA256` (verifica y evita reenviar), `X-Metadata` (JSON ≤2 KB). 201 encolado; 200 si es duplicado (mismo `external_id` o mismo contenido, `duplicate: true`); 409 mismo id con otro contenido; 413/415/422 validacion; 429 cola llena. |
| `GET /api/playlist/items/{id o external_id}` | Estado: `queued → playing → played \| skipped \| failed \| expired \| removed`, posicion, intentos, `started_at`/`finished_at`. |
| `DELETE /api/playlist/items/{id}` | Retira de la cola; si esta al aire solicita el corte (202). |
| `GET /api/playlist` | Cola, en emision, recientes, politica y estado del motor. |
| `GET/PATCH /api/playlist/policy` | Politica de archivos (abajo). |
| `GET /api/playlist/capabilities` | Formatos, limites y motores TTS disponibles. |
| `POST /api/playlist/tts` | Reservado para generar audio en Radio (Edge TTS): 501 hasta registrar un motor en `router.tts_providers`; el audio generado entra por el mismo camino de validacion/encolado. |

**Confirmacion de reproduccion.** `played` se marca cuando el ultimo bloque de la noticia entra al encoder (precision < 2 s); un fallo de decodificacion marca `failed` sin cortar la radio.

**Politica de archivos.** Tras emitirse, la noticia sale de la cola activa. `NEWS_AFTER_PLAY=delete` (defecto) borra el audio; `keep` lo conserva `NEWS_KEEP_HOURS` horas (0 = sin vencimiento) y siempre respeta `NEWS_MAX_STORAGE_MB` (se libera lo mas antiguo). Lo que no llego al aire (vencido, retirado, fallido) siempre se borra. Se puede cambiar en caliente con `PATCH /api/playlist/policy`. Otros limites: `NEWS_MAX_QUEUE` (50), `NEWS_MAX_UPLOAD_MB` (25), `NEWS_MAX_DURATION_S` (600), `NEWS_DEFAULT_TTL_S` (6 h; una noticia vencida no se emite).

**Persistencia.** `NEWS_DIR` (`/data/news`, volumen `news_data` en compose). En Render hace falta un disco persistente para sobrevivir a redeploys; sin el, la cola pendiente se pierde al reiniciar (TerraGazette la vuelve a enviar: el alta es idempotente).

```bash
curl -X PUT "http://localhost:8001/api/playlist/items/demo-1?title=Demo&priority=high" \
  -H "Authorization: Bearer $RADIO_API_TOKEN" --data-binary @noticia.mp3
curl -H "Authorization: Bearer $RADIO_API_TOKEN" http://localhost:8001/api/playlist/items/demo-1
```

Pruebas: `pip install -r backend/requirements.txt pytest httpx && pytest tests` (requiere `ffmpeg` y `ffprobe`; los tests de audio se omiten si faltan).

## Despliegue (Render)

`render.yaml` define un servicio Docker siempre encendido (`plan: starter`; el plan free se suspende y cortaria la emision) con health check en `/healthz`. Conectar el repo en Render como Blueprint y usar la URL publica resultante.

## Verificacion end-to-end (entorno desplegado)

```bash
curl -fsS https://<tu-servicio>/status      # autodj_enabled: true, stream_ready: true, source: "autodj"
```

Abrir `https://<tu-servicio>/` en un navegador limpio: debe pasar a **EN VIVO** sin configurar nada y seguir sonando tras terminar la ultima pista.

## Desarrollo local (solo para probar)

```bash
docker compose up --build -d   # http://localhost:8001
```
