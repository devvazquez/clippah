# clipper

Detector automático de momentos destacados en VODs de **Twitch** y **YouTube**.
Pegas un enlace y devuelve una lista de momentos con **descripción, fotograma y timestamp**.

Todo corre en local. **Coste objetivo: 0 €** — funciona sin ninguna API key (modo
degradado local) y mejora si las hay.

```
enlace VOD → ingesta → señales (chat + audio) → candidatos
           → transcripción (solo candidatos) → puntuación LLM
           → fotogramas → UI con lista de momentos
```

> **El render está implementado**: «Generar clip» produce un mp4 **1080×1920** con los
> subtítulos quemados a partir de los timestamps de palabra, y el botón pasa a
> «Descargar clip». El `RenderSpec` sigue siendo el contrato: se expone en
> `GET /api/moments/{id}/spec`.

---

## Instalación

**Requisitos:** Python 3.11+, Node 20+, `ffmpeg` y `ffprobe` en el `PATH`.

```bash
# ffmpeg
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg           # macOS

git clone <este-repo> && cd clipper
make setup                    # venv del backend + npm install del frontend
make setup-local              # opcional: faster-whisper para transcribir sin API keys
make dev                      # backend :8000 + frontend :3000
```

Abre <http://localhost:3000> y pega un VOD.

`make setup` copia `backend/.env.example` a `backend/.env`. **No hace falta tocarlo**: sin
ninguna clave la app funciona igual, solo más lenta y con títulos menos ricos.

| Comando | Qué hace |
|---|---|
| `make dev` | Levanta los dos servicios con un solo comando |
| `make backend` / `make frontend` | Solo uno de los dos |
| `make check` | `ruff check` + `tsc --noEmit` |
| `make doctor` | Comprueba ffmpeg/ffprobe y las dependencias |
| `make clean-data` | Borra media, miniaturas y la base de datos |

### Capturas

Pantalla de entrada: input con validación, historial y estado de proveedores.

![Pantalla de entrada](docs/screenshot-home.png)

Resultados: parrilla de momentos con fotograma real del VOD, timestamp, score, categoría
y señales. Las capturas están tomadas en modo local (sin API keys), por eso los títulos
salen del transcript y llevan el badge «Sin IA»; las miniaturas del VOD aparecen con el
icono de placeholder porque la CDN de Twitch no era accesible desde donde se capturó.

![Parrilla de momentos](docs/screenshot-job.png)

---

## Modos de funcionamiento

El backend elige proveedor automáticamente y **degrada en lugar de fallar**: un job
siempre termina con resultados.

| | Transcripción | Títulos y puntuación | Momentos visuales | Aviso en la UI |
|---|---|---|---|---|
| Sin claves | `faster-whisper` local, o ninguna si no está instalado | heurística sobre las señales | no | ámbar «Modo local (sin API keys) — más lento» |
| `GROQ_API_KEY` | Groq `whisper-large-v3-turbo` | heurística | no | — |
| `GEMINI_API_KEY` | local | Gemini | **sí** | — |
| Las dos | Groq | Gemini | **sí** | verde «Groq + Gemini» |

Los momentos que vienen del proponente visual llevan un badge **visión** en la tarjeta y
se pueden filtrar con el chip «solo visión».

Cuando una cuota se agota, el pipeline emite un evento `warning`, cae al proveedor local y
**termina con resultados**. El aviso se ve en la pantalla del job.

