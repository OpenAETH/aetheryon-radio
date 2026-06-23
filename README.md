# AETHERYON RADIO

Sistema de radio online con panel DJ de browser, servidor FastAPI WebSocket en Docker, y página de oyentes.

---

## Arquitectura

```
DJ (dj.html)
  └─ WebSocket ──► FastAPI :8000/broadcast
                       └─ Fan-out ──► Listeners (listener.html)
                                         └─ WS :8000/listen
```

---

## 1. Levantar el servidor

```bash
# Desde la raíz del proyecto
docker compose up --build -d

# Ver logs
docker compose logs -f
```

El server queda en `http://localhost:8000`

---

## 2. Exponer a Internet (túnel)

### Opción A — ngrok (más fácil)
```bash
ngrok http 8000
# Te da: https://abc123.ngrok.io
```

### Opción B — Cloudflare Tunnel (gratis, sin cuenta pagada)
```bash
cloudflared tunnel --url http://localhost:8000
```

### Opción C — Tu propio dominio + nginx reverse proxy
```nginx
location /radio/ {
    proxy_pass http://localhost:8000/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

---

## 3. Usar el Panel DJ

Abre `frontend/dj.html` en Chrome/Edge (Firefox tiene limitaciones con MediaRecorder+WebM).

1. **Carga audios**: arrastra MP3/WAV/OGG al panel izquierdo
2. **Conecta el servidor**: ingresa la URL WebSocket en el panel derecho
   - Local: `ws://localhost:8000/broadcast`
   - Con ngrok: `wss://abc123.ngrok.io/broadcast`
3. **Reproduce música**: usa los controles de transporte
4. **Go Live**: presiona el botón rojo para activar el micrófono y transmitir en vivo

---

## 4. Compartir con oyentes

Abre `frontend/listener.html` en cualquier browser, o comparte la URL con el parámetro:

```
listener.html?server=wss://abc123.ngrok.io/listen
```

---

## 5. API del servidor

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/` | GET | Info general |
| `/status` | GET | Oyentes actuales, peak, chunks |
| `/broadcast` | WebSocket | DJ envía audio aquí |
| `/listen` | WebSocket | Oyentes reciben aquí |

---

## Notas técnicas

- El audio se transmite en chunks de **100ms** (WebM/Opus ~64kbps)
- El servidor hace fan-out en memoria — sin disco, sin latencia de archivo
- Latencia estimada: **300-800ms** dependiendo del túnel
- Para producción con +100 oyentes: considera un servidor con más RAM y Icecast como relay secundario
