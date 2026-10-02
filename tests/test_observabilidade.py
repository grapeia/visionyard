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


async def test_codigo_com_quebra_no_fim_e_descartado(client):
    """`$` casa antes de um `\\n` final: `abc\\n` passava pela validação e ia
    inteiro para a linha de log e para o cabeçalho da resposta."""
    r = await client.get("/healthz", headers={"X-Request-Id": "abc\n"})
    assert r.headers["x-request-id"] != "abc\n"


def test_nivel_de_log_em_minusculas_nao_derruba_a_subida(monkeypatch):
    """`logging` só aceita o nome em maiúsculas, e as outras variáveis de
    ambiente da instalação são minúsculas — `VISIONYARD_LOG_LEVEL=debug` é o
    que um operador escreve, e derrubava a API no `lifespan`."""
    from api import logs
    from api.config import settings

    monkeypatch.setattr(settings, "log_level", "debug")
    try:
        logs.configurar()
        assert logging.getLogger().level == logging.DEBUG
    finally:
        monkeypatch.setattr(settings, "log_level", "INFO")
        logs.configurar()


def _registro(nivel: int, msg: str, exc: bool = False) -> logging.LogRecord:
    info = None
    if exc:
        try:
            raise ValueError("estourou")
        except ValueError:
            import sys
            info = sys.exc_info()
    r = logging.LogRecord("visionyard.x", nivel, __file__, 1, msg, (), info)
    r.request_id = "abc123"
    return r


def test_formato_json_e_o_que_o_cloud_logging_le():
    """No Cloud Run tudo o que sai no stderr vira ERROR se a linha não disser
    a severidade — e o Python escreve no stderr. Em JSON, `severity` e
    `message` são os campos que o coletor reconhece."""
    import json

    from api import logs

    linha = json.loads(logs.FormatoJson().format(
        _registro(logging.WARNING, "devagar %s")))
    assert linha["severity"] == "WARNING"
    assert linha["message"] == "devagar %s"
    assert linha["request_id"] == "abc123"
    assert linha["logger"] == "visionyard.x"
    assert json.loads(logs.FormatoJson().format(
        _registro(logging.INFO, "x")))["severity"] == "INFO"


def test_formato_json_leva_o_traceback_na_mesma_linha():
    """Uma linha por evento: o traceback em linhas soltas viraria N entradas
    sem código nenhum, cada uma com severidade padrão."""
    import json

    from api import logs

    saida = logs.FormatoJson().format(_registro(logging.ERROR, "falhou", True))
    assert "\n" not in saida
    linha = json.loads(saida)
    assert linha["severity"] == "ERROR"
    assert "ValueError: estourou" in linha["message"]


def test_formato_json_ligado_pela_configuracao(monkeypatch):
    from api import logs
    from api.config import settings

    monkeypatch.setattr(settings, "log_format", "json")
    try:
        logs.configurar()
        raiz = logging.getLogger()
        assert any(isinstance(h.formatter, logs.FormatoJson)
                   for h in raiz.handlers)
        # o uvicorn tem handler próprio, em texto, no stderr: sem isto cada
        # linha dele chegava ao Cloud Logging como ERROR
        assert logging.getLogger("uvicorn.error").handlers == []
        assert logging.getLogger("uvicorn.error").propagate is True
        # O de acesso fica mudo: o middleware já escreve uma linha por
        # requisição, com o código. Propagado para o root ele voltava mesmo
        # com `--no-access-log` — o uvicorn decide se loga pelo
        # `hasHandlers()`, que enxerga o handler do root.
        acesso = logging.getLogger("uvicorn.access")
        assert acesso.handlers == [] and acesso.propagate is False
        assert not acesso.hasHandlers()
    finally:
        monkeypatch.setattr(settings, "log_format", "texto")
        logs.configurar()
