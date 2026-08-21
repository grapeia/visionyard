"""O que cada rota devolve é declarado, e não a linha do banco.

Sem `response_model`, coluna acrescentada a um `SELECT` viaja para o navegador
sem ninguém decidir e o OpenAPI não descreve saída nenhuma. Este teste é a
guarda: rota nova sem contrato reprova aqui, e não meses depois, quando alguém
tentar gerar um cliente ou descobrir o que a API promete.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

# A porta do agente fica de fora de propósito: o que ela devolve é o payload do
# job, cuja descrição única é `contracts/jobs.py` — uma segunda em pydantic aqui
# seria a duplicação que aquele módulo existe para evitar. E `contracts/` não
# pode importar pydantic: o dispositivo de borda o carrega sem fastapi junto.
FORA = "/v1/agent"


async def test_toda_rota_declara_o_que_devolve(admin):
    doc = (await admin.get("/openapi.json")).json()
    sem_contrato = []
    for rota, metodos in doc["paths"].items():
        if rota.startswith(FORA):
            continue
        for metodo, spec in metodos.items():
            for codigo in ("200", "201"):
                corpo = (spec.get("responses", {}).get(codigo, {})
                         .get("content", {}).get("application/json", {}))
                if not corpo:
                    continue          # 204, ou resposta que não é JSON
                if "$ref" not in str(corpo.get("schema", {})):
                    sem_contrato.append(f"{metodo.upper()} {rota} → {codigo}")
    assert not sem_contrato, "rotas sem response_model:\n" + "\n".join(sem_contrato)
