# AETHERYON RADIO
## Deployment & Operations Guide

**AETHERYON Systems — Agraound Consulting**  
**Junio 2026**

---

## 1. Arquitectura del sistema

AETHERYON Radio es un sistema de transmisión de audio online compuesto por tres capas:

- **Panel DJ (`dj.html`):** interfaz local del operador. Permite cargar audio, controlar la reproducción y gestionar el micrófono.
- **Servidor FastAPI (Docker):** relay WebSocket que recibe el stream del DJ y lo distribuye a los oyentes conectados.
- **Página Listener (Cloudflare Workers):** interfaz pública para oyentes. Recibe el stream mediante WebSocket seguro.

### Flujo de señal

```text
DJ (dj.html local)
  └─ ws://localhost:8001/broadcast
       └─ FastAPI Docker :8001
            └─ Fan-out a oyentes
                 └─ wss://[cloudflare-tunnel]/listen
                      └─ Listener (Cloudflare Workers)
```

## 2. Prerrequisitos

- Docker Desktop con soporte WSL2.
- `cloudflared` instalado en WSL.
- Navegador Chrome o Edge, requerido para la compatibilidad esperada con `MediaRecorder` y WebM/Opus.
- Repositorio: <https://github.com/OpenAETH/aetheryon-radio>

## 3. Despliegue del servidor (Docker)

### 3.1. Configuración del puerto

El servidor escucha internamente en el puerto `8000` y se expone en el puerto `8001` del host. Verificar en `docker-compose.yml`:

```yaml
ports:
  - "8001:8000"
```

### 3.2. Levantar el contenedor

Desde la raíz del proyecto, donde se encuentra `docker-compose.yml`, ejecutar:

```bash
docker compose up --build -d
```

Verificar que el servidor responda:

```bash
curl http://localhost:8001/status
```

Respuesta esperada:

```json
{"dj_online":false,"listeners":0,"peak":0,"chunks_relayed":0}
```

### 3.3. Comandos útiles

Ver logs en tiempo real:

```bash
docker compose logs -f
```

Detener el servicio:

```bash
docker compose down
```

Reconstruir y recrear el contenedor:

```bash
docker compose down && docker compose up --build --force-recreate -d
```

> **Nota:** Si el puerto `8001` está ocupado, cambiar el mapeo en `docker-compose.yml` y actualizar las URLs correspondientes.

## 4. Túnel público (Cloudflare Tunnel)

Cloudflare Tunnel expone el servidor local a Internet sin necesidad de abrir puertos en el router. Es necesario para que los oyentes externos puedan conectarse mediante WebSocket seguro (`wss://`).

### 4.1. Instalar `cloudflared` (WSL/Linux)

```bash
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
```

### 4.2. Iniciar el túnel

Para iniciar un **Quick Tunnel** (sin cuenta y con URL temporal):

```bash
cloudflared tunnel --url http://localhost:8001
```

La terminal mostrará la URL pública asignada. Ejemplo:

```text
https://controlling-proposition-les-disco.trycloudflare.com
```

> **Importante:** En modo Quick Tunnel, la URL cambia al reiniciar el túnel. Para disponer de una URL fija, configurar un túnel nombrado desde una cuenta de Cloudflare.

Confirmar que el túnel está activo revisando los logs. Una conexión establecida puede mostrar una línea similar a:

```text
Registered tunnel connection connIndex=0 ... location=eze01 protocol=quic
```

## 5. Configuración de las interfaces

### 5.1. Panel DJ (`dj.html`, local)

Abrir `dj.html` directamente en Chrome o Edge. No requiere un servidor web local.

Configurar los campos del panel derecho:

**Server URL**

```text
ws://localhost:8001/broadcast
```

**Listener URL**

```text
https://aetheryon-radio.guido-gomez278.workers.dev/?server=wss://[TU-URL-CLOUDFLARE]/listen
```

Reemplazar `[TU-URL-CLOUDFLARE]` por el dominio generado por `cloudflared`. Ejemplo:

```text
ws://localhost:8001/broadcast
```

```text
https://aetheryon-radio.guido-gomez278.workers.dev/?server=wss://controlling-proposition-les-disco.trycloudflare.com/listen
```

Presionar **CONNECT**. El panel debería mostrar:

- `STATUS: CONNECTED`
- `LISTENERS: N`, con la cantidad de oyentes activos.
- `BITRATE: ... kbps`, con la tasa de bits en tiempo real.

### 5.2. Página Listener (Cloudflare Workers)

Página pública:

<https://aetheryon-radio.guido-gomez278.workers.dev/>

En el campo de servidor de la página Listener, ingresar la URL del WebSocket y presionar **SET**:

```text
wss://controlling-proposition-les-disco.trycloudflare.com/listen
```

