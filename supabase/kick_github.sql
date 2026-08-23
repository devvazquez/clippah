-- OPCIONAL: que el motor arranque al momento en vez de esperar al cron.
--
-- El workflow `.github/workflows/clipper.yml` mira la cola cada 5 minutos. Eso ya
-- funciona, pero se notan los minutos de espera: le das a "Guardar en la cola" en el
-- movil y no pasa nada visible durante un rato. Esto lo arregla: cuando se encola algo,
-- Postgres llama a la API de GitHub y el turno empieza en segundos.
--
-- Y si el repositorio es PRIVADO, esto no es opcional: alli los minutos de Actions estan
-- limitados (2.000/mes) y cada arranque cuenta como uno entero, asi que sondear cada 5
-- minutos se come el mes en sondeos. Con esto puesto se puede borrar el bloque
-- `schedule:` del workflow y el motor solo arranca cuando hay trabajo de verdad.
--
-- ANTES DE EJECUTARLO hace falta un token de GitHub:
--   1. https://github.com/settings/personal-access-tokens/new (fine-grained)
--   2. Repository access -> Only select repositories -> el repo de clipper
--   3. Permissions -> Repository permissions -> Contents: Read and write
--      (es lo que pide la API de `repository_dispatch`; no da acceso a nada mas)
--   4. Copia el token y ponlo abajo, en el INSERT.
--
-- El token queda guardado en una tabla de la base. La tabla tiene RLS y no tiene ninguna
-- politica, asi que la clave anon -la que va en el JavaScript de la interfaz- no puede
-- leerla; solo `service_role` y el propietario de la base. Aun asi es una clave con
-- escritura sobre un repositorio: si te incomoda, quedate con el cron y no ejecutes esto.

create extension if not exists pg_net;

-- ------------------------------------------------------------------ la configuracion
create table if not exists public.github_kick (
  id         int primary key default 1 check (id = 1),
  repo       text not null,          -- "usuario/repositorio"
  token      text not null,
  event_type text not null default 'clipper',
  -- Para no disparar veinte turnos si se encolan veinte cosas seguidas.
  last_kick  timestamptz
);

alter table public.github_kick enable row level security;
revoke all on public.github_kick from anon, authenticated;

-- >>> LO UNICO QUE HAY QUE EDITAR <<<
insert into public.github_kick (id, repo, token)
values (1, 'devvazquez/clippah', 'ghp_PON_AQUI_TU_TOKEN')
on conflict (id) do update
  set repo = excluded.repo, token = excluded.token;

-- ------------------------------------------------------------------- el disparador
create or replace function public.kick_github() returns trigger
language plpgsql
security definer
set search_path = public, net
as $$
declare
  cfg public.github_kick;
begin
  select * into cfg from public.github_kick where id = 1;
  if cfg is null or cfg.token is null or cfg.token = '' then
    return null;
  end if;
  -- Un turno tarda mas de un minuto en arrancar de todas formas: dos avisos seguidos no
  -- adelantan nada y cada uno cuesta un arranque de Actions.
  if cfg.last_kick is not null and now() - cfg.last_kick < interval '60 seconds' then
    return null;
  end if;
  update public.github_kick set last_kick = now() where id = 1;

  perform net.http_post(
    url := 'https://api.github.com/repos/' || cfg.repo || '/dispatches',
    body := jsonb_build_object('event_type', cfg.event_type),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || cfg.token,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      -- La API de GitHub rechaza las peticiones sin User-Agent.
      'User-Agent', 'clipper-kick',
      'Content-Type', 'application/json'
    )
  );
  return null;
end
$$;

-- Un analisis nuevo.
drop trigger if exists kick_on_request on public.clip_requests;
create trigger kick_on_request
  after insert on public.clip_requests
  for each row when (new.status = 'queued')
  execute function public.kick_github();

-- Un clip que se ha editado desde la interfaz y hay que rehacer.
drop trigger if exists kick_on_rerender on public.clips;
create trigger kick_on_rerender
  after update of render_status on public.clips
  for each row when (new.render_status = 'rerender_queued')
  execute function public.kick_github();

-- ------------------------------------------------------------------- comprobarlo
-- Despues de ejecutar esto, encola un directo desde la interfaz y mira:
--
--   select id, status_code, created from net._http_response order by created desc limit 5;
--
-- Un 204 es que GitHub acepto el aviso (`repository_dispatch` no devuelve cuerpo). Un 401
-- o un 403 es el token: o esta mal copiado, o no tiene "Contents: Read and write", o no
-- incluye este repositorio. Un 404 suele ser el nombre del repo mal escrito.
