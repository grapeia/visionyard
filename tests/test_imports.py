"""Importação: conversão de bbox, idempotência, mapeamento e dry-run."""
from __future__ import annotations

import json
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from api.importers.base import ImportReport, map_classes
from api.importers.label_studio import LabelStudioImporter, _to_yolo, _unwrap_path

pytestmark = pytest.mark.anyio

PROJ = {"slug": "linha-de-inspecao", "name": "Linha de inspeção", "task_type": "detect",
        "classes": ["classe_a", "classe_b"]}


@pytest.fixture
def sem_laco_embutido(monkeypatch):
    """Desliga o laço que a API sobe no lifespan.

    É lido a cada volta do laço, então desligar aqui basta — sem isso a
    importação já teria sido executada quando o teste fosse olhar a fila, e não
    haveria como afirmar que quem a executa é quem a reclama.

    Os testes de recolhimento também o pedem: o laço chama `recolher_orfaos` a
    cada volta, e uma volta caindo no meio do teste roubaria dele a linha que
    ele ia recolher.
    """
    from api.config import settings
    monkeypatch.setattr(settings, "import_worker_embedded", False)


# --------------------------------------------------------------------------- #
# Conversão — erro aqui é silencioso: o treino roda, só que pior
# --------------------------------------------------------------------------- #

def test_bbox_percentual_canto_vira_yolo_centro():
    """LS: canto sup. esq. em %. YOLO: centro, normalizado."""
    # caixa cobrindo o quadrante superior esquerdo exato
    cx, cy, w, h = _to_yolo({"x": 0, "y": 0, "width": 50, "height": 50})
    assert (cx, cy, w, h) == (0.25, 0.25, 0.5, 0.5)

    # caixa centrada, metade da imagem
    cx, cy, w, h = _to_yolo({"x": 25, "y": 25, "width": 50, "height": 50})
    assert (cx, cy, w, h) == (0.5, 0.5, 0.5, 0.5)

    # imagem inteira
    assert _to_yolo({"x": 0, "y": 0, "width": 100, "height": 100}) == \
        (0.5, 0.5, 1.0, 1.0)


def test_caso_real_do_dump():
    """Valores tirados de um result de verdade do dump do usuário."""
    cx, cy, w, _h = _to_yolo({"x": 17.08507670850767, "y": 0,
                             "width": 27.71966527196652,
                             "height": 18.410041841004183})
    assert cx == pytest.approx(0.3094, abs=1e-4)
    assert cy == pytest.approx(0.0921, abs=1e-4)
    assert w == pytest.approx(0.2772, abs=1e-4)
    assert 0 <= cx <= 1 and 0 <= cy <= 1


def test_desembrulha_caminho_do_ls():
    assert _unwrap_path("/data/local-files/?d=frames/foo.jpg") == "frames/foo.jpg"
    assert _unwrap_path("gs://bucket/x.webp") == "gs://bucket/x.webp"
    assert _unwrap_path("/frames/foo.jpg") == "frames/foo.jpg"


def test_mapeamento_de_classes():
    mapping, unmapped = map_classes(["classe_a", "classe_b"],
                                    ["classe_a", "classe_b"])
    assert mapping == {"classe_a": 0, "classe_b": 1}
    assert unmapped == []

    # nome divergente resolvido por override
    mapping, unmapped = map_classes(["origem_a"], ["classe_a"],
                                    {"origem_a": "classe_a"})
    assert mapping == {"origem_a": 0} and unmapped == []

    # sem override, é reportado — nunca descartado calado
    mapping, unmapped = map_classes(["desconhecida"], ["classe_a"])
    assert mapping == {} and unmapped == ["desconhecida"]


# --------------------------------------------------------------------------- #
# Dump sintético
# --------------------------------------------------------------------------- #