Después, presionar **TUNE IN**. El estado esperado es:

`CONECTADO — ESPERANDO DJ`

El campo también acepta la URL completa con el parámetro `?server=`. En ese caso, la dirección del WebSocket se completa automáticamente.

> **Importante:** La URL del ejemplo es temporal. Usar la URL activa que muestre la terminal de `cloudflared`.

## 6. Operación en vivo

### 6.1. Transmitir audio de la queue

1. Cargar archivos MP3, WAV u OGG arrastrándolos al panel izquierdo de `dj.html`.
2. Hacer doble clic en una pista para cargarla.
3. Presionar **▶ PLAY**. Si el panel está conectado, comienza el broadcast de audio.
4. El Listener cambia al estado **EN VIVO** al recibir el primer bloque de audio.

### 6.2. Transmitir el micrófono en vivo

1. Presionar **GO LIVE**. El navegador solicitará permiso para usar el micrófono.
2. Aceptar el permiso.
3. El audio del micrófono se mezcla con el audio de la queue en el broadcast.
4. Presionar nuevamente **GO LIVE** para mutear el micrófono. La reproducción de la queue continúa transmitiéndose.

### 6.3. Controles del transporte

| Control | Función |
|---|---|
| **▶ PLAY / ⏸ PAUSE** | Reproduce o pausa la pista actual e inicia o detiene el broadcast de audio. |
| **⏮ PREV / NEXT ⏭** | Navega entre las pistas de la queue. |
| **⟳ LOOP** | Repite la pista actual en bucle. |
| **AUTO** | Avanza automáticamente a la siguiente pista al terminar la actual. |
| **GO LIVE** | Activa o mutea el micrófono sin interrumpir el broadcast de la queue. |
| **Crossfader** | Mezcla el audio de la queue (izquierda) y el micrófono (derecha). |
| **VOL** | Controla el volumen general de monitoreo local; no modifica el volumen del broadcast. |

## 7. API del servidor

| Endpoint | Método | Descripción |
|---|---|---|
| `/` | `GET` | Devuelve el estado general del servidor en JSON. |
| `/status` | `GET` | Informa oyentes activos, pico de oyentes y cantidad de chunks enviados. |
| `/broadcast` | WebSocket | Punto de conexión del DJ para enviar audio. |
| `/listen` | WebSocket | Punto de conexión de los oyentes para recibir audio. |

## 8. Resolución de problemas (Troubleshooting)

### 8.1. El puerto `8001` ya está en uso

Identificar el proceso que escucha en el puerto:

```bash
ss -tlnp | grep 8001
```

Si corresponde y se confirmó que puede detenerse, finalizarlo:

```bash
kill -9 <PID>
```

Como alternativa, cambiar el puerto en `docker-compose.yml` y actualizar el `Server URL` del panel DJ y las demás URLs afectadas.

### 8.2. Error `WebSocket connection failed` en el Listener

- Verificar que `cloudflared` siga ejecutándose en la terminal.
- Si se reinició el Quick Tunnel, actualizar la URL: el dominio anterior puede haber dejado de ser válido.
- Desde una página HTTPS, usar `wss://` para conexiones WebSocket seguras, no `ws://`.

### 8.3. El Listener no recibe audio

- Confirmar que el panel DJ esté conectado al servidor (`STATUS: CONNECTED`).
- Recordar que el broadcast comienza al presionar **PLAY** cuando el DJ está conectado.
- Conectar el Listener antes de comenzar a transmitir. Si se conecta después, volver a conectarlo si no recibe el stream.
- Usar Chrome o Edge, navegadores objetivo de esta implementación. Firefox puede presentar diferencias de compatibilidad con `MediaRecorder` y WebM/Opus.

### 8.4. ngrok vs. Cloudflare Tunnel

En la configuración probada para este proyecto, ngrok en su plan gratuito presentó bloqueos o intermediación de solicitudes externas que provocaron errores de CORS y fallos de WebSocket. Cloudflare Tunnel permite exponer el servicio local y transportar WebSockets sin abrir puertos entrantes en el router.

## 9. Referencias

- **Repositorio:** <https://github.com/OpenAETH/aetheryon-radio>
- **Listener público:** <https://aetheryon-radio.guido-gomez278.workers.dev/>
- **Documentación de Cloudflare Tunnel:** <https://developers.cloudflare.com/cloudflare-one/connections/connect-apps>
- **Servidor FastAPI:** `backend/main.py`, relay WebSocket con fan-out en memoria.
- **Códec y transporte de audio:** WebM/Opus a 64 kbps, en chunks de 100 ms mediante la API `MediaRecorder`.

---

**AETHERYON Systems**  
*Independent Technology Partner · Agraound Consulting*