Consíguelas gratis en [console.groq.com](https://console.groq.com/keys) y
[aistudio.google.com](https://aistudio.google.com/apikey), y pégalas en `backend/.env`.

---

## Cómo decide qué es un momento

Lo interesante no es detectar «picos», es detectar los picos correctos. Cuatro decisiones
hacen casi todo el trabajo:

**1. z-score local, no umbrales absolutos.** Cada streamer tiene un baseline distinto:
unos están callados, otros tienen un chat que spamea sin parar. Cada señal se normaliza
contra su propia mediana y MAD móviles (ventana de 300 s), que son robustas a picos:

```
z = (x - mediana_movil(x)) / (1.4826 * MAD_movil(x) + suelo)
```

**2. El chat va con retraso.** El pico de chat ocurre entre 3 y 15 s *después* del evento.
Si centras el clip en el pico de chat, lo interesante queda fuera por delante. Las señales
de chat se **adelantan** `CHAT_LAG_S` (8 s por defecto) antes de fusionarlas, y la ventana
es asimétrica: `[t_peak − 25 s, t_peak + 15 s]`.

**3. Bonus de combo (×1.4).** Si en la misma ventana de 10 s coinciden pico de audio y pico
de chat, el score se multiplica. La coincidencia de dos señales independientes es mucho más
predictiva que un pico grande en una sola.

**4. Emotes, no keywords.** `emote_burst` cuenta mensajes que son mayoritariamente emotes o
llevan expresiones de hype. Es **independiente del idioma**: los streamers en
castellano/catalán no escriben «POGGERS», escriben «jajaja», «madre mía» o «quina passada».
La lista está en `backend/app/config.py` y se puede sobrescribir por entorno.

Además: `unique_users` por bin (más robusto frente al spam de un solo usuario),
non-maximum suppression con `MIN_GAP_S = 45`, descarte de los primeros y últimos 60 s
(pantallas de «starting soon») y exclusión de los tramos muteados por copyright del cálculo
del baseline.

**5. Un segundo proponente: mirar la pantalla.** Audio y chat solo encuentran momentos
con *reacción*. Un hito visual silencioso — equipo raro conseguido, construcción
terminada, un marcador alto — no levanta el audio ni el chat, así que nunca llega a ser
candidato, y el LLM no lo ve nunca porque solo filtra lo que las señales proponen.

Con `GEMINI_API_KEY` se activa un proponente visual: una sola pasada de ffmpeg muestrea
un fotograma cada `VISION_SAMPLE_S` (20 s por defecto) a 512 px, y Gemini juzga los
fotogramas en lotes con salida JSON forzada (`notable`, `what`, `kind`, `confidence`).
Cada acierto se afina buscando el máximo local de la señal de audio en su ventana — el
evento suele empezar antes del fotograma que lo delata — y pasa por el mismo NMS que los
picos, así que un momento que las señales ya habían encontrado no se duplica.

> **La visión propone, no titula.** Gemini ve un fotograma sin audio ni contexto y se
> equivoca en la semántica del juego: en las pruebas llamó «criatura robótica hostil» al
> compañero de partida. Por eso su descripción entra en el prompt de puntuación como
> *pista*, con instrucción explícita de que el transcript manda si la contradice. Solo
> se usa como título cuando no hay LLM que redacte.

Una pasada de ffmpeg cuesta lo mismo con 1 fotograma cada 20 s que cada 5: lo que se
paga es recorrer el vídeo (~6 min por cada 30 min de VOD). Los fotogramas se cachean, así
que reanalizar es gratis. Coste en tokens: ~1.100 por fotograma, unos 110.000 para un VOD
de 33 min. Se desactiva con `VISION_ENABLED=0`.

**6. El criterio no es «aquí pasa algo», es «esto funciona como clip».** El prompt de
puntuación juzga potencial viral con criterios explícitos: gancho en los primeros 1-2
segundos, se entiende sin contexto, tiene remate, una emoción fuerte y clara, y se puede
resumir en una frase. Penaliza conversación cotidiana, explicaciones, chistes internos y
gameplay competente pero normal — y dice explícitamente que la reacción del chat *no*
convierte un momento en viral, porque los suyos reaccionan a cosas que a un desconocido
no le dicen nada. `clip_score` se reserva: 80-100 solo si lo compartirías tú, 60-79 vale
para los seguidores del canal, por debajo de 50 no es un clip.

Medido re-puntuando los mismos 7 fragmentos de un VOD real, el ranking se invierte: con
el prompt anterior mandaban «Frustración con el trabajo» (55) y «Discusión sobre un
compañero» (45); con el de viralidad suben «Reacción a muñeca gigante» (45), «Susto con
cara gigante» (40) y «Game Over y frustración» (35), y la charla cae a 25/20/15/10. Cada
momento trae además su `hook`: qué se ve u oye en los primeros 2 segundos, que es lo que
te dice si hay que recortar el arranque.

### Solo se transcriben los candidatos

30 candidatos × 40 s = **20 minutos** de audio, frente a las 6 horas del VOD. Esta decisión
es la que hace viable el tier gratuito: con los 28.800 segundos de audio diarios de Groq
salen unos 24 VODs de 6 h al día.

Con las palabras alineadas se **refinan los bordes** del corte para que no empiece ni acabe
a media frase: el inicio retrocede al primer arranque de palabra tras el silencio más largo
de `[t_peak−30, t_peak−10]`, y el final avanza hasta el último final de palabra antes del
siguiente silencio de más de 0,6 s. Eso es lo que separa un clip que parece editado de un
recorte aleatorio.

---

## El clip que sale

El formato no es una elección estética: sale de medir los 30 clips más vistos de
**auronplay** e **ibai** vía el GraphQL de Twitch.

| Canal | Mediana | Media | 16-30 s |
|---|---|---|---|
| auronplay | 26 s | 34 s | 60 % |
| ibai | 26 s | 28 s | 63 % |
| un canal pequeño (los clips que hace a mano) | 20 s | 22 s | 64 % |

De ahí `TARGET_CLIP_S=26`: cuando el refinado de bordes se pasa holgadamente del
objetivo, se recorta por el final — el pico está al principio de la ventana, así que
sobra cola, no cabeza.

Lo que se renderiza:

- **1080×1920, H.264, 30 fps, AAC.** Vertical de verdad, no un 16:9 con bandas.
- **Subtítulos quemados de 2-3 palabras**, generados desde los timestamps de palabra que
  ya produce la transcripción. Dos o tres palabras se leen de un vistazo; una frase
  entera obliga a parar el scroll, que es lo contrario de lo que se busca. Van en
  mayúsculas, con borde negro grueso, y **por encima del 20 % inferior** del lienzo,
  donde las plataformas ponen su propia interfaz.
- **Tres layouts.** `blur` (por defecto) escala el 16:9 completo al ancho y rellena con
  una copia ampliada y desenfocada de sí mismo: no pierde nada del fotograma, lo que
  importa en estos directos porque la webcam va **compuesta dentro** del 16:9 y un
  recorte central se la come. `crop` recorta a 9:16 alrededor de `focus_x` (imagen más
  grande, pero se pierden los laterales). `split` pone la webcam arriba y el juego abajo
  cuando se le pasan las coordenadas de la cámara.
- **El título solo se quema si lo escribió el LLM.** Sin IA el título son las primeras
  palabras del transcript: quemarlo en el vídeo sería peor que no poner nada.

Coste: unos 60 s de ffmpeg por clip de 30 s, tirando directamente del stream remoto sin
descargar el VOD.

## Ajustado a canales pequeños

Un canal sin audiencia no es un canal grande en miniatura: cambia qué señales sirven.

- **El chat escaso se trata como si no hubiera chat** (`MIN_CHAT_RATE_PER_MIN`), porque
  su z-score se satura y taparía al audio sin aportar nada.
- **Si el chat no sirve, la visión dobla su presupuesto**: muestrea un fotograma cada 10 s
  en vez de cada 20 y acepta el doble de aciertos. Recorrer el vídeo cuesta lo mismo con
  un intervalo que con el otro; solo cambia el gasto en tokens. Cuando el chat es la única
  señal muerta, la pantalla es lo único que queda.
- **La duración objetivo se toma de los clips que el propio canal publica**, no de los de
  un canal masivo.

## Arquitectura

```
clipper/
├── backend/                    Python 3.11 · FastAPI · SQLite · asyncio
│   ├── app/
│   │   ├── main.py             rutas /api + SSE
│   │   ├── db.py               esquema y migraciones
│   │   ├── config.py           settings desde entorno
│   │   ├── events.py           bus de eventos (persistido, reengancha el SSE)
│   │   ├── pipeline/
│   │   │   ├── orchestrator.py cola in-process y ejecución del job
│   │   │   ├── ingest.py       yt-dlp: metadata, audio, URL de stream
│   │   │   ├── chat.py         Twitch GQL / YouTube live_chat
│   │   │   ├── signals.py      RMS de audio, densidad de chat, z-scores
│   │   │   ├── candidates.py   fusión, picos, NMS
│   │   │   ├── transcribe.py   Groq | faster-whisper local
│   │   │   ├── score.py        Gemini | heurística
│   │   │   └── frames.py       ffmpeg -ss → jpg
│   │   └── providers/
│   │       ├── ratelimit.py    token bucket + cuota diaria persistida
│   │       ├── groq.py, gemini.py, local.py
│   └── data/                   (gitignored) media/, thumbs/, clipper.db
├── frontend/                   Next.js 15 · TypeScript · Tailwind
└── scripts/                    utilidades de verificación
```

**Descarga:** en el modo por defecto (`DOWNLOAD_MODE=stream_seek`) **no se descarga el
vídeo**. Un VOD de 6 h en 720p son ~10 GB; solo se baja el audio (WAV 16 kHz mono, ~690 MB
para 6 h) y los fotogramas se sacan bajo demanda con `ffmpeg -ss <t> -i <url_remota>`.
Poniendo `-ss` **antes** de `-i`, ffmpeg hace seek en el contenedor remoto sin descargarlo.
Si la extracción remota falla dos veces, cae automáticamente a descargar el mp4 a 480p.

---

## API

Prefijo `/api`. CORS abierto a `localhost:3000` (configurable con `CORS_ORIGINS`).
Documentación interactiva en <http://127.0.0.1:8000/docs>.

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/jobs` | `{url}` → `{job_id, video}`. Valida el enlace e inicia el pipeline |
| `GET` | `/jobs/{id}` | Estado + `moments[]` si ha terminado |
| `GET` | `/jobs/{id}/events` | **SSE** con progreso en vivo (reenvía el historial) |
| `DELETE` | `/jobs/{id}` | Cancela y limpia ficheros temporales |
| `GET` | `/jobs` | Historial paginado |
| `GET` | `/moments/{id}/thumbnail` | JPEG del fotograma (bajo demanda + caché en disco) |
| `POST` | `/moments/{id}/render` | Renderiza el mp4 vertical 9:16 con subtítulos. `?layout=blur\|crop\|split` |
| `GET` | `/moments/{id}/clip` | Descarga el mp4 renderizado |
| `GET` | `/moments/{id}/spec` | El `RenderSpec` del momento |
| `GET` | `/health` | Estado de proveedores y cuota restante del día |

Eventos SSE (progreso monótono, un `id:` por evento para poder reengancharse):

```json
{"stage": "ingest",     "progress": 0.15, "message": "Descargando audio (12%)"}
{"stage": "chat",       "progress": 0.30, "message": "18.420 mensajes"}
{"stage": "candidates", "progress": 0.50, "message": "30 candidatos"}
{"stage": "transcribe", "progress": 0.75, "message": "Transcribiendo 14/30 (groq)"}
{"stage": "warning",    "message": "Cuota de groq agotada. Usando Whisper local."}
{"stage": "done",       "progress": 1.0,  "moments": 12}
```

---

## Configuración

Todo en `backend/.env` (ver `backend/.env.example`). Lo más útil:

| Variable | Def. | Para qué |
|---|---|---|
| `DOWNLOAD_MODE` | `stream_seek` | `full` descarga el mp4 a 480p en lugar de hacer seek remoto |
| `MAX_VOD_HOURS` | `8` | Rechaza VODs más largos con un error legible |
| `CHAT_LAG_S` | `8` | Cuánto se adelanta el chat al fusionar |
| `W_CHAT` / `W_USERS` / `W_EMOTE` / `W_AUDIO` | `1.0` / `1.2` / `1.5` / `0.8` | Pesos de la fusión. Sin chat, `W_AUDIO=1` y el resto a 0 |
| `PEAK_PERCENTILE` | `97` | Umbral de picos. Se relaja solo si salen menos de 5 |
| `MIN_GAP_S` | `45` | Separación mínima entre picos (NMS) |
| `MAX_CANDIDATES` / `TOP_N` | `30` / `12` | Candidatos analizados / momentos mostrados. **Es el que manda en el tiempo total** si transcribes en local: ver abajo |
| `MIN_CLIP_S` / `MAX_CLIP_S` | `12` / `60` | Duración del clip tras refinar bordes |
| `WHISPER_MODEL` | `large-v3-turbo` | `small` o `medium` para máquinas modestas |
| `YTDLP_COOKIES_FROM_BROWSER` | — | `firefox`/`chrome`/… si YouTube pide verificación anti-bot |
| `VISION_ENABLED` | `1` | Proponente visual (necesita `GEMINI_API_KEY`) |
| `VISION_SAMPLE_S` | `20` | Un fotograma cada N s. Bajarlo no acelera ni encarece el muestreo, solo el coste en tokens |
| `VISION_MAX_HITS` | `12` | Candidatos visuales aceptados como máximo |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | Alias `-latest` a propósito: `gemini-2.5-flash-lite` ya devuelve 404 para cuentas nuevas |
| `KEEP_MEDIA` | `0` | `1` conserva el WAV al terminar (útil para reanalizar) |
| `EDGE_TRIM_S` | `60` | Segundos descartados al principio y al final |
| `GROQ_ASD` | `28800` | Segundos de audio/día de Groq. Bájalo para probar la degradación |

### Cuánto tarda

Con Groq la transcripción es cuestión de segundos. En local depende de **cuánta voz**
hay en las ventanas, no de su duración: el filtro VAD descarta el silencio antes de
decodificar. Medido en una máquina de 4 núcleos con `WHISPER_MODEL=small`:

| Tipo de directo | Voz por ventana de 53 s | Por candidato |
|---|---|---|
| Gameplay con música y pausas | 9-15 s | ~3 s |
| Directo hablado / charla | 33-39 s | ~95 s |

Es decir, en un directo muy hablado los 30 candidatos por defecto pueden ser **45
minutos** de transcripción local. Si te importa el tiempo: baja `MAX_CANDIDATES` a
`TOP_N` (transcribir 30 para mostrar 12 solo tiene sentido cuando el LLM filtra falsas
alarmas), usa `WHISPER_MODEL=small`, o pon una `GROQ_API_KEY`.

La descarga del audio es el otro tramo lento: unos 8 minutos por cada 30 min de VOD, y
se cachea, así que reanalizar el mismo VOD con otros parámetros ya no la repite (con
`KEEP_MEDIA=1`).

---

## Verificación

```bash
make check                                     # ruff check + tsc --noEmit

# Fases 3-5: picos de audio/chat y candidatos de un VOD real
backend/.venv/bin/python scripts/inspect_signals.py https://www.twitch.tv/videos/<id>

# Alineado del chat, ventanas y ratios sobre un VOD sintetico (sin red)
backend/.venv/bin/python scripts/check_alignment.py

# Fase 10: la cuota se agota y el pipeline degrada en lugar de fallar
backend/.venv/bin/python scripts/check_quota_degradation.py
```

Lo que se ha comprobado contra un VOD real (Twitch, 1 h 45 min, sin API keys):

| | |
|---|---|
| Chat | 19.942 mensajes por paginación por offset, sin tropezar con KPSDK |
| Señales | 241 bins de audio muteado excluidos del baseline automáticamente |
| Candidatos | picos con NMS de 45 s, ventanas de 16-45 s tras refinar bordes |
| Transcripción | `faster-whisper` local, español detectado con 0,96-0,99 de confianza |
| Fotogramas | 12 JPEG de 640×360 por seek remoto, sin descargar el vídeo |
| Degradación | `GROQ_ASD=10` → evento `warning` («Cuota de groq agotada… Usando Whisper local») y el job termina con momentos |
| SSE | corte y reconexión con `Last-Event-ID`: retoma en el evento siguiente, sin repetir ni perder |
| Stub de render | `POST /moments/{id}/render` → 501 con el `RenderSpec` completo (50 captions con timestamps absolutos) |

Para reproducir el criterio de degradación completo, pon en `backend/.env`:

```bash
GROQ_API_KEY=cualquier-cosa
GROQ_ASD=60
```

y lanza un análisis: verás el evento `warning` («Cuota de groq agotada… Usando Whisper
local») y el job terminará con momentos.

---

## Trampas conocidas

- **El chat replay de Twitch se pagina por `contentOffsetSeconds`, no por cursor.** La
  paginación por cursor dispara el reto de integridad KPSDK en la segunda petición y
  necesitaría un navegador real. Por offset no lo dispara.
- **`gemini-2.5-flash-lite` está retirado para cuentas nuevas** y devuelve 404 con el
  mensaje de que uses un modelo más reciente. Por eso los defaults son los alias
  `-latest`, que no se quedan obsoletos. Un `503 high demand` de un modelo concreto
  también es normal: se reintenta y se cae al siguiente de la cadena.
- **`ffmpeg -ss` va antes de `-i`.** Al revés decodifica el fichero entero desde el
  principio y tarda minutos en un VOD largo.
- **Las URLs HLS de Twitch caducan** (token de ~24 h). Se guarda `stream_url_expires_at`;
  si un `ffmpeg -ss` devuelve 403 se re-resuelve con `yt-dlp -g` y se reintenta una vez.
- **Twitch mutea tramos con música con copyright.** El audio muteado da RMS ≈ 0 y produce
  falsos negativos: los tramos de silencio absoluto de más de 30 s se excluyen del cálculo
  del baseline.
- **Los VODs de cuentas normales caducan a los 60 días** y sin replay no hay chat. El
  análisis continúa solo con audio y lo avisa en la UI.
- **Raids y donaciones** disparan el chat sin que pase nada: para eso está el
  `worth_clipping` del LLM.
- **No se fuerza el idioma en Whisper.** Los streamers de aquí mezclan castellano y catalán
  en la misma frase; la autodetección funciona mejor.
- **`ffprobe` puede reportar duraciones distintas a las de yt-dlp** en HLS. Se usa siempre
  la de yt-dlp como referencia para los offsets del chat.
- **YouTube puede pedir verificación anti-bot** desde IPs de datacenter. En una máquina
  normal no pasa; si pasa, yt-dlp acepta `--cookies-from-browser`.

---

## Notas legales

Descargar VODs **no encaja con los Términos de Servicio de Twitch**. El contenido pertenece
al streamer. Los VODs suelen incluir música con copyright que hará que el clip resultante
sea retirado en TikTok o YouTube.

Esta herramienta es para **uso personal o con permiso explícito del creador**.

El endpoint GraphQL de Twitch que se usa para el chat replay es **privado y no
documentado**: puede dejar de funcionar sin previo aviso.