def _png(path: Path, w: int = 64, h: int = 48, tint: int = 0) -> None:
    """PNG mínimo válido, sem depender de Pillow para escrever.

    `tint` varia os bytes: imagens idênticas teriam o mesmo sha256 e seriam
    deduplicadas — correto no produto, mas indesejado num fixture que quer
    N samples distintos.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    px = bytes([255, tint % 256, 0])
    raw = b"".join(b"\x00" + px * w for _ in range(h))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b""))


def _dump(tmp_path: Path, n_tasks: int = 3, annotate: int = 2,
          label: str = "classe_a") -> tuple[Path, Path]:
    """Cria um sqlite no formato do Label Studio + as imagens."""
    images = tmp_path / "frames"
    images.mkdir()
    db_path = tmp_path / "ls.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE project (id INTEGER PRIMARY KEY, label_config TEXT);
        CREATE TABLE task (id INTEGER PRIMARY KEY, project_id INT, data TEXT);
        CREATE TABLE task_completion (id INTEGER PRIMARY KEY, task_id INT,
            result TEXT, was_cancelled INT DEFAULT 0, completed_by_id INT);
        CREATE TABLE htx_user (id INTEGER PRIMARY KEY, email TEXT);
    """)
    conn.execute("INSERT INTO project VALUES (1, ?)",
                 (f'<View><RectangleLabels name="label" toName="img">'
                  f'<Label value="{label}"/>'
                  f'<Label value="classe_b"/>'
                  f'</RectangleLabels></View>',))
    conn.execute("INSERT INTO htx_user VALUES (7, 'rotulador@teste.com')")
    for i in range(n_tasks):
        name = f"frame_{i}.png"
        _png(images / name, tint=i * 37 + 11)
        conn.execute("INSERT INTO task VALUES (?,1,?)",
                     (i + 1, json.dumps(
                         {"$undefined$": f"/data/local-files/?d=frames/{name}"})))
        if i < annotate:
            result = json.dumps([{
                "type": "rectanglelabels", "original_width": 64,
                "original_height": 48,
                "value": {"x": 10, "y": 20, "width": 30, "height": 40,
                          "rotation": 0, "rectanglelabels": [label]}}])
            conn.execute(
                "INSERT INTO task_completion (task_id, result, completed_by_id)"
                " VALUES (?,?,7)", (i + 1, result))
    conn.commit()
    conn.close()
    return db_path, images


def test_importer_le_classes_e_samples(tmp_path):
    db_path, _images = _dump(tmp_path)
    imp = LabelStudioImporter(str(db_path), image_root=str(tmp_path))
    assert imp.classes() == ["classe_a", "classe_b"]

    report = ImportReport()
    samples = list(imp.read(report))
    assert len(samples) == 3
    assert sum(1 for s in samples if s.annotated) == 2
    assert samples[0].author_email == "rotulador@teste.com"
    assert samples[0].boxes[0].cls_name == "classe_a"
    assert samples[0].boxes[0].cx == pytest.approx(0.25)


def test_bbox_com_rotacao_e_pulada(tmp_path):
    """YOLO não representa caixa girada — importar ignorando daria caixa errada."""
    db_path, _ = _dump(tmp_path, n_tasks=1, annotate=1)
    conn = sqlite3.connect(db_path)
    result = json.dumps([{"type": "rectanglelabels", "original_width": 64,
                          "original_height": 48,
                          "value": {"x": 10, "y": 20, "width": 30,
                                    "height": 40, "rotation": 45,
                                    "rectanglelabels": ["classe_a"]}}])
    conn.execute("UPDATE task_completion SET result=?", (result,))
    conn.commit()
    conn.close()

    report = ImportReport()
    samples = list(LabelStudioImporter(str(db_path),
                                       image_root=str(_.parent)).read(report))
    assert samples[0].boxes == []
    assert any("rotação" in s["reason"] for s in report.skipped)


# --------------------------------------------------------------------------- #
# Ponta a ponta pela API
# --------------------------------------------------------------------------- #

async def test_dry_run_nao_grava(admin, tmp_path):
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path)

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path),
                               "image_root": str(tmp_path)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["report"]["samples_created"] == 3
    assert body["source_classes"] == ["classe_a", "classe_b"]

    # nada foi gravado
    r = await admin.get(f"/v1/projects/{PROJ['slug']}")
    assert r.json()["counts"]["samples"] == 0


