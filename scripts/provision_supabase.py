"""Crea el proyecto de Supabase y lo deja listo, con la Management API.

Hace lo que hay que hacer una vez: crear el proyecto, aplicar `supabase/schema.sql`
(tablas, RLS, Realtime, bucket), leer las claves y escribirlas donde van — la service_role
en `backend/.env` y la anon en `frontend/.env.local`.

    export SUPABASE_ACCESS_TOKEN=sbp_...          # Account -> Access Tokens
    python scripts/provision_supabase.py --name clipper --region eu-west-3

Si el proyecto ya existe, `--ref <ref>` se salta la creacion y solo aplica el esquema y
reescribe las claves, asi que se puede repetir sin miedo.

El token es de cuenta entera: no se escribe en ningun fichero ni se imprime.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "supabase" / "schema.sql"
BACKEND_ENV = ROOT / "backend" / ".env"
FRONTEND_ENV = ROOT / "frontend" / ".env.local"
API = "https://api.supabase.com/v1"
# Un proyecto nuevo tarda un par de minutos en levantar la base.
READY_TIMEOUT_S = 600


def api(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(60.0, connect=15.0),
    )


def die(message: str) -> None:
    print(f"\n{message}")
    raise SystemExit(1)


def check(response: httpx.Response, what: str) -> object:
    if response.status_code >= 400:
        die(f"{what} fallo ({response.status_code}): {response.text[:400]}")
    return response.json() if response.content else None


def wait_ready(client: httpx.Client, ref: str) -> None:
    print("  esperando a que la base arranque", end="", flush=True)
    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        project = check(client.get(f"/projects/{ref}"), "consultar el proyecto")
        status = str(project.get("status") or "")          # type: ignore[union-attr]
        if status == "ACTIVE_HEALTHY":
            print(" listo")
            return
        if status in ("INIT_FAILED", "REMOVED", "RESTORE_FAILED"):
            die(f"el proyecto quedo en {status}")
        print(".", end="", flush=True)
        time.sleep(5)
    die("el proyecto no llego a ACTIVE_HEALTHY en 10 minutos")


def apply_schema(client: httpx.Client, ref: str) -> None:
    sql = SCHEMA.read_text()
    print(f"  aplicando {SCHEMA.relative_to(ROOT)} ({len(sql.splitlines())} lineas)")
    # La base recien creada puede tardar unos segundos mas en aceptar consultas aunque el
    # proyecto ya se declare sano.
    for attempt in range(6):
        r = client.post(f"/projects/{ref}/database/query", json={"query": sql})
        if r.status_code < 400:
            return
        if attempt == 5:
            die(f"el esquema fallo ({r.status_code}): {r.text[:500]}")
        print(f"  reintento {attempt + 1}/5 ({r.status_code})")
        time.sleep(10)


def api_keys(client: httpx.Client, ref: str) -> tuple[str, str]:
    keys = check(client.get(f"/projects/{ref}/api-keys", params={"reveal": "true"}),
                 "leer las claves")
    anon = service = ""
    for key in keys:                                       # type: ignore[union-attr]
        name = str(key.get("name") or "")
        if name == "anon":
            anon = str(key.get("api_key") or "")
        elif name == "service_role":
            service = str(key.get("api_key") or "")
    if not (anon and service):
        die("la respuesta no traia las claves anon/service_role")
    return anon, service


def write_env(path: Path, values: dict[str, str], *, header: str) -> None:
    """Actualiza las claves que toca y deja el resto del fichero como estaba."""
    lines = path.read_text().splitlines() if path.exists() else []
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                break
        else:
            if header and header not in lines:
                lines += ["", header]
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    print(f"  escrito {path.relative_to(ROOT)} ({', '.join(values)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="clipper", help="nombre del proyecto")
    parser.add_argument("--region", default="eu-west-3", help="region (eu-west-3 = Paris)")
    parser.add_argument("--org", default="", help="slug de la organizacion (si tienes varias)")
    parser.add_argument("--ref", default="", help="usa un proyecto que ya existe")
    args = parser.parse_args()

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not token:
        die("Falta SUPABASE_ACCESS_TOKEN (Supabase -> Account -> Access Tokens)")
    if not SCHEMA.exists():
        die(f"No encuentro {SCHEMA}")

    with api(token) as client:
        ref = args.ref
        if ref:
            project = check(client.get(f"/projects/{ref}"), "consultar el proyecto")
            print(f"Proyecto existente: {project['name']} ({ref})")  # type: ignore[index]
        else:
            orgs = check(client.get("/organizations"), "listar organizaciones")
            if not orgs:
                die("La cuenta no tiene ninguna organizacion")
            slug = args.org or str(orgs[0]["slug"])        # type: ignore[index]
            print(f"Organizacion: {slug}")
            # La contrasena de la base no hace falta para nada de lo que hacemos (todo va
            # por HTTPS), asi que se genera una fuerte y se deja en el dashboard.
            db_pass = secrets.token_urlsafe(24)
            print(f"Creando proyecto {args.name!r} en {args.region}")
            project = check(
                client.post("/projects", json={
                    "name": args.name, "organization_slug": slug,
                    "region": args.region, "db_pass": db_pass,
                }),
                "crear el proyecto",
            )
            ref = str(project["ref"])                       # type: ignore[index]
            print(f"  ref: {ref}")
            wait_ready(client, ref)

        apply_schema(client, ref)
        anon, service = api_keys(client, ref)

    url = f"https://{ref}.supabase.co"
    write_env(BACKEND_ENV, {"SUPABASE_URL": url, "SUPABASE_SERVICE_KEY": service},
              header="# --- Supabase ---")
    write_env(FRONTEND_ENV,
              {"NEXT_PUBLIC_SUPABASE_URL": url, "NEXT_PUBLIC_SUPABASE_ANON_KEY": anon},
              header="# --- Supabase ---")

    print(f"""
Proyecto listo: {url}
  anon         {anon[:12]}… ({len(anon)} caracteres)
  service_role {service[:12]}… ({len(service)} caracteres)

Siguiente:
  make backend         # arranca el worker de la cola
  make export          # construye la interfaz con la clave anon dentro
  python scripts/push_clips.py    # sube los clips que ya estan renderizados""")


if __name__ == "__main__":
    sys.exit(main())
