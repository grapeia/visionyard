"""Log com correlação por requisição.

Sem isto um 500 não se liga ao request que o causou: a saída dependia do
default do uvicorn, dois módulos usavam `logging` sem configuração nenhuma, e
o traceback de uma falha ficava sem nada que o amarrasse à tela em que o
usuário estava.

O mecanismo é um só: cada requisição ganha um código, que vai para uma
`ContextVar`, entra em TODA linha de log emitida durante ela (por um filtro no
root, não por quem chama o logger), volta no cabeçalho `X-Request-Id` e, quando
a resposta é 500, no corpo — é o que o usuário informa ao suporte.

O formato é uma linha de texto com o código no meio, ou — com
`VISIONYARD_LOG_FORMAT=json`, que é o do Cloud Run — um objeto JSON por linha,
com `severity` e `message`, que é o que o Cloud Logging lê. O que muda entre os
dois é o formatter, e só ele: o código entra pelo mesmo filtro.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from .config import settings

logger = logging.getLogger("visionyard")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Código vindo do proxy só é aceito se for código: ele entra em linha de log, e
# texto livre com quebra de linha deixaria qualquer cliente escrever uma linha
# inteira de log — inclusive uma que parecesse legítima.
# `\Z` e não `$`: `$` casa ANTES de uma quebra de linha final, então "abc\n"
# passava inteiro para a linha de log e para o cabeçalho da resposta.
_CODIGO_VALIDO = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")

# Rotas que não geram linha de acesso: o healthz é batido pelo orquestrador a
# cada poucos segundos e afogaria o resto.
_SEM_LOG = ("/healthz", "/readyz", "/static", "/favicon.ico")

FORMATO = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


class FormatoJson(logging.Formatter):
    """Um objeto por linha, nos campos que o Cloud Logging reconhece.

    `severity` é o que importa: no Cloud Run toda linha do stderr que não diz a
    própria severidade é classificada como ERROR, e o `logging` escreve no
    stderr — um alerta por taxa de erro dispararia com o log de acesso. O
    traceback vai DENTRO de `message`: em linhas soltas ele viraria uma entrada
    por linha, sem código de requisição e sem severidade.
    """

    def format(self, record: logging.LogRecord) -> str:
        mensagem = record.getMessage()
        if record.exc_info:
            mensagem = f"{mensagem}\n{self.formatException(record.exc_info)}"
        return json.dumps({
            "severity": record.levelname,
            "message": mensagem,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
        }, ensure_ascii=False)


class _InjetarCodigo(logging.Filter):
    """Põe o código da requisição em todo registro.

    Como filtro no handler do root e não em cada chamada: senão o `%(request_id)s`
    do formato estouraria em qualquer log emitido fora de requisição (o
    `db.migrate`, um script) e cada `logger.info` do repositório teria de
    lembrar de passar o campo.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


def configurar() -> None:
    """Configura o root uma vez. Idempotente: o `lifespan` roda a cada teste."""
    # `.upper()`: o `logging` só aceita o nome em maiúsculas, e as outras
    # variáveis da instalação são minúsculas — `VISIONYARD_LOG_LEVEL=debug` é o
    # que um operador escreve, e derrubava a subida inteira no `lifespan`.
    nivel = settings.log_level.upper()
    raiz = logging.getLogger()
    if not raiz.handlers:
        logging.basicConfig(level=nivel, format=FORMATO)
    em_json = settings.log_format == "json"
    for h in raiz.handlers:
        if not any(isinstance(f, _InjetarCodigo) for f in h.filters):
            h.addFilter(_InjetarCodigo())
        if em_json:
            h.setFormatter(FormatoJson())
        elif isinstance(h.formatter, FormatoJson):
            h.setFormatter(logging.Formatter(FORMATO))
    if em_json:
        # O uvicorn tem handler próprio, em texto e no stderr: sem passar pelo
        # root, cada linha dele chegaria ao Cloud Logging como ERROR.
        for nome in ("uvicorn", "uvicorn.error"):
            lg = logging.getLogger(nome)
            lg.handlers = []
            lg.propagate = True
        # O de acesso fica mudo, e não propagado: o middleware de
        # `correlacionar` já escreve uma linha por requisição, com o código. E
        # propagar o religaria mesmo com `--no-access-log`, porque o uvicorn
        # decide se loga pelo `hasHandlers()`, que enxerga o root.
        acesso = logging.getLogger("uvicorn.access")
        acesso.handlers = []
        acesso.propagate = False
    raiz.setLevel(nivel)


def codigo_atual() -> str:
    return _request_id.get()


async def correlacionar(
        request: Request,
        proximo: Callable[[Request], Awaitable[Response]]) -> Response:
    """Middleware: código, cronômetro e a rede embaixo de tudo.

    O `except` aqui é a rede que faltava: sem ele o erro não tratado subia até
    o uvicorn e virava um 500 sem corpo, com o traceback num lugar que não se
    liga a requisição nenhuma. Erro de usuário não chega aqui — `HTTPException`
    já virou resposta antes, na camada de dentro.
    """
    vindo = request.headers.get("x-request-id", "")
    codigo = vindo if _CODIGO_VALIDO.match(vindo) else uuid.uuid4().hex[:12]
    token = _request_id.set(codigo)
    inicio = time.monotonic()
    try:
        try:
            resposta = await proximo(request)
        except Exception:
            # O detalhe fica no log: mensagem de exceção cita caminho interno,
            # conta de serviço e SQL, e a resposta é lida por qualquer um.
            logger.exception("erro não tratado em %s %s", request.method,
                             request.url.path)
            resposta = JSONResponse(
                status_code=500,
                content={"detail": "erro interno — informe o código "
                                   f"{codigo} a quem administra a instalação",
                         "request_id": codigo})
        resposta.headers["X-Request-Id"] = codigo
        ms = (time.monotonic() - inicio) * 1000
        if not request.url.path.startswith(_SEM_LOG):
            logger.info("%s %s %s %.0fms", request.method, request.url.path,
                        resposta.status_code, ms)
        return resposta
    finally:
        _request_id.reset(token)