async def test_import_real_e_idempotente(admin, tmp_path):
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path)
    payload = {"kind": "label_studio_sqlite", "sqlite_path": str(db_path),
               "image_root": str(tmp_path),
               "image_base_uri": "gs://bucket/frames"}

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json=payload)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    job = await _aguarda(admin, job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["samples_created"] == 3
    assert job["result"]["annotations_created"] == 2

    r = await admin.get(f"/v1/projects/{PROJ['slug']}")
    assert r.json()["counts"]["samples"] == 3
    assert r.json()["counts"]["annotations"] == 2

    # reimportar não duplica
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json=payload)
    job = await _aguarda(admin, r.json()["job_id"])
    assert job["result"]["samples_created"] == 0
    assert job["result"]["samples_existing"] == 3
    assert job["result"]["annotations_updated"] == 2

    r = await admin.get(f"/v1/projects/{PROJ['slug']}")
    assert r.json()["counts"]["samples"] == 3
    assert r.json()["counts"]["annotations"] == 2


async def test_classe_desconhecida_e_reportada(admin, tmp_path):
    """Classe fora do schema não some calada: aparece no relatório."""
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path, n_tasks=1, annotate=1, label="classe_nova")

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path),
                               "image_root": str(tmp_path)})
    report = r.json()["report"]
    assert "classe_nova" in report["unmapped_classes"]
    assert any("classe_nova" in w for w in report["warnings"])


async def test_override_resolve_nome_divergente(admin, tmp_path):
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path, n_tasks=1, annotate=1, label="origem_a")

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path),
                               "image_root": str(tmp_path),
                               "class_overrides": {"origem_a": "classe_a"}})
    job = await _aguarda(admin, r.json()["job_id"])
    assert job["result"]["unmapped_classes"] == {}
    assert job["result"]["annotations_created"] == 1


async def test_annotator_nao_importa(admin, client, tmp_path):
    """Importar mexe no dado de todo mundo — exige engineer."""
    from .test_projects import novo_usuario
    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path, n_tasks=1)
    async with novo_usuario(admin, client, "ana@teste.com") as (ana, _uid):
        await admin.put(f"/v1/projects/{PROJ['slug']}/members",
                        json={"email": "ana@teste.com", "role": "annotator"})
        r = await ana.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                           json={"kind": "label_studio_sqlite",
                                 "sqlite_path": str(db_path),
                                 "image_root": str(tmp_path)})
        assert r.status_code == 403


async def test_dump_inexistente_da_400(admin):
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": "/nao/existe.sqlite3"})
    assert r.status_code == 400


async def _aguarda(admin, job_id: str, tentativas: int = 60) -> dict:
    """A importação roda em background; espera terminar."""
    import asyncio
    for _ in range(tentativas):
        r = await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")
        job = r.json()
        if job["status"] in ("succeeded", "failed"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job não terminou: {job}")


async def test_imagens_identicas_sao_um_sample_so(admin, tmp_path):
    """Dedupe é por conteúdo, não por nome: o mesmo frame com dois nomes
    diferentes é uma imagem só — e o dry-run precisa prever isso."""
    db_path, images = _dump(tmp_path, n_tasks=2, annotate=0)
    for f in sorted(images.glob("*.png")):
        _png(f, tint=99)                       # todas idênticas

    await admin.post("/v1/projects", json=PROJ)
    payload = {"kind": "label_studio_sqlite", "sqlite_path": str(db_path),
               "image_root": str(tmp_path)}

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json=payload)
    assert r.json()["report"]["samples_created"] == 1
    assert r.json()["report"]["samples_existing"] == 1

    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json=payload)
    job = await _aguarda(admin, r.json()["job_id"])
    assert job["result"]["samples_created"] == 1


def test_prefixo_gcs_preserva_barra_inicial():
    """Nome de objeto no GCS pode começar com "/" — e no bucket de produção
    deste projeto começa ("/frame_2024..."). Remover a barra fazia a listagem
    devolver zero resultados."""
    from api.importers.gcs_prefix import GCSPrefixImporter
    imp = GCSPrefixImporter("meu-bucket", prefix="/frame_2024")
    assert imp.prefix == "/frame_2024"
    assert imp.bucket == "meu-bucket"
    # e o esquema gs:// é removido do nome do bucket
    assert GCSPrefixImporter("gs://outro/").bucket == "outro"


