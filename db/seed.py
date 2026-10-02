"""Seed: cria o primeiro superusuário da instalação.

Necessário porque não existe auto-cadastro — uma tela de login vazia não teria
como produzir o primeiro usuário. Rode uma vez, depois use a API pra criar os
demais.

    python -m db.seed --email voce@empresa.com --name "Seu Nome"
    python -m db.seed --email voce@empresa.com --name "Seu Nome" --password ...

Sem --password, a senha é gerada e impressa uma única vez. É o caminho
recomendado numa máquina: senha em argumento de linha de comando fica no
histórico do shell e é visível em `ps` para qualquer usuário dela.

Num job do Cloud Run é o contrário: o que ele imprime vai para o Cloud Logging,
e a senha gerada ficaria lá, legível a quem lê log do projeto. Ali a senha vem
de `VISIONYARD_SEED_PASSWORD` (que o job recebe do Secret Manager) e não é
impressa. Ver deploy/gcp/README.md.

Idempotente: se o email já existe, não altera nada (use --reset-password pra
redefinir a senha de um seed perdido).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys

import psycopg
from psycopg.rows import dict_row

from api.config import settings
from api.security import hash_password

ALPHABET = string.ascii_letters + string.digits


def generate_password(n: int = 20) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def main() -> int:
    ap = argparse.ArgumentParser(description="cria o primeiro superusuário")
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--password", help="omita para gerar uma senha forte")
    ap.add_argument("--reset-password", action="store_true",
                    help="redefine a senha se o usuário já existir")
    args = ap.parse_args()

    email = args.email.strip().lower()
    informada = args.password or os.environ.get("VISIONYARD_SEED_PASSWORD")
    if informada and len(informada) < settings.password_min_length:
        print(f"senha precisa de ao menos {settings.password_min_length} "
              f"caracteres", file=sys.stderr)
        return 1
    password = informada or generate_password()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        existing = conn.execute(
            "SELECT id, is_superuser FROM app_user WHERE lower(email) = %s",
            (email,)).fetchone()

        if existing and not args.reset_password:
            print(f"usuário {email} já existe (id={existing['id']}) — "
                  f"nada a fazer.\nUse --reset-password para redefinir a senha.")
            return 0

        if existing:
            # Só a senha. Antes este UPDATE também fazia status='active' e
            # is_superuser=true, o que transformava um comando de manutenção em
            # escalação de privilégio: rodá-lo contra o email de um usuário
            # comum (ou desativado) o promovia a superusuário da instalação,
            # sem nada disso aparecer no --help nem no audit_log.
            conn.execute(
                "UPDATE app_user SET password_hash = %s WHERE id = %s",
                (hash_password(password), existing["id"]))
            # Redefinir senha derruba TODO acesso anterior. Só revogar sessão
            # deixava o atacante dentro: um token de API criado por ele segue
            # autenticando (e sem expires_at nunca expira), e um convite de
            # redefinição pendente permite trocar a senha de novo.
            conn.execute("UPDATE user_session SET revoked_at = now()"
                         " WHERE user_id = %s AND revoked_at IS NULL",
                         (existing["id"],))
            conn.execute("UPDATE api_token SET revoked_at = now()"
                         " WHERE user_id = %s AND revoked_at IS NULL",
                         (existing["id"],))
            conn.execute("UPDATE user_invite SET revoked_at = now()"
                         " WHERE user_id = %s AND used_at IS NULL"
                         "   AND revoked_at IS NULL", (existing["id"],))
            conn.execute(
                "INSERT INTO audit_log (actor_type, actor_label, action,"
                " entity_type, entity_id, note)"
                " VALUES ('system','seed','user.password.reset','app_user',"
                " %s,'via db.seed')", (existing["id"],))
            user_id = existing["id"]
            action = "senha redefinida"
        else:
            row = conn.execute(
                "INSERT INTO app_user (email, display_name, password_hash,"
                " status, is_superuser) VALUES (%s,%s,%s,'active',true)"
                " RETURNING id", (email, args.name, hash_password(password))
            ).fetchone()
            if row is None:
                raise RuntimeError("INSERT em app_user não devolveu id")
            conn.execute(
                "INSERT INTO audit_log (actor_type, actor_label, action,"
                " entity_type, entity_id, after, note)"
                " VALUES ('system','seed','user.create','app_user',%s,"
                " %s::jsonb,'primeiro superusuário via db.seed')",
                (row["id"], json.dumps({"email": email, "superuser": True})))
            user_id = row["id"]
            action = "criado"
        conn.commit()

    print(f"\n  superusuário {action}: {email}  (id={user_id})")
    if not informada:
        print(f"  senha: {password}")
        print("\n  Anote agora — não é possível recuperá-la depois.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
