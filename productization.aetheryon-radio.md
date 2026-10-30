# Productization — AETHERYON Radio

> Catálogo de producto y capacidades interoperables de AETHERYON Radio.
> **Fuente:** `repo_report--aetheryon-radio.txt` aportado el 2026-10-29.
> **Estado general:** `in_development` para nuevas convergencias; el producto base ya dispone de emisión AutoDJ y emisión DJ en vivo según los archivos del reporte.
> **Nota de evidencia:** este documento se basa en el repo_report disponible. Las interfaces listadas como inferidas o documentadas no se consideran verificadas mediante una prueba ejecutada durante esta revisión.

## 1. Identidad

- **Producto:** AETHERYON Radio
- **Repositorio:** `https://github.com/OpenAETH/aetheryon-radio` (referenciado en la guía del repositorio)
- **Tipo:** aplicación de radio online / servicio de transmisión de audio.
- **Propósito en una frase:** ofrecer una emisión de audio continua y permitir que una persona sintonice una emisión de DJ en vivo.
- **Componentes principales:**
  - `backend/main.py`: servidor FastAPI, relay WebSocket y supervisor AutoDJ.
  - `index.html`: interfaz de escucha.
  - `frontend/dj.html`: panel de DJ.
  - `music/playlist.txt`: lista de reproducción del AutoDJ.
  - `backend/Dockerfile`, `docker-compose.yml`, `render.yaml`: empaquetado y despliegue.
- **Evidencia principal:** `README.md`, `backend/main.py`, `frontend/dj.html`, `index.html`, `music/README.md`, `music/playlist.txt`, `docker-compose.yml`, `backend/Dockerfile`, `render.yaml`, `AETHERYON_RADIO_Deployment_Operations_Guide.md`.

## 2. Descripción de utilidad humanizada

AETHERYON Radio es un medio de escucha online. Una persona puede abrir la página de la radio y escuchar la cortina programada que se reproduce en bucle. Si dispone de una emisión de DJ, también puede sintonizarla. El operador puede transmitir audio en vivo desde el panel DJ, mientras el servidor distribuye la emisión a los oyentes conectados.

La radio separa la experiencia de escucha del trabajo de programación: la emisión AutoDJ puede continuar sin que un DJ esté conectado, y vuelve a tomar el control cuando termina la emisión en vivo.

## 3. Utilidad propia

### Qué permite hacer
- Emitir una playlist AutoDJ desde el backend usando FFmpeg.
- Reproducir la playlist en bucle.
- Recibir una emisión en vivo desde el panel DJ por WebSocket.
- Distribuir el stream a múltiples oyentes mediante WebSocket.
- Servir la interfaz de escucha y el panel DJ.
- Exponer información básica de estado y una comprobación de salud.

### Resultado que entrega
Un canal de audio online que combina reproducción programada y emisiones de DJ, con una interfaz de escucha para usuarios remotos.

### Límites conocidos
- La playlist actual se define en `music/playlist.txt`, con un archivo por línea, relativo al directorio de música.
- El repo_report no muestra un endpoint HTTP específico para consultar, agregar, quitar o reemplazar elementos de la playlist.
- El reporte no demuestra que la playlist pueda modificarse de forma remota sin reconstruir o actualizar el contenido desplegado.
- El stream se distribuye mediante WebSocket y el fan-out de oyentes se mantiene en memoria, según la guía y el código descritos en el reporte.
- La seguridad/autorización para una futura operación de edición de playlist no está especificada en el reporte.

## 4. Capacidades que ofrece a la suite

### CAP-RADIO-001 — Emisión de audio AutoDJ
- **Descripción:** reproduce los archivos enumerados en la playlist configurada y publica la emisión para los oyentes.
- **Tipo:** `content` / `transport`
- **Disponibilidad:** `implemented` según el código y la configuración incluidos en el repo_report.
- **Interfaz actual:** el backend ejecuta el supervisor AutoDJ; la lista se configura mediante `AUTODJ_PLAYLIST`, con valor predeterminado `/music/playlist.txt`.
- **Formato de entrada:** nombres/rutas de archivos de audio, uno por línea en la playlist; las entradas se validan para mantener los archivos dentro del directorio de música.
- **Salida:** stream WebM/Opus distribuido por WebSocket.
- **Condiciones y límites:** depende de FFmpeg y de que existan archivos reproducibles en `MUSIC_DIR`; el AutoDJ se pausa mientras hay un DJ conectado y retoma después de su desconexión.
- **Evidencia:** `backend/main.py`, `music/README.md`, `music/playlist.txt`, `backend/Dockerfile`, `docker-compose.yml`.