async def test_caminho_fora_da_raiz_e_recusado(admin, tmp_path):
    """sqlite_path vinha do cliente sem cerca: a resposta dizia se um arquivo
    existe ou não em qualquer lugar do servidor, e qualquer sqlite legível
    podia ser importado."""
    await admin.post("/v1/projects", json=PROJ)
    for caminho in ("/etc/passwd", "/root/.ssh/id_rsa",
                    str(tmp_path / ".." / ".." / ".." / "etc" / "hosts")):
        r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                             json={"kind": "label_studio_sqlite",
                                   "sqlite_path": caminho,
                                   "image_root": str(tmp_path)})
        assert r.status_code == 400
        assert "fora da pasta" in r.json()["detail"]


async def test_erro_nao_revela_se_o_arquivo_existe(admin, tmp_path):
    """A mensagem era 'dump não encontrado: /caminho' — um oráculo."""
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(tmp_path / "nao-existe.sqlite3"),
                               "image_root": str(tmp_path)})
    assert r.status_code == 400
    assert str(tmp_path) not in r.json()["detail"]


async def test_import_sem_image_root_e_recusado(admin, tmp_path):
    """Sem image_root o importador pulava TODOS os samples e o job terminava
    com 'succeeded' e zero importados — sem nada indicar o que faltou."""
    db_path, _ = _dump(tmp_path, n_tasks=2)
    await admin.post("/v1/projects", json=PROJ)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=true",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path)})
    assert r.status_code == 400
    assert "image_root" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Job órfão: a importação roda dentro do processo da API, e um deploy no meio
# leva a task junto — a linha do job fica em `running` para sempre
# --------------------------------------------------------------------------- #

async def _import_preso(admin, superuser, *, ha_segundos: int) -> str:
    """Job de importação em `running` com o último ponto batido há N segundos.
    É exatamente o que sobra quando o processo morre no meio da importação."""
    await admin.post("/v1/projects", json=PROJ)
    import psycopg

    from .conftest import TEST_DSN
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        pid = conn.execute("SELECT id FROM project WHERE slug = %s",
                           (PROJ["slug"],)).fetchone()[0]
        row = conn.execute(
            "INSERT INTO job (project_id, type, payload, status, created_by,"
            " started_at, heartbeat_at)"
            " VALUES (%s,'import','{}'::jsonb,'running',%s,"
            "         now() - make_interval(secs => %s),"
            "         now() - make_interval(secs => %s)) RETURNING id",
            (pid, superuser["id"], ha_segundos, ha_segundos)).fetchone()
    return str(row[0])


async def test_importacao_orfa_e_recolhida(admin, superuser,
                                           sem_laco_embutido):
    """Sem isto o job fica preso em `running` para sempre: o reaper do agente
    filtra por `heartbeat_at` e por `attempts >= max_attempts`, e importação
    não bate ponto nem conta tentativa."""
    from api.services import imports as svc

    job_id = await _import_preso(admin, superuser, ha_segundos=24 * 3600)
    assert await svc.recolher_orfaos() == 1

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "failed"
    # a mensagem é lida na tela por quem tem papel `viewer`: precisa dizer o
    # que fazer, e refazer a importação é seguro (o dedup é por md5)
    assert "reimportar" in j["error"].lower()


async def test_importacao_viva_nao_e_recolhida(admin, superuser,
                                               sem_laco_embutido):
    """O ponto recente é a diferença entre 'morreu' e 'está demorando' — um
    dump grande passa minutos numa imagem só."""
    from api.services import imports as svc

    job_id = await _import_preso(admin, superuser, ha_segundos=5)
    assert await svc.recolher_orfaos() == 0

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "running"


async def test_relato_atrasado_nao_reescreve_import_recolhido(
        admin, superuser, sem_laco_embutido):
    """Mesma invariante do agente: quem foi dado por morto e volta não
    ressuscita o desfecho. Aqui o caso é a task que sobreviveu ao reaper e
    termina depois dele."""
    from api.services import imports as svc

    job_id = await _import_preso(admin, superuser, ha_segundos=24 * 3600)
    await svc.recolher_orfaos()
    await svc.concluir_job(job_id, {"samples_created": 7})

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "failed"
    assert (j["result"] or {}).get("samples_created") != 7


