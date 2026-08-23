-- Esquema de la cola compartida entre la interfaz y el backend.
--
-- La interfaz (navegador, clave anon) escribe peticiones en `clip_requests` y lee
-- `clips`. El backend (clave service_role, en la sandbox) reclama peticiones, va
-- escribiendo el progreso en la misma fila y sube cada clip a Storage. Las dos partes
-- ven los cambios de la otra por Realtime, sin sondear.
--
-- Se ejecuta tal cual en el SQL editor del proyecto (es idempotente).

-- ---------------------------------------------------------------- estados posibles
do $$
begin
  if not exists (select 1 from pg_type where typname = 'clip_request_status') then
    create type clip_request_status as enum (
      'queued',    -- la ha guardado la interfaz, nadie la ha tocado
      'claimed',   -- el backend la ha cogido (evita que dos la cojan a la vez)
      'running',   -- analizando o renderizando; `stage` y `progress` van al dia
      'done',
      'error',
      'canceled'
    );
  end if;
end $$;

-- ------------------------------------------------------------------- la cola
create table if not exists public.clip_requests (
  id            uuid primary key default gen_random_uuid(),
  url           text not null,
  clips         int  not null default 1 check (clips between 1 and 10),
  -- Lo que pide quien encola, en una frase: "el mas gracioso", "donde le llaman jopa".
  -- Orienta al scorer y, si es una busqueda, dispara la busqueda en el chat y en las
  -- transcripciones guardadas (ver `backend/app/pipeline/hint.py`).
  prompt        text,
  status        clip_request_status not null default 'queued',
  -- Espejo del progreso del pipeline local, para que la interfaz lo vea en directo.
  stage         text not null default 'queued',
  progress      real not null default 0,
  message       text not null default 'En cola',
  error         text,
  -- Datos del VOD, que solo se saben despues de leerlo.
  video_title   text,
  video_url     text,
  duration_s    real,
  clips_done    int not null default 0,
  -- Trazabilidad con la base local de la sandbox.
  job_id        text,
  worker        text,
  created_at    timestamptz not null default now(),
  claimed_at    timestamptz,
  finished_at   timestamptz,
  updated_at    timestamptz not null default now()
);

-- Para los proyectos que ya tenian la tabla creada, donde el `create table` de arriba
-- no hace nada.
alter table public.clip_requests add column if not exists prompt text;

create index if not exists clip_requests_queue_idx
  on public.clip_requests (status, created_at);
create index if not exists clip_requests_created_idx
  on public.clip_requests (created_at desc);

-- ------------------------------------------------------------------- los clips
create table if not exists public.clips (
  id            uuid primary key default gen_random_uuid(),
  request_id    uuid references public.clip_requests (id) on delete set null,
  -- id del momento en la base local: hace la subida idempotente.
  moment_id     text not null unique,
  rank          int  not null default 0,
  title         text not null default '',
  storage_path  text not null,
  size_bytes    bigint not null default 0,
  duration_s    real not null default 0,
  width         int  not null default 1080,
  height        int  not null default 1920,
  -- De donde sale y que lleva puesto.
  video_url     text,
  video_title   text,
  t_start       real,
  t_end         real,
  score         real,
  clip_score    real,
  source        text,          -- signals | vision
  category      text,
  sfx           text,
  music         text,
  transcript    text,
  reason        text,          -- por que lo eligio el modelo
  created_at    timestamptz not null default now()
);

-- Subtitulos editables. `captions` es lo que hay quemado en el mp4 ahora mismo;
-- `captions_edited` es lo que ha escrito la interfaz y el backend aun no ha renderizado.
-- Cada cue es {"text": "...", "start": 1.2, "end": 2.0}, en segundos desde el inicio del
-- clip. `version` sube en cada re-render: el mp4 nuevo va a otra ruta para que ningun
-- navegador sirva el viejo de su cache.
alter table public.clips add column if not exists captions        jsonb not null default '[]'::jsonb;
alter table public.clips add column if not exists captions_edited jsonb;
-- El sonido se edita igual que los subtitulos: `sfx_cues` es lo que suena ahora
-- ([{"name": "vineboom.mp3", "t": 12.4, "gain_db": -9}]) y `sfx_edited` lo que espera
-- render ([] = ningun efecto). `music_edited` es la pista ('' = ninguna).
alter table public.clips add column if not exists sfx_cues        jsonb not null default '[]'::jsonb;
alter table public.clips add column if not exists sfx_edited      jsonb;
alter table public.clips add column if not exists music_edited    text;
-- El titulo quemado se edita igual: `title` es el que lleva puesto el mp4 y
-- `title_edited` el que espera render. Es lo primero que se lee en el feed y lo que el
-- modelo acierta menos, asi que tiene que poder cambiarse sin volver a analizar nada.
alter table public.clips add column if not exists title_edited    text;
alter table public.clips add column if not exists render_status   text not null default 'ready';
alter table public.clips add column if not exists render_error    text;
alter table public.clips add column if not exists version         int not null default 1;