### CAP-RADIO-002 — Distribución de stream a oyentes
- **Descripción:** distribuye los datos de la emisión activa a los clientes conectados.
- **Tipo:** `transport`
- **Disponibilidad:** `implemented` según el código descrito en el reporte.
- **Interfaz actual:** WebSocket `/listen`; emisión DJ por WebSocket `/broadcast`.
- **Formato:** mensaje de control `stream_start`, segmento de inicialización WebM y datos binarios de audio, según el docstring de `backend/main.py`.
- **Condiciones y límites:** el estado de oyentes y el fan-out son locales al proceso; no se documenta persistencia de sesiones.
- **Evidencia:** `backend/main.py`, `README.md`, `AETHERYON_RADIO_Deployment_Operations_Guide.md`.

### CAP-RADIO-003 — Estado y salud del servicio
- **Descripción:** permite consultar el estado general de la radio y una comprobación básica de salud.
- **Tipo:** `analytics` / `control`
- **Disponibilidad:** `implemented` según las rutas declaradas en `backend/main.py`.
- **Interfaz actual:** `GET /status` y `GET /healthz`.
- **Salida de `/status`:** incluye `dj_online`, `source`, `stream_ready`, `listeners`, `peak`, contadores de chunks/bytes, uptime y `autodj_enabled`.
- **Salida de `/healthz`:** incluye `ok`, `source` y `stream_ready`.
- **Evidencia:** `backend/main.py`, rutas `status()` y `healthz()`.

## 5. Capacidades que necesita consumir

### NEED-RADIO-001 — Actualización externa de la playlist
- **Necesidad:** permitir que un sistema autorizado actualice la playlist de AutoDJ sin intervención manual en el panel DJ ni edición directa del archivo en el despliegue.
- **Proveedor potencial:** un sistema editorial como TerraGazette, u otro productor de contenido compatible.
- **Contrato requerido:** una interfaz controlada para reemplazar o modificar la lista de reproducción; definir formato de entrada, validación de archivos/referencias, orden, duplicados, respuesta de éxito/error, autenticación y política de actualización.
- **Estado:** `proposed`.
- **Criterio de aceptación:** un consumidor autorizado envía una actualización válida; la radio la valida, aplica el cambio según la política definida y el nuevo contenido se reproduce sin interrumpir indebidamente la emisión ni alterar la función de DJ en vivo.
- **Evidencia del vacío:** las rutas enumeradas en `backend/main.py` incluyen `/`, `/dj`, `/status`, `/healthz`, WebSocket `/broadcast` y `/listen`; el repo_report no identifica una ruta de administración de playlist.

### NEED-RADIO-002 — Fuente de contenido editorial reproducible
- **Necesidad:** recibir contenido de otros sistemas en una forma que AutoDJ pueda reproducir.
- **Proveedor potencial:** TerraGazette u otro sistema editorial.
- **Contrato requerido:** decidir si el consumidor entrega archivos de audio, archivos accesibles localmente, URLs permitidas o un formato de tarea para generación de audio. El repo_report no establece que AutoDJ convierta texto a voz ni que acepte URLs remotas como elementos de playlist.
- **Estado:** `proposed`.
- **Criterio de aceptación:** el formato acordado produce un elemento de audio válido y reproducible, con controles de procedencia y fallos.
- **Nota:** la lectura periodística o generación TTS debe resolverse en el productor o en un componente adaptador, salvo que se implemente y valide explícitamente en Radio.

## 6. Capability Exchange — mercado de capacidades

| ID | Proveedor | Capacidad ofrecida | Consumidor potencial | Estado | Referencia |
|---|---|---|---|---|---|
| EX-RADIO-001 | AETHERYON Radio | Emisión AutoDJ y distribución de audio | TerraGazette u otros productores de audio | `implemented` como capacidad base; el intercambio externo no está implementado | CAP-RADIO-001 |
| EX-RADIO-002 | AETHERYON Radio | Distribución de stream a oyentes | Cualquier cliente compatible con el protocolo de escucha | `implemented` | CAP-RADIO-002 |
| EX-RADIO-003 | AETHERYON Radio | Actualización remota de playlist | TerraGazette / sistema de automatización | `proposed` | NEED-RADIO-001 |

