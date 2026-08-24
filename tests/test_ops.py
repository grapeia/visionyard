"""Sondas de operação: estar vivo e estar pronto são perguntas diferentes.

Responder as duas com a mesma checagem faz do banco um interruptor do
processo: uma oscilação do Postgres derruba a sonda de vida e o orquestrador
reinicia uma API que estava perfeitamente sã — e reiniciar não traz o banco de
volta, só apaga o que ela ainda conseguia servir.
"""
from __future__ import annotations

import pytest

from api import db

pytestmark = pytest.mark.anyio


async def test_vivo_nao_depende_do_banco(client, monkeypatch):
    async def sem_banco(*a, **kw):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(db, "fetch_one", sem_banco)
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_pronto_depende_do_banco(client):
    r = await client.get("/readyz")
    assert r.status_code == 200


async def test_pronto_recusa_quando_o_banco_nao_responde(client, monkeypatch):
    """503 e não 500: é indisponibilidade temporária, e é o que tira a réplica
    do balanceador sem derrubá-la."""
    async def sem_banco(*a, **kw):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(db, "fetch_one", sem_banco)
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert "banco fora do ar" not in r.text     # detalhe fica no log


# --------------------------------------------------------------------------- #
# Cache do front: um grafo de módulos sem build só serve inteiro
# --------------------------------------------------------------------------- #

async def test_a_casca_revalida_sempre(client):
    """Ela é quem diz qual é o módulo de entrada; velha em cache, quebra tudo."""
    r = await client.get("/")
    assert r.headers["cache-control"] == "no-cache"


async def test_modulo_do_front_revalida_sempre(client):
    """O `?v=` está só no ponto de entrada — os 15 módulos que ele importa não
    têm como carregá-lo, e sem diretriz de cache o navegador aplica frescor
    heurístico. Um deploy que mexe em `nucleo.js` entregaria o `app.js` novo
    sobre o `nucleo.js` velho, e a página morreria no import.
    """
    for arquivo in ("app.js", "nucleo.js", "app.css"):
        r = await client.get(f"/static/{arquivo}")
        assert r.status_code == 200, arquivo
        assert r.headers.get("cache-control") == "no-cache", arquivo
