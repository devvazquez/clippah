# Montarlo para que funcione solo, gratis y sin depender de ningún ordenador

Esto es lo que hay que hacer una vez para que clipper quede así:

```
móvil ──▶ clipper-5zq.pages.dev ──▶ Supabase (cola)
          Cloudflare Pages              │
          gratis, siempre en pie        │  cada 5 min, o al instante
                                        ▼
                              GitHub Actions (el motor)
                              ffmpeg, 4 vCPU, 16 GB
                                        │
                                        ▼
                              Supabase Storage ──▶ la galería, editable
```

No hay ninguna máquina que mantener encendida. **El motor es un workflow de GitHub
Actions**: cada 5 minutos mira la cola, y si hay algo, analiza el directo, renderiza los
clips en vertical y los sube. Cuando no hay nada, sale en unos segundos. En un repositorio
público, los minutos de Actions son ilimitados y gratis.

---

## Lo que ya está hecho

No hace falta que toques nada de esto, está aplicado y comprobado:

- La columna `prompt` está en Supabase y la política de la clave anon la acepta (probado:
  una petición con frase entra, una de 400 caracteres se rechaza).
- La interfaz nueva está publicada en <https://clipper-5zq.pages.dev> con el campo «Qué
  buscar» y sus atajos.
- Los dos workflows están en `.github/workflows/`: `clipper.yml` (el motor) y `pages.yml`
  (publicar la interfaz cuando cambie, opcional). Están en la rama por defecto del repo,
  que es donde GitHub exige que estén para que el cron y el disparador funcionen: no hay
  que fusionar nada.

## Lo que tienes que hacer tú

Son dos cosas y unos cinco minutos.

### 1. Decidir si el repositorio es público o privado

Es la única decisión que cambia algo, y cambia bastante:

| | Público | Privado |
|---|---|---|
| Minutos de Actions | **ilimitados** | 2.000/mes |
| Sondear cada 5 min | gratis | se come el mes entero solo en sondeos (cada arranque cuenta como 1 minuto) |
| Qué queda a la vista | el código y las transcripciones de los directos. Las claves **no**: están en `backend/.env`, que no se versiona | nada |

- **Público** (lo recomendado si te da igual que se vea el código): no toques nada más, el
  workflow ya viene con el cron de 5 minutos.
- **Privado**: abre `.github/workflows/clipper.yml`, borra el bloque `schedule:` (las tres
  líneas del `cron`) y haz el **paso 4** para que el motor arranque solo cuando hay
  trabajo. Con eso los 2.000 minutos dan para unos 40 directos al mes.

Se cambia en `Settings → General → Change repository visibility`.

### 2. Poner las claves en GitHub

`Settings → Secrets and variables → Actions → New repository secret`. Cuatro secretos:

| Secreto | De dónde sale |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → Data API → Project URL |
| `SUPABASE_SERVICE_KEY` | ahí mismo → API keys → **service_role** (la secreta, no la anon) |
| `GROQ_API_KEY` | la que ya usas. Sin ella transcribe con Whisper local: mucho más lento |
| `GEMINI_API_KEY` | la que ya usas. Sin ella los títulos y la puntuación salen peores |

Las dos primeras son obligatorias; sin ellas el motor no puede leer la cola y lo dice en
el log. Las dos últimas son las que tienes en `backend/.env` de la sandbox.

### 3. Comprobar que el runner funciona

Antes de gastar cuota en un directo de verdad:

1. Pestaña **Actions** → workflow **clipper** → **Run workflow**.
2. Marca la casilla **«Solo comprobar que el runner está bien»** → Run.

Tarda unos 3 minutos (instala el venv, las fuentes y los emojis). No toca Supabase ni
gasta cuota de nada: monta un Supabase de mentira y hace el ciclo entero contra él
—analizar, renderizar, subir, editar el título, rehacer el clip— más las comprobaciones
de la búsqueda por frase. Si acaba en verde, el motor funciona en ese runner: son 30 + 27
comprobaciones, incluidas que el audio arranca a 0.000 y que el clip sale a −14 LUFS.

Si falla, el resumen del run dice en qué paso. Los dos sitios donde ha fallado de verdad
mientras se montaba esto: una dependencia de Python sin declarar en `backend/pyproject.toml`
y `ffmpeg`, que **no viene en la imagen del runner** y el workflow instala en el turno.

### 4. Opcional: que arranque al instante en vez de esperar al cron

Con el cron solo, entre darle a «Guardar en la cola» y ver que empieza pueden pasar 5-15
minutos (GitHub no garantiza el minuto exacto). Si quieres que salga en segundos —y si el
repositorio es privado, esto es lo que sustituye al cron:

1. Saca un token de GitHub en <https://github.com/settings/personal-access-tokens/new>:
   *fine-grained*, **Only select repositories** → este repo, y en Repository permissions →
   **Contents: Read and write**. Es lo que pide la API que despierta el workflow.
2. Abre `supabase/kick_github.sql`, pega tu token en el `INSERT` y ejecuta el fichero
   entero en el SQL editor de Supabase.

Desde entonces, cada vez que se encola algo o se edita un clip, Postgres avisa a GitHub y
el turno empieza solo. El propio fichero explica cómo comprobar que el aviso llega
(`select … from net._http_response`).

### 5. Opcional: que la interfaz se publique sola

