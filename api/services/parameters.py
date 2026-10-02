"""Parâmetros da instalação editáveis na tela do superusuário.

Antes, mudar um bucket era editar o `.env` do servidor e reiniciar a API. Os
parâmetros daqui são os que mudam com o uso — buckets de imagens, onde
publicar modelos, validade da URL do agente — e moram em
`installation_setting`, lidos com cache curto.

Duas regras decidem o valor de cada um, nesta ordem:

1. **Variável de ambiente vence.** Se `VISIONYARD_<CHAVE>` está definida (no
   ambiente ou no `.env`), é ela, e a tela mostra o campo travado. Um deploy
   declarativo não pode ser desfeito por um clique — e quem trava é quem
   escreveu a variável, que é quem pode destravar.
2. Senão, o que a tela gravou; senão, o default de `config.py`.

O que NÃO está aqui, de propósito: DSN, cookie, proxies e `import_root`, que
são cercas de segurança ou decidem como o processo sobe (um `import_root`
editável na tela reabriria o oráculo de filesystem que ele existe para
fechar), e a credencial do GCS — ver `015_installation_setting.sql`.

O cache é por processo e vale `CACHE_S`: a API e o `api.worker` são processos
diferentes, e o que um grava o outro enxerga dentro desse prazo, sem restart.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from starlette.concurrency import run_in_threadpool

from .. import db, storage
from ..audit import audit
from ..auth import Principal
from ..config import settings

CACHE_S = 10.0

# Regra de nome de bucket do GCS, sem o caso com ponto-de-domínio: 3 a 63,
# minúsculas, dígito, hífen, sublinhado e ponto, começando e terminando em
# letra ou dígito. Recusar aqui é o que evita "nenhuma imagem encontrada" por
# um espaço colado no fim.
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]")
SEGMENTO_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _invalido(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, msg)


def _bucket(v: Any) -> str:
    if not isinstance(v, str):
        raise _invalido("informe o nome do bucket como texto")
    v = v.strip().removeprefix("gs://").strip("/")
    if not BUCKET_RE.fullmatch(v):
        raise _invalido(f"'{v}' não é um nome de bucket válido")
    return v


def _buckets(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise _invalido("informe uma lista de buckets")
    nomes = list(dict.fromkeys(_bucket(b) for b in v))
    if len(nomes) > 50:
        raise _invalido("no máximo 50 buckets")
    return nomes


def _prefixo(v: Any) -> str:
    """Pasta dentro do bucket de modelos. Sem `..`: o caminho de publicação é
    montado a partir daqui, e um `..` o tiraria da pasta do projeto."""
    if not isinstance(v, str):
        raise _invalido("informe o prefixo como texto")
    v = v.strip().strip("/")
    partes = v.split("/")
    if not v or any(not SEGMENTO_RE.fullmatch(p) or p in (".", "..")
                    for p in partes):
        raise _invalido(f"'{v}' não é um prefixo válido (pastas separadas por "
                        f"/, sem . nem ..)")
    return v


def _inteiro(minimo: int, maximo: int) -> Callable[[Any], int]:
    def validar(v: Any) -> int:
        # bool é int em Python: `true` viraria 1 minuto sem esta guarda.
        if isinstance(v, bool) or not isinstance(v, int):
            raise _invalido("informe um número inteiro")
        if not minimo <= v <= maximo:
            raise _invalido(f"entre {minimo} e {maximo}")
        return v
    return validar


@dataclass(frozen=True)
class Parametro:
    chave: str
    rotulo: str
    ajuda: str
    tipo: str               # "lista" | "texto" | "inteiro" — o que a tela desenha
    validar: Callable[[Any], Any]


PARAMETROS: dict[str, Parametro] = {p.chave: p for p in (
    Parametro("buckets", "Buckets de imagens",
              "Os buckets que a instalação enxerga, um por linha. É também a "
              "cerca da navegação: bucket fora da lista não é listado.",
              "lista", _buckets),
    Parametro("models_bucket", "Bucket de modelos",
              "Onde o treino publica o peso e o export publica o build. Sem "
              "ele, treino e export falham ao publicar.",
              "texto", _bucket),
    Parametro("models_prefix", "Prefixo dos modelos",
              "Pasta dentro do bucket de modelos.", "texto", _prefixo),
    Parametro("agent_url_minutes", "Validade da URL do agente (min)",
              "Quanto vale a URL assinada que a máquina recebe para baixar ou "
              "publicar. Ela pede na hora de usar, então só precisa cobrir a "
              "transferência de um arquivo.",
              "inteiro", _inteiro(5, 720)),
)}

_cache: dict[str, Any] | None = None
_lido_em = 0.0


def esquecer() -> None:
    """Descarta o cache deste processo — quem grava chama, para a própria
    resposta já refletir a mudança."""
    global _cache
    _cache = None


def travados() -> set[str]:
    """Os parâmetros que o ambiente definiu. `model_fields_set` traz o que o
    pydantic-settings leu do ambiente ou do `.env`, e não o que é default."""
    return set(settings.model_fields_set) & PARAMETROS.keys()


def _fresco() -> dict[str, Any] | None:
    if _cache is not None and time.monotonic() - _lido_em < CACHE_S:
        return _cache
    return None


async def _salvos(conn: db.Conn) -> dict[str, Any]:
    global _cache, _lido_em
    rows = await db.fetch_all(
        "SELECT key, value FROM installation_setting", conn=conn)
    _cache = {r["key"]: r["value"] for r in rows}
    _lido_em = time.monotonic()
    return _cache


async def valor(chave: str, conn: db.Conn | None = None) -> Any:
    """O valor que vale agora.

    Quem já tem uma conexão na mão passa `conn`: com o cache vencido, sem ela
    esta função tomaria uma segunda vaga do pool — e dez fluxos fazendo isso ao
    mesmo tempo travam o pool inteiro (ver o CLAUDE.md)."""
    if chave in travados():
        return getattr(settings, chave)
    salvos = _fresco()
    if salvos is None:
        async with db.conexao_ou(conn) as c:
            salvos = await _salvos(c)
    return salvos[chave] if chave in salvos else getattr(settings, chave)


def _linha(p: Parametro, salvo: dict | None, trav: set[str]) -> dict[str, Any]:
    if p.chave in trav:
        origem, v = "ambiente", getattr(settings, p.chave)
    elif salvo is not None:
        origem, v = "tela", salvo["value"]
    else:
        origem, v = "padrão", getattr(settings, p.chave)
    return {"chave": p.chave, "rotulo": p.rotulo, "ajuda": p.ajuda,
            "tipo": p.tipo, "valor": v, "origem": origem,
            "variavel": f"VISIONYARD_{p.chave.upper()}",
            "alterado_em": salvo["updated_at"] if salvo else None,
            "alterado_por": salvo["quem"] if salvo else None}


async def _linhas_salvas(conn: db.Conn) -> dict[str, dict]:
    rows = await db.fetch_all(
        "SELECT s.key, s.value, s.updated_at, u.display_name AS quem"
        "  FROM installation_setting s"
        "  LEFT JOIN app_user u ON u.id = s.updated_by", conn=conn)
    return {r["key"]: r for r in rows}


async def listar() -> list[dict[str, Any]]:
    async with db.connection() as conn:
        salvos = await _linhas_salvas(conn)
    trav = travados()
    return [_linha(p, salvos.get(p.chave), trav) for p in PARAMETROS.values()]


def _editavel(chave: str) -> Parametro:
    p = PARAMETROS.get(chave)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parâmetro desconhecido")
    if chave in travados():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"definido por VISIONYARD_{chave.upper()} no ambiente do servidor "
            f"— mude lá, ou retire a variável para editar aqui")
    return p


async def definir(admin: Principal, chave: str, bruto: Any,
                  ip: str | None) -> dict[str, Any]:
    p = _editavel(chave)
    novo = p.validar(bruto)
    async with db.connection() as conn, conn.transaction():
        # Lock consultivo pela chave, e não `FOR UPDATE` na linha: na primeira
        # gravação a linha não existe, não há o que travar, e duas gravações
        # simultâneas liam "nada antes" — a segunda auditava `before=None`
        # sobre o valor que a primeira acabara de gravar. A escrita em si o
        # ON CONFLICT já decidia; o lock é para a trilha dizer a verdade.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                           (f"parameter:{chave}",))
        antes = await db.fetch_one(
            "SELECT value FROM installation_setting WHERE key = %s",
            (chave,), conn=conn)
        await db.execute(
            "INSERT INTO installation_setting (key, value, updated_by)"
            " VALUES (%s, %s::jsonb, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
            "   updated_at = now(), updated_by = EXCLUDED.updated_by",
            (chave, json.dumps(novo), admin.id), conn=conn)
        # `entity_id` é uuid e a chave daqui é texto: ela vai no corpo.
        await audit(admin, "parameter.set", "installation_setting",
                    before=({"chave": chave, "valor": antes["value"]}
                            if antes else None),
                    after={"chave": chave, "valor": novo}, ip=ip, conn=conn)
        salvos = await _linhas_salvas(conn)
    esquecer()
    return _linha(p, salvos.get(chave), travados())


async def restaurar(admin: Principal, chave: str,
                    ip: str | None) -> dict[str, Any]:
    """Volta ao default de `config.py`, apagando o que a tela gravou."""
    p = _editavel(chave)
    async with db.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                           (f"parameter:{chave}",))
        antes = await db.fetch_one(
            "DELETE FROM installation_setting WHERE key = %s RETURNING value",
            (chave,), conn=conn)
        if antes is not None:
            await audit(admin, "parameter.reset", "installation_setting",
                        before={"chave": chave, "valor": antes["value"]},
                        ip=ip, conn=conn)
    esquecer()
    return _linha(p, None, travados())


async def diagnosticar() -> dict[str, Any]:
    """Testa a credencial contra os buckets que valem agora. Lê os valores
    antes e consulta o GCS depois, sem conexão nenhuma na mão: a consulta é
    rede, e pode demorar."""
    buckets = await valor("buckets")
    modelos = await valor("models_bucket")
    return await run_in_threadpool(storage.diagnosticar, buckets, modelos)
