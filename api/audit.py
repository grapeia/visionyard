"""Trilha de auditoria: quem fez o quê, em qual projeto, a partir de onde.

Módulo próprio, e não uma função dentro de `deps.py`, por dois motivos: não é
uma dependência do FastAPI (ninguém a injeta numa rota), e `auth.py` precisa
chamá-la de dentro das suas transações — importar `deps` de lá fecharia um
ciclo, já que `deps` importa `auth`.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import db

if TYPE_CHECKING:
    # Só para a anotação: importar `auth` de verdade aqui fecharia o ciclo que
    # este módulo existe para abrir.
    from .auth import Principal


async def audit(actor: Principal | None, action: str, entity_type: str,
                entity_id: str | None = None, *, project_id: str | None = None,
                before: dict | None = None, after: dict | None = None,
                ip: str | None = None, note: str | None = None,
                conn: db.Conn | None = None) -> None:
    """Registra em audit_log. actor=None significa ação do próprio sistema.

    Quem escreve dentro de uma transação passa a mesma `conn`: assim o fato e o
    seu registro comitam juntos, ou nenhum dos dois.
    """
    await db.execute(
        "INSERT INTO audit_log (actor_type, actor_id, actor_label, project_id,"
        " action, entity_type, entity_id, before, after, ip, note)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("user" if actor else "system",
         actor.id if actor else None,
         actor.email if actor else "system",
         project_id, action, entity_type, entity_id,
         json.dumps(before) if before else None,
         json.dumps(after) if after else None,
         ip, note),
        conn=conn)