**Regla:** que exista una capacidad base de AutoDJ no significa que exista un contrato para que otra aplicación la modifique. El intercambio externo de playlist permanece como oportunidad hasta desarrollar y validar una interfaz específica.

## 7. Convergencias de la suite

### CONV-TERRAGAZETTE-RADIO-001 — Noticias editoriales hacia la emisión de radio
- **Objetivo humano:** permitir que las noticias preparadas por TerraGazette lleguen a la emisión de AETHERYON Radio y se escuchen como contenido editorial sonoro.
- **Proveedor:** TerraGazette, capacidad de noticias procesadas. La capacidad concreta y su interfaz deben documentarse en el `productization.md` de TerraGazette.
- **Consumidor:** AETHERYON Radio, `NEED-RADIO-001` y `NEED-RADIO-002`.
- **Flujo propuesto:**
  1. TerraGazette selecciona/prepara una noticia.
  2. Un adaptador produce un activo reproducible o una referencia aceptada por el contrato.
  3. Un endpoint autenticado de Radio valida y actualiza la playlist o una cola editorial separada.
  4. AutoDJ reproduce el contenido según una política acordada.
- **Contrato:** no existe todavía en la evidencia disponible. Debe especificar payload, autenticación, validación, orden, idempotencia, errores, persistencia y política de activación.
- **Cambios requeridos:** diseñar la interfaz de gestión de playlist; definir dónde se almacenan los activos editoriales; definir si las noticias se añaden a una playlist, se insertan en una cola o reemplazan la programación; definir manejo de fallos.
- **Riesgos/dependencias:** texto no equivale a audio; el actual AutoDJ consume archivos de audio locales en el directorio de música. Deben protegerse los endpoints de modificación y evitar que entradas arbitrarias permitan acceso a archivos fuera de la biblioteca.
- **Estado:** `proposed`.
- **Criterio de validación:** enviar una noticia de prueba desde un entorno de TerraGazette, comprobar la aceptación/rechazo del contrato, verificar que se incorpora según la política establecida y escuchar el resultado a través de un cliente de radio. Confirmar que la emisión DJ en vivo y el retorno al AutoDJ siguen funcionando.

## 8. Arquitectura e interfaces existentes

| Interfaz | Método/protocolo | Función observada | Estado de evidencia | Fuente |
|---|---|---|---|---|
| `/` | HTTP GET | Sirve la interfaz de escucha | Declarada en código | `backend/main.py` |
| `/dj` | HTTP GET | Sirve el panel DJ | Declarada en código | `backend/main.py` |
| `/status` | HTTP GET | Devuelve estado de DJ, fuente, oyentes, contadores y AutoDJ | Declarada en código | `backend/main.py` |
| `/status` | HTTP OPTIONS | Respuesta para preflight/status | Declarada en código | `backend/main.py` |
| `/healthz` | HTTP GET | Comprobación de salud | Declarada en código | `backend/main.py` |
| `/broadcast` | WebSocket | Entrada de stream desde DJ en vivo | Declarada en código | `backend/main.py` |
| `/listen` | WebSocket | Entrega stream a oyentes | Declarada en código | `backend/main.py` |

### Configuración relevante
- `AUTODJ_ENABLED`: habilita AutoDJ; el valor predeterminado en el código es `true`.
- `AUTODJ_PLAYLIST`: ruta de la playlist; predeterminado `/music/playlist.txt`.
- `AUTODJ_RESTART_DELAY`: espera antes de reiniciar AutoDJ tras un fallo; predeterminado `3`.
- `MUSIC_DIR`: directorio de archivos de audio; predeterminado `/music`.
- `AUTODJ_LOOP_REPEAT`: cantidad de repeticiones para construir el bucle de playlist; predeterminado `1000`.
- `MAX_LISTENERS`: máximo de oyentes configurado en el backend; predeterminado `200`.
- Docker expone el puerto `8000` del contenedor y `docker-compose.yml` lo mapea al `8001` del host.

