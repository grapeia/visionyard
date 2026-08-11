"""Runner de migrations: aplica db/migrations/NNN_*.sql em ordem, uma vez cada.

Sem Alembic de propósito — o schema é SQL escrito à mão (sem ORM), então um
runner de 60 linhas cobre o caso e não adiciona uma camada que precisaria ser
aprendida e mantida.

Cada migration roda dentro de uma transação junto com o registro em
schema_migrations: ou aplica e registra, ou não faz nada. O checksum detecta
edição de migration já aplicada, que é a forma clássica de dois ambientes
divergirem silenciosamente.

    python -m db.migrate            # aplica pendentes
    python -m db.migrate --status   # só lista
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import psycopg

from api.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    duration_ms integer
);
"""


def discover() -> list[tuple[str, Path]]:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    return [(f.name.split("_")[0], f) for f in files]


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="só lista, não aplica")
    args = ap.parse_args()

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(BOOTSTRAP)
        applied: dict[str, str] = dict(conn.execute(
            "SELECT version, checksum FROM schema_migrations").fetchall())

        pending = []
        for version, path in discover():
            cs = checksum(path)
            if version not in applied:
                pending.append((version, path, cs))
                print(f"  pendente  {path.name}")
            elif applied[version] != cs:
                print(f"  ALTERADA  {path.name} — já aplicada com checksum "
                      f"{applied[version]}, arquivo agora é {cs}.\n"
                      f"            Migration aplicada não se edita: "
                      f"crie a próxima.", file=sys.stderr)
                return 1
            else:
                print(f"  ok        {path.name}")

        if args.status:
            return 0
        if not pending:
            print("nada a aplicar")
            return 0

        for version, path, cs in pending:
            print(f"aplicando {path.name} ...", end=" ", flush=True)
            # transação explícita: DDL + registro atômicos
            with psycopg.connect(settings.database_url) as tx:
                t0 = time.monotonic()
                tx.execute(path.read_text())
                ms = (time.monotonic() - t0) * 1000
                tx.execute(
                    "INSERT INTO schema_migrations (version, checksum,"
                    " duration_ms) VALUES (%s, %s, %s)", (version, cs, int(ms)))
                tx.commit()
            print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
