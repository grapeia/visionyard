"""Contrato do payload de job: o que a API grava e o que o agente lê.

O payload é a segunda API do sistema, e a única que atravessa processo, máquina
e cadência de deploy — o controlador atualiza quando se faz deploy, o agente na
borda quando alguém vai até ele. Até este módulo existir, cada lado descrevia o
payload por conta própria (`{"weights_uri": ...}` no serviço,
`payload["weights_uri"]` no executor): renomear uma chave passava no `ruff`, no
`mypy` e na suíte inteira, e falhava horas depois como `KeyError` no
dispositivo, marcando o job como `failed` com uma mensagem que não dizia que o
contrato tinha quebrado.

Aqui a descrição é uma só. O produtor constrói a dataclass e grava
`como_dict()`; o executor lê com `de_payload()` e passa a usar atributo, que o
`mypy` confere.

**A v1 é exatamente o que já está gravado.** Job enfileirado antes desta
mudança continua no banco e vai ser reclamado depois dela, então nenhuma chave
foi renomeada para entrar aqui — inclusive `rotulo`, em português no meio de
chaves em inglês. Arrumar nome é mudança de versão, não de arquivo.

Os dois lados desta borda não sobem juntos, e o parser trata as duas
assimetrias: chave desconhecida é ignorada (controlador novo, agente velho —
que é a ordem do deploy) e `v` maior que o conhecido é recusado com uma
mensagem que diz para atualizar o agente.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Any, TypeVar

PAYLOAD_VERSION = 2

# Chave da versão. Curta porque o payload é lido em log e em tela de depuração;
# ausente quer dizer v1 — é o que os jobs gravados até aqui têm.
CHAVE_VERSAO = "v"


class PayloadInvalido(ValueError):
    """Payload que este código não sabe ler: falta chave, ou é de uma versão
    posterior à que este processo conhece."""


P = TypeVar("P", bound="Payload")


@dataclass(frozen=True)
class Payload:
    #: nome do tipo de job, como está na coluna `job.type` — entra só na
    #: mensagem de erro, para que ela diga de que job se fala.
    TIPO = "?"

    def como_dict(self) -> dict[str, Any]:
        """O dicionário a gravar em `job.payload`, com a versão junto."""
        return {CHAVE_VERSAO: 1, **asdict(self)}

    @classmethod
    def de_payload(cls: type[P], payload: Mapping[str, Any]) -> P:
        versao = payload.get(CHAVE_VERSAO, 1)
        if versao > PAYLOAD_VERSION:
            raise PayloadInvalido(
                f"job {cls.TIPO} pede payload v{versao} e este processo entende "
                f"até v{PAYLOAD_VERSION} — atualize o agente")
        obrigatorias = [f.name for f in fields(cls)
                        if f.default is MISSING and f.default_factory is MISSING]
        faltando = [nome for nome in obrigatorias if payload.get(nome) is None]
        if faltando:
            raise PayloadInvalido(
                f"payload de job {cls.TIPO} sem {', '.join(faltando)}")
        # Chave que não é campo é descartada: o controlador sobe antes dos
        # agentes, então um campo novo tem de atravessar o agente velho.
        return cls(**{f.name: payload[f.name] for f in fields(cls)
                      if f.name in payload})


@dataclass(frozen=True)
class Train(Payload):
    """Treino de uma versão congelada. Roda em máquina com capacidade `train`.

    `run_id` é o `training_run` por onde o agente manda evento e métrica. O id
    é sorteado no serviço antes do INSERT do job justamente para caber aqui: o
    run tem FK para o job, então o payload não teria como trazê-lo se
    dependesse do id que o banco geraria depois.
    """
    TIPO = "train"

    dataset_version_id: str
    run_id: str
    model_slug: str
    base_model: str
    project_slug: str
    dataset_slug: str
    version: int
    params: dict[str, Any]

    def como_dict(self) -> dict[str, Any]:
        # Um agente v1 ignoraria os parâmetros novos e treinaria outro modelo.
        # Só o treino avançado exige v2; jobs antigos e básicos seguem em v1.
        saida = super().como_dict()
        if self.params.keys() - {"epochs", "imgsz", "batch"}:
            saida[CHAVE_VERSAO] = 2
        return saida


@dataclass(frozen=True)
class Prelabel(Payload):
    """Pré-tagueamento. As imagens do escopo não vêm no payload: o agente as
    pede em `/targets`, porque `only_missing` depende de quem já anotou o quê
    no momento em que o job roda, não no momento em que foi enfileirado."""
    TIPO = "prelabel"

    model_version_id: str
    weights_uri: str
    schema_id: str
    project_slug: str
    conf: float = 0.25
    only_missing: bool = True
    # escopo: exatamente um dos três (a rota é quem garante).
    dataset_slug: str | None = None
    sample_id: str | None = None
    sample_ids: list[str] | None = None


@dataclass(frozen=True)
class Export(Payload):
    """Compilação para um formato implantável. Roda NA máquina-alvo quando é
    engine — o artefato do TensorRT é específico do dispositivo."""
    TIPO = "export"

    model_version_id: str
    weights_uri: str
    model_slug: str
    project_slug: str
    version: int
    format: str = "engine"
    imgsz: int = 640
    precision: str = "fp16"
    dla: bool = False
    dynamic: bool = False


@dataclass(frozen=True)
class Evaluate(Payload):
    """Medição de um artefato contra uma versão de benchmark.

    `artifact_uri` é o peso (`format='pt'`) ou o build compilado; é o formato
    que muda a latência, que é o que a comparação existe para medir."""
    TIPO = "evaluate"

    model_version_id: str
    artifact_uri: str
    schema_id: str
    dataset_version_id: str
    dataset_slug: str
    project_slug: str
    format: str = "pt"
    rotulo: str = "pt"
    imgsz: int = 640
    model_build_id: str | None = None


#: Tipo de job → contrato. Sem entrada aqui é job que não vai para o agente
#: (`import` roda dentro do processo da API; ver item 5 do PLANO).
POR_TIPO: dict[str, type[Payload]] = {
    c.TIPO: c for c in (Train, Prelabel, Export, Evaluate)}
