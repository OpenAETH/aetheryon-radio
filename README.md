# AETHERYON RADIO

Radio online autonoma. El backend (FastAPI) emite una playlist en bucle (**Listener Loop**) y la distribuye por WebSocket; el cliente de escucha (`index.html`) se conecta solo a esa emision. Opcionalmente, un oyente puede sintonizar la URL publica de un DJ.

## Arquitectura

```
Worker (URL publica) -> Container: backend (FastAPI)
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

## Despliegue (Cloudflare Workers + Containers)

Workers no puede correr ffmpeg ni el relay, asi que el backend corre en un **Cloudflare Container** (la imagen de `backend/Dockerfile`, con `music/` incluido). El Worker (`worker/index.mjs`) solo expone la URL publica y reenvia HTTP y WebSocket al contenedor, que se despierta con el primer oyente y arranca el AutoDJ solo.

```bash
npm install
npx wrangler deploy        # requiere Docker local para construir la imagen
```

`wrangler.jsonc` usa el nombre `aetheryon-radio`, por lo que reemplaza el Worker estatico actual y conserva la URL `workers.dev`. Requiere el plan Workers Paid (verificar en la pagina de precios de Containers).

## Verificacion end-to-end (entorno desplegado)

```bash
curl -fsS https://aetheryon-radio.<tu-subdominio>.workers.dev/status   # autodj_enabled: true, stream_ready: true, source: "autodj"
```

Abrir la URL `workers.dev` en un navegador limpio: debe pasar a **EN VIVO** sin configurar nada y seguir sonando tras terminar la ultima pista.

## Desarrollo local (solo para probar)

```bash
docker compose up --build -d   # http://localhost:8001
```