async def test_listar_jobs_recolhe_o_que_ficou_preso(admin, superuser,
                                                     sem_laco_embutido):
    """A tela de jobs é onde o job preso é visto; recolher ali é o que faz o
    reaper rodar numa instalação sem agente nenhum batendo claim."""
    await _import_preso(admin, superuser, ha_segundos=24 * 3600)
    jobs = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs")).json()
    assert [j["status"] for j in jobs if j["type"] == "import"] == ["failed"]


async def test_import_orfao_nao_e_reclamado_por_maquina(
        admin, client, superuser, sem_laco_embutido):
    """Job de importação roda DENTRO da API: não há executor do outro lado.

    O claim do agente não filtra por tipo e casa qualquer `requires` vazio, e
    passou a alcançar a importação no dia em que ela começou a bater ponto. A
    máquina o encerraria como "tipo não suportado" por cima do dono — e como o
    prazo do agente (180s) é menor que o da importação (900s), ela ganharia
    sempre a corrida, inclusive contra uma importação viva que só está lenta.
    """
    from .test_training import _device, _h

    await _import_preso(admin, superuser, ha_segundos=24 * 3600)
    dev = await _device(admin, {"train": True}, slug="maquina-qualquer")
    r = await client.post("/v1/agent/claim", headers=_h(dev["token"]))
    assert r.json()["job"] is None, "a máquina reclamou um job de importação"


async def test_evento_concorrente_nao_derruba_o_recolhimento(
        admin, superuser, sem_laco_embutido):
    """`job_event` tem PK (job_id, seq) e o seq sai de `max(seq)+1`.

    Desde que o recolhimento passou a escrever evento, há dois escritores no
    mesmo job — o reaper e a task que ainda pode estar viva, que é o cenário
    que a guarda de `concluir_job` prevê. Se os dois calculam o mesmo seq, o
    perdedor levantava `UniqueViolation`: no reaper isso aborta a transação
    inteira, então nada é recolhido E a tela de jobs devolve 500.
    """
    import asyncio

    from api import db
    from api.services import imports as svc

    job_id = await _import_preso(admin, superuser, ha_segundos=10)
    async with db.connection() as dono:
        async with dono.transaction():
            # a linha existe, mas nenhuma outra conexão a enxerga ainda
            await dono.execute(
                "INSERT INTO job_event (job_id, seq, message)"
                " VALUES (%s, 1, 'do dono')", (job_id,))
            tarefa = asyncio.create_task(
                svc.registrar_evento(job_id, "do recolhimento"))
            await asyncio.sleep(0.2)   # calcula seq=1 e trava no índice único
        # o commit solta a outra: ou ela desiste, ou estoura
    await tarefa


# --------------------------------------------------------------------------- #
# A fila: quem pede não é quem executa
# --------------------------------------------------------------------------- #