### Despliegue y operación
- `docker-compose.yml` define el servicio `radio-server`, reinicio `unless-stopped` y el montaje `./music:/music:ro`.
- `backend/Dockerfile` instala FFmpeg y ejecuta Uvicorn en el puerto `8000`.
- `render.yaml` forma parte del despliegue, y la guía documenta el uso de un servicio Docker persistente.
- La playlist tiene un archivo por línea; líneas vacías y comentarios que empiezan por `#` se ignoran, según `music/README.md`.

## 9. Vías de productización

- **Componente reutilizable:** relay de audio WebSocket y motor AutoDJ que distribuye una playlist programada.
- **Paquete de aplicación:** radio online autogestionada con playlist continua, panel DJ y página de escucha.
- **Sistema personalizado:** radio corporativa o canal sonoro que incorpore fuentes de contenido autorizadas, sujeto a definir y validar las integraciones necesarias.
- **Modelo de entrega:** el repositorio contiene configuración Docker y Render; la modalidad comercial/operativa ofrecida a clientes no queda demostrada por el repo_report.
- **Estado comercial:** `evaluating`. No se infieren precios ni planes comerciales.

## 10. Roadmap de productización

| Prioridad | Acción | Motivo | Dependencia | Criterio de cierre | Estado |
|---|---|---|---|---|---|
| P1 | Definir contrato de gestión de playlist | Habilitar intercambios con otros sistemas | Decidir reemplazo, adición o cola separada | Contrato versionado con payloads y errores definidos | proposed |
| P1 | Diseñar protección del endpoint | Evitar cambios no autorizados | Mecanismo de autenticación y autorización | Pruebas de acceso autorizado/no autorizado | proposed |
| P1 | Definir representación del contenido editorial | TerraGazette produce noticias; Radio reproduce audio | Decidir si el audio se genera en TerraGazette o en un adaptador | Activo de audio de prueba reproducible | proposed |
| P2 | Implementar actualización controlada de playlist/cola | Permitir cambios remotos sin editar manualmente el despliegue | Contrato y seguridad acordados | Prueba de integración automatizada | proposed |
| P2 | Validar el flujo TerraGazette → Radio | Confirmar el valor de la convergencia | Ambos sistemas y un activo de prueba | Prueba end-to-end y escucha verificada | proposed |

## 11. Calidad de la evidencia y vacíos

### Confirmado por el repo_report
- Hay backend FastAPI en `backend/main.py`.
- El backend contiene rutas HTTP `/`, `/dj`, `/status`, `/healthz` y WebSockets `/broadcast`, `/listen`.
- AutoDJ lee `AUTODJ_PLAYLIST` y emite audio mediante FFmpeg.
- `music/playlist.txt` contiene un nombre de archivo de audio.
- `music/README.md` explica el formato de playlist y el comportamiento de bucle.
- Docker/Compose definen la ejecución y el montaje de la carpeta de música.

### Inferido
- El producto se puede describir como un medio de escucha continuo con opción de emisión de DJ, a partir de la interfaz y los flujos documentados.
- La capacidad de distribución de stream puede consumirse desde clientes que implementen el protocolo esperado, aunque la compatibilidad de un cliente externo concreto no está verificada aquí.

### Propuesto, no implementado según la evidencia revisada
- Endpoint de edición remota de playlist.
- Contrato de intercambio de noticias con TerraGazette.
- Inserción de noticias habladas o conversión de texto a voz dentro de Radio.
- Cola editorial independiente, scheduling de noticias y controles de prioridad.

### No determinado
- Autenticación/autorización actual de los endpoints de estado y de cualquier futura interfaz de gestión.
- Si el despliegue real utiliza exactamente la configuración de Compose o una variante externa.
- Contrato de salida actual de TerraGazette y ubicación/formato del audio generado por ese sistema.
- Garantías de persistencia, idempotencia o versionado para actualizaciones de playlist, dado que esa interfaz no aparece en el reporte.

## 12. Mantenimiento

Actualizar este documento cuando cambien los endpoints, el formato de playlist, el mecanismo AutoDJ, la operación de DJ en vivo o las integraciones de la suite. Reanalizarlo con un `repo_report` reciente. La propuesta de productización no requiere modificar la aplicación por sí misma; cualquier implementación posterior debe realizarse como un cambio acotado, con pruebas y preservando el comportamiento actual.
