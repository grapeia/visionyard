"""Correlação por requisição: sem ela um 500 não se liga ao request que o causou.

O que estes testes fixam é o mínimo para diagnosticar em produção: toda
resposta carrega um código, o código volta no corpo do erro (é o que o usuário
tem para informar), e o `X-Request-Id` que vem do proxy é preservado — mas só
se for um código, e não texto que forjaria linha de log.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import HTTPException

from api.main import app

pytestmark = pytest.mark.anyio


async def _sem_relancar(fn):
    """Cliente que devolve o 500 em vez de relançar a exceção do app — é o que
    o navegador vê, e é isso que se quer afirmar."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as c:
            await fn(c)


def _rota_temporaria(path: str, handler):
    """Rota só para este teste — não há caminho que estoure de propósito na
    API, e é justamente o caminho não previsto que se quer exercitar."""
    app.add_api_route(path, handler, methods=["GET"], include_in_schema=False)
    return lambda: app.router.routes.pop(
        next(i for i, r in enumerate(app.router.routes)
             if getattr(r, "path", None) == path))


async def test_toda_resposta_leva_um_codigo(client):
    r = await client.get("/healthz")
    assert r.headers.get("x-request-id")


async def test_codigo_do_proxy_e_preservado(client):
    """Correlacionar entre o proxy e a API só funciona se o código atravessar."""
    r = await client.get("/healthz", headers={"X-Request-Id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


async def test_codigo_forjado_e_descartado(client):
    """O código entra em linha de log: aceitar texto com quebra de linha
    deixaria qualquer cliente escrever uma linha inteira de log."""
    r = await client.get("/healthz",
                         headers={"X-Request-Id": "abc\nERRO forjado"})
    devolvido = r.headers["x-request-id"]
    assert "\n" not in devolvido and devolvido != "abc\nERRO forjado"


async def test_erro_nao_previsto_vira_500_com_o_codigo():
    async def explode():
        raise RuntimeError("detalhe interno que não pode vazar")

    remover = _rota_temporaria("/v1/_estoura", explode)
    try:
        async def caso(c):
            r = await c.get("/v1/_estoura")
            assert r.status_code == 500
            corpo = r.json()
            # o código no corpo é o que o usuário informa ao suporte; o detalhe
            # do erro fica no log do servidor
            assert corpo["request_id"] == r.headers["x-request-id"]
            assert "detalhe interno" not in str(corpo)

        await _sem_relancar(caso)
    finally:
        remover()


async def test_erro_de_usuario_nao_vira_500(client):
    async def recusa():
        raise HTTPException(409, "projeto arquivado — só leitura")

    remover = _rota_temporaria("/v1/_recusa", recusa)
    try:
        r = await client.get("/v1/_recusa")
        assert r.status_code == 409
        assert r.json()["detail"] == "projeto arquivado — só leitura"
    finally:
        remover()


async def test_log_do_erro_carrega_o_codigo(caplog):
    """É o que liga a linha de log ao código que o usuário informou."""
    async def explode():
        raise RuntimeError("falha interna")

    remover = _rota_temporaria("/v1/_estoura2", explode)
    try:
        async def caso(c):
            with caplog.at_level(logging.ERROR):
                r = await c.get("/v1/_estoura2")
            registros = [x for x in caplog.records
                         if getattr(x, "request_id", None) == r.headers["x-request-id"]]
            assert registros, "nenhuma linha de log com o código da requisição"
            assert any(x.exc_info for x in registros), "traceback não foi ao log"

        await _sem_relancar(caso)
    finally:
        remover()
