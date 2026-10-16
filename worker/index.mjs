// AETHERYON Radio — Worker de entrada (Cloudflare Workers + Containers).
//
// El Worker NO emite audio: Workers no puede correr ffmpeg ni procesos de larga duracion.
// Su unico trabajo es dar una URL publica estable y reenviar TODO el trafico (HTTP y WebSocket)
// al contenedor, que ejecuta backend/main.py (AutoDJ en bucle + relay /listen /broadcast).
// Una unica instancia con nombre fijo => todos los oyentes comparten la misma emision.
import { Container } from "@cloudflare/containers";

export class RadioContainer extends Container {
  defaultPort = 8000;       // uvicorn dentro de backend/Dockerfile
  sleepAfter = "30m";       // sin trafico se duerme; el primer oyente lo despierta y el AutoDJ arranca solo
  envVars = {
    AUTODJ_ENABLED: "true",
    AUTODJ_PLAYLIST: "/music/playlist.txt",
    MUSIC_DIR: "/music",
  };
}

export default {
  async fetch(request, env) {
    return env.RADIO.getByName("aetheryon-radio").fetch(request);
  },
};