-- Portada de las tarjetas de la galeria: la miniatura del momento (jpg 640x360, ~30 kB)
-- vive en el mismo bucket que el mp4. `video_date` es la fecha del directo (AAAAMMDD):
-- con todos los directos del canal titulados igual, es lo que distingue una tarjeta de
-- otra.
alter table public.clips add column if not exists poster_path text;
alter table public.clips add column if not exists video_date  text;

-- Ficha para volver a quemar el clip en una maquina que no hizo el analisis: de que VOD
-- sale, en que segundos, con que titulo y donde estan las webcams. La base local vive en
-- `backend/data/` y no se versiona, asi que un contenedor recien clonado (el turno
-- programado) no sabe nada del momento; con esto lo reconstruye y el render sigue el
-- camino de siempre. La clave anon no la puede escribir: no esta entre las columnas que
-- se le conceden mas abajo.
alter table public.clips add column if not exists render_spec jsonb;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'clips_render_status_check'
  ) then
    alter table public.clips add constraint clips_render_status_check
      check (render_status in ('ready', 'rerender_queued', 'rendering', 'error'));
  end if;
end $$;

create index if not exists clips_rerender_idx
  on public.clips (render_status, created_at) where render_status = 'rerender_queued';

create index if not exists clips_request_idx on public.clips (request_id);
create index if not exists clips_created_idx on public.clips (created_at desc);

-- --------------------------------------------------------- updated_at automatico
create or replace function public.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists clip_requests_touch on public.clip_requests;
create trigger clip_requests_touch before update on public.clip_requests
  for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------------- realtime
-- Realtime solo emite lo que este en esta publicacion. `full` hace que los UPDATE
-- lleguen con la fila entera y no solo con la clave, que es lo que necesita la
-- interfaz para pintar el progreso sin volver a consultar.
alter table public.clip_requests replica identity full;
alter table public.clips         replica identity full;

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime' and tablename = 'clip_requests'
  ) then
    alter publication supabase_realtime add table public.clip_requests;
  end if;
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime' and tablename = 'clips'
  ) then
    alter publication supabase_realtime add table public.clips;
  end if;
end $$;

-- ----------------------------------------------------------------------- RLS
-- La interfaz no tiene login: entra con la clave anon, que es publica por
-- definicion (va en el JavaScript). Asi que estas politicas son el limite real de lo
-- que puede hacer cualquiera que tenga la URL del proyecto: encolar peticiones,
-- cancelarlas y mirar los clips. Nada de borrar ni de reescribir resultados.
--
-- Si la interfaz se publica en una URL que pueda encontrar alguien mas, conviene
-- meterle Supabase Auth y cambiar `to anon` por `to authenticated`.
alter table public.clip_requests enable row level security;
alter table public.clips         enable row level security;

drop policy if exists "anon lee la cola" on public.clip_requests;
create policy "anon lee la cola" on public.clip_requests
  for select to anon, authenticated using (true);

drop policy if exists "anon encola" on public.clip_requests;
create policy "anon encola" on public.clip_requests
  for insert to anon, authenticated
  with check (
    status = 'queued'
    and clips between 1 and 10
    and length(url) between 12 and 400
    and url ~ '^https?://'
    -- La frase es libre, pero acotada: es texto que acaba dentro de un prompt de LLM.
    and (prompt is null or length(prompt) <= 300)
  );

drop policy if exists "anon cancela lo que aun no ha empezado" on public.clip_requests;
create policy "anon cancela lo que aun no ha empezado" on public.clip_requests
  for update to anon, authenticated
  using (status = 'queued')
  with check (status = 'canceled');

-- Lo que se edita del clip -titulo, subtitulos y sonido- es lo unico que la interfaz
-- puede escribir. RLS no distingue columnas, asi que la restriccion de verdad son los
-- permisos: se le quita el UPDATE entero y se le devuelve solo sobre esas columnas. Con
-- eso, un cliente con la clave anon no puede tocar la puntuacion ni la ruta del mp4, y
-- del titulo solo escribe el borrador (`title_edited`), no el que ya esta quemado.
revoke update on public.clips from anon, authenticated;
grant update (title_edited, captions_edited, sfx_edited, music_edited, render_status)
  on public.clips to anon, authenticated;

drop policy if exists "anon pide re-render con subtitulos nuevos" on public.clips;
drop policy if exists "anon pide re-render" on public.clips;
create policy "anon pide re-render" on public.clips
  for update to anon, authenticated
  using (render_status in ('ready', 'error'))
  with check (render_status = 'rerender_queued');

drop policy if exists "anon lee los clips" on public.clips;
create policy "anon lee los clips" on public.clips
  for select to anon, authenticated using (true);

-- El backend entra con service_role, que se salta RLS: no necesita politicas.

-- ------------------------------------------------------------------- storage
-- Bucket privado: los mp4 se sirven con URLs firmadas que caducan, no con enlaces
-- eternos. La interfaz las pide con la clave anon, asi que hace falta darle lectura
-- sobre los objetos del bucket. Los jpg son las portadas de las tarjetas, que viven al
-- lado de su mp4.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('clips', 'clips', false, 209715200, array['video/mp4', 'image/jpeg'])
on conflict (id) do update
  set file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "anon descarga clips" on storage.objects;
create policy "anon descarga clips" on storage.objects
  for select to anon, authenticated using (bucket_id = 'clips');