async def test_quem_pede_nao_executa_e_a_fila_espera(admin, tmp_path,
                                                     sem_laco_embutido):
    """O item 5: a importação sai do processo que atendeu a requisição.

    Antes, o POST criava o job já `running` e o executava ali mesmo, num
    `asyncio.create_task` — deploy no meio matava a execução, e o hash de um
    arquivo grande disputava o event loop com quem estava atendendo
    requisição. Agora o POST enfileira e responde; o job fica esperando até
    alguém reclamar, e é esse alguém que pode estar noutro processo.
    """
    from api import worker

    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path)
    r = await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                         json={"kind": "label_studio_sqlite",
                               "sqlite_path": str(db_path),
                               "image_root": str(tmp_path),
                               "image_base_uri": "gs://bucket/frames"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"

    # nada aconteceu: ninguém reclamou ainda
    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "queued" and j["started_at"] is None
    proj = (await admin.get(f"/v1/projects/{PROJ['slug']}")).json()
    assert proj["counts"]["samples"] == 0

    # o executor reclama e roda — reconstruindo o importador só a partir da
    # linha do job, que é o que permite que ele viva noutro processo
    assert await worker.passo() is True

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "succeeded", j
    assert j["result"]["samples_created"] == 3
    proj = (await admin.get(f"/v1/projects/{PROJ['slug']}")).json()
    assert proj["counts"]["samples"] == 3

    assert await worker.passo() is False      # a fila esvaziou


async def test_dois_executores_nao_pegam_a_mesma_importacao(admin, tmp_path,
                                                            sem_laco_embutido):
    """Duas réplicas da API, ou a API e um worker de fora, olham a mesma fila.

    Sem `FOR UPDATE SKIP LOCKED` no claim, as duas importariam o mesmo dump ao
    mesmo tempo — e o segundo relatório reescreveria o primeiro em `job.result`.
    """
    import asyncio

    from api import worker

    await admin.post("/v1/projects", json=PROJ)
    db_path, _ = _dump(tmp_path)
    await admin.post(f"/v1/projects/{PROJ['slug']}/imports?dry_run=false",
                     json={"kind": "label_studio_sqlite",
                           "sqlite_path": str(db_path),
                           "image_root": str(tmp_path)})

    pegaram = await asyncio.gather(worker.passo(), worker.passo())
    assert sorted(pegaram) == [False, True]


async def test_fila_sem_executor_no_ar_e_recolhida(admin, superuser,
                                                   sem_laco_embutido):
    """O jeito novo de a importação não acontecer: ninguém veio buscá-la.

    Só existe desde que a execução saiu do processo do POST — antes, enfileirar
    e executar eram o mesmo ato. A mensagem é outra de propósito: reimportar
    não conserta fila sem executor, e quem lê a tela precisa saber que o
    problema é da instalação.
    """
    from api.services import imports as svc

    job_id = await _import_na_fila(admin, superuser, ha_segundos=24 * 3600)
    assert await svc.recolher_orfaos() == 1

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "failed"
    assert "api.worker" in j["error"]
    assert "reimportar" not in j["error"].lower()


async def test_fila_que_anda_nao_e_recolhida(admin, superuser,
                                             sem_laco_embutido):
    """Esperar a vez não é estar abandonado.

    Três dumps grandes enfileirados juntos: o terceiro passa horas na fila
    porque os dois primeiros estão sendo importados, e recolhê-lo por isso
    mataria o comportamento correto de uma fila. O que distingue os dois casos
    é haver ou não uma importação em curso.
    """
    from api.services import imports as svc

    job_id = await _import_na_fila(admin, superuser, ha_segundos=24 * 3600)
    await _import_preso(admin, superuser, ha_segundos=5)   # uma rodando, viva

    assert await svc.recolher_orfaos() == 0
    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "queued"


async def _import_na_fila(admin, superuser, *, ha_segundos: int,
                          payload: dict | None = None) -> str:
    """Job de importação em `queued` desde N segundos atrás."""
    await admin.post("/v1/projects", json=PROJ)
    import psycopg

    from .conftest import TEST_DSN
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        pid = conn.execute("SELECT id FROM project WHERE slug = %s",
                           (PROJ["slug"],)).fetchone()[0]
        row = conn.execute(
            "INSERT INTO job (project_id, type, payload, status, created_by,"
            " created_at) VALUES (%s,'import',%s::jsonb,'queued',%s,"
            "                     now() - make_interval(secs => %s))"
            " RETURNING id",
            (pid, json.dumps(payload or {}), superuser["id"],
             ha_segundos)).fetchone()
    return str(row[0])


async def _import_concluido(admin, superuser, *, ha_segundos: int) -> str:
    """Job de importação que terminou bem há N segundos — prova de que havia
    executor no ar naquele instante."""
    await admin.post("/v1/projects", json=PROJ)
    import psycopg

    from .conftest import TEST_DSN
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        pid = conn.execute("SELECT id FROM project WHERE slug = %s",
                           (PROJ["slug"],)).fetchone()[0]
        row = conn.execute(
            "INSERT INTO job (project_id, type, payload, status, created_by,"
            " started_at, finished_at)"
            " VALUES (%s,'import','{}'::jsonb,'succeeded',%s,"
            "         now() - make_interval(secs => %s),"
            "         now() - make_interval(secs => %s)) RETURNING id",
            (pid, superuser["id"], ha_segundos + 1, ha_segundos)).fetchone()
    return str(row[0])


async def test_recolher_uma_travada_nao_leva_a_fila_junto(admin, superuser,
                                                          sem_laco_embutido):
    """Recolher a que travou não é prova de que ninguém está executando.

    A guarda da fila olhava só `NOT EXISTS (importação running)` — e o UPDATE
    logo acima, na mesma transação, acabara de tirar do `running` a que passou
    do prazo. O executor podia estar vivo (um dump grande passa minutos numa
    imagem só, sem bater ponto), e a fila atrás dele morria junto, com uma
    mensagem dizendo que o processo não está no ar.
    """
    from api.services import imports as svc

    fila = await _import_na_fila(admin, superuser, ha_segundos=24 * 3600)
    await _import_preso(admin, superuser, ha_segundos=24 * 3600)

    assert await svc.recolher_orfaos() == 1        # só a travada
    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{fila}")).json()
    assert j["status"] == "queued"


async def test_importacao_recem_concluida_protege_a_fila(admin, superuser,
                                                         sem_laco_embutido):
    """Entre terminar uma e reclamar a próxima, nenhuma está `running`.

    Uma tela de jobs aberta nesse instante recolhia a fila inteira. Ter
    terminado uma importação há pouco é a mesma prova de vida que estar
    executando uma.
    """
    from api.services import imports as svc

    fila = await _import_na_fila(admin, superuser, ha_segundos=24 * 3600)
    await _import_concluido(admin, superuser, ha_segundos=5)

    assert await svc.recolher_orfaos() == 0
    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{fila}")).json()
    assert j["status"] == "queued"


def test_payload_sem_a_chave_de_hoje_ainda_e_lido():
    """O payload atravessa deploy desde que a importação virou fila.

    Job enfileirado antes de um deploy é reclamado depois dele — é a mesma
    assimetria do `contracts/jobs.py`, e a mesma regra: campo novo entra com
    default. Lendo com `[]`, acrescentar um campo ao `ImportIn` mataria com
    `KeyError` tudo o que já estava na fila.
    """
    from api.services import imports as svc

    imp = svc.construir_importador({"kind": "gcs_prefix", "bucket": "b"})
    assert "b" in imp.describe()


async def test_erro_do_executor_nao_publica_caminho_do_servidor(
        admin, superuser, tmp_path, sem_laco_embutido):
    """`job.error` é lido com papel `viewer` — a cerca cita `import_root`.

    O caso é o que o próprio executor prevê: worker com `import_root` diferente
    do de quem pediu. A recusa é legítima, mas o texto dela punha o caminho
    absoluto do servidor na tela de todo mundo.
    """
    from api import worker
    from api.config import settings

    job_id = await _import_na_fila(
        admin, superuser, ha_segundos=0,
        payload={"kind": "label_studio_sqlite",
                 "sqlite_path": "/fora/da/cerca/ls.sqlite3",
                 "image_root": "/fora/da/cerca"})
    assert await worker.passo() is True

    j = (await admin.get(f"/v1/projects/{PROJ['slug']}/jobs/{job_id}")).json()
    assert j["status"] == "failed"
    assert settings.import_root not in j["error"]
    assert all(settings.import_root not in e["message"] for e in j["events"])


async def test_maquina_nao_reclama_importacao_na_fila(admin, client, superuser,
                                                      sem_laco_embutido):
    """A importação passou a existir em `queued` — o estado que o claim mira.

    `requires` vazio casa com qualquer capacidade e o claim não filtra por
    tipo: o que a mantém fora é `TIPOS_LOCAIS`. Antes desta fila o caso era
    inalcançável (o job nascia `running`), e o teste que existia só cobria o
    órfão.
    """
    from .test_training import _device, _h

    await _import_na_fila(admin, superuser, ha_segundos=0)
    dev = await _device(admin, {"train": True, "infer": True}, slug="qualquer")
    r = await client.post("/v1/agent/claim", headers=_h(dev["token"]))
    assert r.status_code == 200
    assert r.json()["job"] is None
