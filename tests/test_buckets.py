"""Navegação de bucket: quem pode, o que vaza e o que a allowlist barra."""
from __future__ import annotations

import pytest

from api.config import settings

from .test_projects import novo_usuario

pytestmark = pytest.mark.anyio

PROJ = {"slug": "linha-de-inspecao", "name": "Linha de inspeção", "task_type": "detect",
        "classes": ["classe_a", "classe_b"]}


async def test_annotator_nao_navega_bucket(admin, client):
    """Navegar bucket é passo de compor dataset, não leitura pública.

    Sem isso, um rotulador terceirizado enxergaria o conteúdo de todos os
    buckets da instalação.
    """
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ana@teste.com", "role": "annotator"})
        r = await ana.get(f"/v1/projects/{PROJ['slug']}/buckets")
        assert r.status_code == 403
        r = await ana.get(f"/v1/projects/{PROJ['slug']}/buckets/browse"
                          f"?bucket={settings.buckets[0]}")
        assert r.status_code == 403


async def test_nao_membro_nao_navega(admin, client):
    await admin.post("/v1/projects", json=PROJ)
    async with novo_usuario(admin, client, "estranho@teste.com") as (outro, _):
        r = await outro.get(f"/v1/projects/{PROJ['slug']}/buckets")
        assert r.status_code == 404          # 404, não 403: não revela o projeto


async def test_bucket_fora_da_lista_e_recusado(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/buckets/browse"
                        "?bucket=bucket-de-outro-sistema")
    assert r.status_code == 404


async def test_allowlist_vazia_nega_tudo(admin, monkeypatch):
    """Falha fechada.

    A conta de serviço da máquina enxerga o projeto inteiro do GCP; uma
    allowlist que ignorasse a lista vazia viraria um listador de qualquer
    bucket da empresa.
    """
    await admin.post("/v1/projects", json=PROJ)
    monkeypatch.setattr(settings, "buckets", [])
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/buckets/browse"
                        "?bucket=qualquer-um")
    assert r.status_code == 404


async def test_erro_do_gcs_nao_vaza_detalhe(admin, monkeypatch):
    """Mensagem de erro do GCS cita projeto, conta de serviço e caminho."""
    from api.routers import buckets as mod

    def explode(*a, **k):
        raise RuntimeError("403 conta-secreta@production-438011.iam "
                           "sem permissão em gs://bucket-interno/segredo")
    monkeypatch.setattr(mod, "_navegar", explode)

    await admin.post("/v1/projects", json=PROJ)
    r = await admin.get(f"/v1/projects/{PROJ['slug']}/buckets/browse"
                        f"?bucket={settings.buckets[0]}")
    assert r.status_code == 502
    corpo = r.text
    assert "conta-secreta" not in corpo
    assert "production-438011" not in corpo
    assert "bucket-interno" not in corpo