Ya está publicada; esto solo sirve para que un cambio futuro en `frontend/` se vea sin
construir nada a mano. Añade estos secretos y el workflow `interfaz` lo hará en cada push:
`CLOUDFLARE_API_TOKEN` (permiso *Cloudflare Pages: Edit*), `CLOUDFLARE_ACCOUNT_ID`,
`NEXT_PUBLIC_SUPABASE_URL` y `NEXT_PUBLIC_SUPABASE_ANON_KEY`.

---

## Cómo se usa desde el móvil

Abre <https://clipper-5zq.pages.dev> (mejor «Añadir a pantalla de inicio»: se abre como
una app). Pegas el enlace del directo, escribes lo que quieres y cuántos clips.

**El campo «Qué buscar» hace dos cosas distintas**, según lo que escribas:

- **Un gusto** — «el más gracioso», «un fail», «donde grita». Se le pasa al modelo que
  puntúa: entre dos momentos parecidos gana el que encaja con lo que pediste. No cambia el
  resto del criterio: un momento sin gracia sigue sin dar un clip aunque encaje.
- **Una búsqueda** — «busca donde le llaman jopa», «encuentra donde dice "no me lo creo"».
  Además de lo anterior, va a **localizar** ese momento. Y esto es lo que no podía hacer
  antes: el análisis normal solo mira los picos de reacción, así que una frase dicha en un
  tramo tranquilo no llegaba ni a transcribirse.

  La búsqueda no gasta cuota. Mira el **chat del directo** (que se descarga entero de todas
  formas) y las **transcripciones guardadas** en `backend/data/transcripts/`. Si no
  encuentra nada, lo dice y sigue con los momentos de más reacción.

  Detalle que importa: en el chat los nombres propios están bien escritos y en la
  transcripción no («Jopa» → «Hopa»), así que el chat es el que suele acertar.

Si lo dejas vacío, hace lo de siempre: los momentos con más reacción.

Los clips aparecen en la galería agrupados por directo y se editan ahí igual que ahora
—título, subtítulos, música, efectos—. Al guardar, el clip se rehace en el mismo motor.

### Cuánto tarda

| | |
|---|---|
| Arrancar el turno | 5-15 min con el cron, segundos con el paso 4 |
| Montar el runner | ~2 min (ffmpeg se instala cada turno; el venv va en caché) |
| Bajar el audio | ~8 min por cada 30 min de directo |
| Analizar y puntuar | 5-10 min |
| Renderizar cada clip | 1-2 min |

Un directo de 2 h con 3 clips: unos 45 minutos desde que le das a guardar. El progreso se
ve en la propia interfaz mientras pasa.

---

## Los límites de lo gratis

| Servicio | Límite gratis | Cuánto es eso | Qué pasa al llegar |
|---|---|---|---|
| GitHub Actions | ilimitado (repo público) | — | — |
| GitHub Actions | 2.000 min/mes (repo privado) | ~40 directos | los runs esperan al mes siguiente |
| Supabase Storage | 1 GB | ~150 clips | no se pueden subir más: borra clips viejos de la galería |
| Supabase salida | 5 GB/mes | ~700 descargas de clip | se corta hasta el mes siguiente |
| Groq (transcripción) | 7.200 s de audio/hora, 28.800/día | 4 directos largos al día | se cae a Whisper local, más lento |
| Gemini (títulos) | cuota diaria de la capa gratis | de sobra para esto | se cae a la heurística: títulos peores, clips igual |
| Cloudflare Pages | 500 builds/mes | de sobra | — |

Un aviso: **Supabase pausa los proyectos gratis tras una semana sin actividad**. Con el
cron de 5 minutos eso no pasa nunca, porque el propio sondeo cuenta como actividad. Si
quitas el cron (repo privado), el proyecto puede pausarse en una semana muerta; se
reactiva desde el panel.

## Si algo no va

| Lo que ves | Qué es |
|---|---|
| Encolas y no pasa nada en 20 min | Actions → clipper: ¿hay runs? Si no hay ninguno, faltan los secretos o el cron está desactivado |
| «Scheduled workflows disabled» por correo | GitHub apaga los cron tras 60 días sin commits en el repo. Se vuelve a activar con un botón en Actions |
| Un run falla en «Mirar la cola» | mal `SUPABASE_URL` o `SUPABASE_SERVICE_KEY` |
| Falla al bajar el audio con un 403 | Twitch bloqueando la IP del runner. Es el riesgo real de este montaje; hay que verlo en el primer directo de verdad. Si pasa a menudo, la salida es un runner propio (`Settings → Actions → Runners`) en cualquier máquina tuya |
| Un clip se queda en «rehaciendo» | el turno acabó antes de terminarlo. El siguiente lo recoge: el worker devuelve a la cola lo que se quedó a medias |
| «Sin clips: el análisis no dejó momentos» | el análisis no encontró nada que pasara el criterio. Prueba con más clips o sin frase |

El log completo de cada turno queda en el run, como *artifact* `turno-<id>`, siete días.

## Lo que deja de hacer falta

La sandbox y `make backend-keep`. El motor que se moría cada pocas horas ya no es donde
pasa nada: si un turno de Actions se cae, el siguiente recoge lo que quedó pendiente, y eso
es automático. `make dev` sigue valiendo para trabajar en local.
