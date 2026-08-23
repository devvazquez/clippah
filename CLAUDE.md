# Guía para un agente que trabaje en clipper

Esto no repite el README (léelo para la arquitectura). Es lo que hay que saber para hacer
los encargos que llegan de verdad, con los comandos exactos y las trampas en las que ya se
ha caído alguien.

**Antes de nada:** el motor necesita `ffmpeg`, el venv (`make setup-backend`) y las fuentes
(`make setup-font`). Las claves van en `backend/.env` o como variables de entorno; sin
`SUPABASE_URL` y `SUPABASE_SERVICE_KEY` no hay cola, y sin `GROQ_API_KEY` /
`GEMINI_API_KEY` se trabaja en modo degradado (más lento y con títulos peores).

---

## Encargo: «encuéntrame un clip gracioso»

### 1. Busca en las transcripciones, no en los momentos analizados

El análisis solo transcribe las ventanas candidatas (unos dos minutos por directo), así que
buscar en la base de datos deja fuera casi todo el directo. Las transcripciones completas
están versionadas en `backend/data/transcripts/*.groq.tsv`, un fichero por directo, con
`inicio<TAB>fin<TAB>texto` y **segundos absolutos del VOD**:

| Directo | VOD | Duración |
|---|---|---|
| 17 ago | `v2849071905` | 117 min |
| 18 ago | `v2850022597` | 102 min |
| 20 ago | `v2851488297` | 33 min |

```bash
python scripts/find_phrase.py susto grito madre         # instantáneo: lee los .tsv
python scripts/find_phrase.py --groq --vod v2852853179 jopa   # un directo sin transcribir
```

Si el directo que te interesa no está en la tabla, hace falta transcribirlo (`--groq`, unos
30× tiempo real). **Whisper en local no vale para esto**: con `tiny` y `small` el habla
rápida con ruido de juego sale destrozada («un sigo, un señor») y un nombre propio no
aparece nunca. Cuesta cuota: Groq admite **7.200 s de audio por hora** y 28.800 al día, y un
directo largo es 7.000 s, así que un directo entero se come la hora. La herramienta guarda
cada trozo, así que si se corta, relanzarla continúa donde estaba.

### 2. Lee la transcripción como un editor, no como un buscador

Lo que hace gracia casi nunca lleva una palabra clave. Lee los tramos donde hay reacción
—gritos, risas, insultos, «qué haces», «no me lo creo»— y busca lo que cumple el criterio
del scorer (`backend/app/providers/gemini.py`, la constante `PROMPT`). Resumido:

1. **Que haga gracia o no hay clip.** Alguien que no conoce a nadie: ¿se ríe, se sorprende,
   le da vergüenza ajena? Si la respuesta es «es simpático», es un no.
2. **Gancho inmediato**: algo pasa en los primeros 1-2 s.
3. **Retiene hasta el final**: sube, tiene un segundo remate. Un chiste al principio con
   veinte segundos de bajada muere igual que uno sin gancho.
4. **Se entiende solo**, sin conocer al streamer ni el juego.
5. **Corto**: 15-30 s. Un minuto no se acaba.

Lo que se descarta sin piedad: planificar construcciones, explicaciones, leer el chat,
agradecer follows, gameplay competente pero normal. En estos directos eso es la mitad del
tiempo.

### 3. Elige la ventana mirando los tiempos de las palabras

**Trampa que ya costó un clip:** el `inicio` de una línea del `.tsv` es el de su primera
palabra, y una línea agrupa ~10 palabras, así que la frase que buscas puede estar 6
segundos más adelante. Y Whisper reparte los tiempos sobre las pausas: una frase dicha con
una pausa de dos segundos en medio llega como dos palabras «larguísimas».

Antes de decidir los bordes, mira dónde hay voz de verdad (local, gratis, instantáneo):

```bash
ffmpeg -nostdin -hide_banner -ss 4058 -t 26 -i backend/data/media/twitch-v2850022597.wav \
  -af "silencedetect=noise=-32dB:d=0.45" -f null -
```

Los tiempos que imprime son relativos al `-ss`, hay que sumarlo. Empieza el clip en el
bloque de voz donde arranca la frase, no antes: un arranque con dos segundos de silencio se
lleva por delante el gancho.

### 4. Córtalo y publícalo

```bash
python scripts/clip_at.py --vod v2850022597 --start 4062.3 --end 4080.2 --publish
python scripts/clip_at.py --vod v2850022597 --start 4062.3 --end 4080.2 \
    --title "ayer Jopa, hoy PoliSpawn 💀" --peak 4069.4 --publish
```

Hace lo mismo que el pipeline con esa ventana: transcribe, se lo pasa al scorer para el
título/música/efectos, saca la miniatura, renderiza y sube. Sin `--publish` solo deja el
mp4 en `backend/data/clips/`.

- **El título es lo primero que se lee en el feed.** Tiene que nombrar la cosa concreta de
  *este* clip (el bicho, el mote, la cifra). Si valdría para otro clip distinto, está mal.
  Máximo 42 caracteres. Se puede pasar con `--title` o cambiar luego desde la interfaz.
- **Los nombres propios salen mal.** Whisper los escribe de oído; `TRANSCRIBE_VOCAB` (en
  `config.py`) le pasa el vocabulario del canal como pista y arregla bastantes, pero no
  todos. Compruébalos y corrígelos.
- El scorer puede puntuar bajo un clip que te ha pedido una persona. Da igual: se publica,
  pero dilo.

### 5. Compruébalo antes de decir que está

```bash
ffprobe -v error -show_entries stream=codec_type,start_time,duration -of csv=p=0 clip.mp4
ffmpeg -nostdin -hide_banner -i clip.mp4 -af ebur128 -f null -   # I: entre -14 y -17 LUFS
ffmpeg -v error -ss 6 -i clip.mp4 -frames:v 1 -vf scale=340:-1 -y /tmp/f.jpg  # y míralo
```

