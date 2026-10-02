"""Fixtures dos testes da API.

Banco dedicado (`visionyard_test`), recriado do zero pelas migrations a cada
sessão e truncado entre testes. Nunca aponta para o banco de desenvolvimento —
o DSN é fixado aqui, antes de qualquer import de `api`, porque as settings são
lidas no import.
"""
from __future__ import annotations

import os
from pathlib import Path

TEST_DB = "visionyard_test"
ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
TEST_DSN = f"postgresql://postgres:postgres@localhost:5432/{TEST_DB}"

os.environ["VISIONYARD_DATABASE_URL"] = TEST_DSN   # antes de importar api.config
# A instalação nasce sem bucket configurado; os testes de navegação precisam
# de um, então fixam aqui — também antes do import de api.config.
os.environ["VISIONYARD_BUCKETS"] = '["bucket-de-teste"]'
# Idem para o bucket de modelos: sem ele treino e export não têm onde publicar.
os.environ["VISIONYARD_MODELS_BUCKET"] = "modelos-de-teste"

import httpx
import psycopg
import pytest

from api.main import app
from api.security import hash_password

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"

SUPERUSER_EMAIL = "admin@teste.com"
SUPERUSER_PASSWORD = "senha-de-teste-123"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Recria o banco de teste e aplica todas as migrations."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")

    with psycopg.connect(TEST_DSN) as conn:
        for sql_file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            conn.execute(sql_file.read_text())
        conn.commit()
    yield
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture(autouse=True)
def import_root_no_tmp(tmp_path_factory):
    """A importação de arquivo só aceita caminho sob `settings.import_root`.

    Os testes montam dumps em tmp_path, então a raiz aponta para a base do
    pytest — sem isso eles baterem na cerca que existe justamente para impedir
    que o cliente escolha qualquer caminho do servidor.
    """
    from api.config import settings
    anterior = settings.import_root
    settings.import_root = str(tmp_path_factory.getbasetemp())
    yield
    settings.import_root = anterior


@pytest.fixture(autouse=True)
def assinaturas(monkeypatch) -> list[tuple[str, str]]:
    """A assinatura de URL para o agente, sem GCS.

    Autouse porque a lista de implantações pendentes assina a cada chamada:
    sem isto toda a suíte de deploy daria 503 por falta de credencial. A URL
    falsa diz o método e o objeto, e a lista devolvida registra o que foi
    assinado — é por ela que os testes conferem o escopo.
    """
    from api import storage
    feitas: list[tuple[str, str]] = []

    def falsa(uris, minutos, metodo="GET", content_type=None):
        feitas.extend((metodo, u) for u in uris)
        return {u: f"https://assinada/{metodo}/{u[5:]}" for u in uris}

    monkeypatch.setattr(storage, "assinar_ou_falhar", falsa)
    return feitas


@pytest.fixture(autouse=True)
def clean_tables():
    """Zera os dados entre testes, preservando o schema.

    TRUNCATE ... CASCADE em app_user e project derruba tudo o que depende
    deles; login_attempt e audit_log não têm FK e vão explicitamente.
    """
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE app_user, project, login_attempt, audit_log,"
                     " device CASCADE")
    # O cache dos parâmetros é do processo e vale segundos: sem isto o que um
    # teste gravou seguiria valendo no seguinte, com a tabela já vazia.
    from api.services import parameters
    parameters.esquecer()
    yield


@pytest.fixture
def superuser() -> dict:
    """Superusuário ativo — o equivalente ao que db/seed.py cria."""
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO app_user (email, display_name, password_hash, status,"
            " is_superuser) VALUES (%s,%s,%s,'active',true) RETURNING id",
            (SUPERUSER_EMAIL, "Admin de Teste",
             hash_password(SUPERUSER_PASSWORD))).fetchone()
    return {"id": str(row[0]), "email": SUPERUSER_EMAIL,
            "password": SUPERUSER_PASSWORD}


@pytest.fixture
async def client():
    """Cliente HTTP sem sessão, com o pool de conexões aberto."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as c:
            yield c


@pytest.fixture
async def admin(client: httpx.AsyncClient, superuser: dict):
    """Cliente já autenticado como superusuário."""
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 200, r.text
    return client