El audio tiene que empezar en `0.000` y durar lo que el vídeo. Mira el fotograma de verdad:
es la única forma de ver que el título está quemado, que no tapa nada importante y que el
encuadre de las cámaras es el bueno.

---

## Donde corre esto de verdad

El motor **no** es esta sandbox: es el workflow `.github/workflows/clipper.yml`, que cada 5
minutos mira la cola de Supabase y hace lo que haya. Un runner de GitHub trae ffmpeg y
tiene 4 vCPU, y en un repo publico los minutos son gratis e ilimitados. Ver `DEPLOY.md`.

Consecuencias para lo que hagas aqui:

- **Lo que se encola desde la interfaz lo procesa Actions**, no el backend local. Si estas
  esperando a que pase algo, mira la pestana Actions del repo, no `backend.log`.
- **Nada puede depender de `backend/data/`**, que no viaja: cada runner arranca de cero.
  Por eso cada clip lleva su ficha (`render_spec`) en Supabase. Las transcripciones si
  viajan, porque estan versionadas.
- Para probar el ciclo entero sin gastar cuota ni tocar Supabase: `make check-queue` (30
  comprobaciones) y `make check-hint` (27).
- Levantar el backend local (`make backend-keep`) sigue valiendo para trabajar, pero ya no
  es lo que sostiene el servicio.

## Lo que pide el usuario en una frase

La interfaz manda, con el enlace, una frase corta que llega hasta el pipeline
(`clip_requests.prompt` -> `jobs.hint` -> `pipeline/hint.py`). Hace dos cosas:

- **Siempre**: se le pasa al scorer (`HINT_BLOCK` en `providers/gemini.py`) y rompe empates.
  No relaja el criterio: un fragmento sin gracia no se convierte en clip por encajar.
- **Si la frase es una busqueda** («busca donde…», «encuentra donde dicen…»): ademas
  localiza el momento buscando los terminos en el chat del VOD y en las transcripciones
  versionadas, y anade esas ventanas como candidatos con `source='hint'`, que en
  `score.finalize` van multiplicados por `HINT_FACTOR`.

Asi que **antes de buscar a mano con `find_phrase.py`, mira si basta con encolar el directo
con la frase**: el pipeline hace lo mismo y encima renderiza. `find_phrase.py` sigue siendo
lo tuyo para explorar una transcripcion sin gastar un turno entero.

---

## Encargo: «analiza este directo»

```bash
# Lo normal: encolarlo y que lo haga el worker (aparece en la interfaz en directo)
curl -s -X POST "$SUPABASE_URL/rest/v1/clip_requests" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.twitch.tv/videos/2850022597","clips":2,"status":"queued"}'

make drain        # procesa lo que haya en la cola y termina
```

Un directo largo son 30-45 min. El worker renderiza los `clips` mejores del análisis. Para
repartir entre varios directos, encola una petición por directo con su número.

Si solo quieres volver a puntuar con el criterio nuevo, sin gastar transcripción:
`python scripts/rescore.py job_xxxx`.

---

## Encargo: «cambia el título / los subtítulos / el sonido de un clip»

Se hace desde la interfaz (<https://clipper-5zq.pages.dev>), y por debajo es escribir el
borrador en Supabase y poner `render_status='rerender_queued'`. La clave anon solo puede
escribir esas cinco columnas: `title_edited`, `captions_edited`, `sfx_edited`,
`music_edited`, `render_status`. El worker rehace el clip, lo sube como versión nueva (ruta
nueva, para que ningún navegador sirva el mp4 viejo de su caché) y limpia el borrador.

**Si un clip se queda en «rehaciendo» y no avanza, el worker está caído.** No es el clip.

---

## Cosas que muerden

- **El worker se muere solo.** Ha pasado media docena de veces: el log corta justo después
  de `no se pudo leer la cola: All connection attempts failed`, sin traza. Levántalo con
  `setsid nohup make backend-keep > backend.log 2>&1 < /dev/null &` y comprueba
  `curl localhost:8000/api/queue/status`. Mientras está caído, lo que se guarda desde la
  interfaz espera en la cola (no se pierde) pero no avanza.
- **La base local no viaja.** Está en `backend/data/`, que no se versiona. Un contenedor
  recién clonado no sabe nada de los momentos: por eso cada clip guarda su ficha
  (`render_spec`) en Supabase y el worker reconstruye la fila con ella
  (`service.ensure_moment`). Si tocas el re-render, no lo rompas.
- **Cuotas.** Groq: 7.200 s de audio/hora, 28.800/día. Gemini: mira `quota_usage` en la base
  antes de tirar. Si Groq se agota, el transcriptor se cae a Whisper local, que con el
  modelo grande tarda minutos por ventana y parece que se ha colgado.
- **`alimiter` de ffmpeg no es un techo** si no le pones `level=disabled`: por defecto
  también auto-nivela y sube la mezcla por su cuenta.
- **No renderices sobre el clip de verdad al probar.** `check_queue.py` lo hizo y dejó en la
  galería un mp4 con «PRUEBA» quemado; ahora trabaja sobre una copia en `/tmp`.

## Verificación antes de dar algo por bueno

```bash
make check          # ruff + tsc
make check-queue    # el ciclo entero contra un Supabase de mentira (28 comprobaciones)
```

Y la regla que más disgustos ha evitado: **si dices que algo funciona, ten delante la salida
que lo demuestra.** Un `status` que responde no prueba que el proceso sea el nuevo (puede
ser uno viejo que sigue ocupando el puerto); un clip «listo» no prueba que el título esté
quemado (mira el fotograma).
