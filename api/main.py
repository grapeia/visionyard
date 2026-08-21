"""App FastAPI da Control API.

    .venv/bin/uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, logs
from .routers import agents as agents_router
from .routers import auth as auth_router
from .routers import buckets as buckets_router
from .routers import datasets as datasets_router
from .routers import devices as devices_router
from .routers import imports as imports_router
from .routers import jobs as jobs_router
from .routers import models as models_router
from .routers import prelabel as prelabel_router
from .routers import projects as projects_router
from .routers import samples as samples_router
from .routers import training as training_router
from .routers import users as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.configurar()
    await db.open_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(title="visionyard — Control API", version="0.1.0",
              lifespan=lifespan)

# Fora de qualquer rota e antes dos routers: vale para o estático e para o
# caminho que nenhum handler previu, que é justamente o que se quer ver.
app.middleware("http")(logs.correlacionar)

app.include_router(agents_router.router, prefix="/v1")
app.include_router(auth_router.router, prefix="/v1")
app.include_router(buckets_router.router, prefix="/v1")
app.include_router(datasets_router.router, prefix="/v1")
app.include_router(devices_router.router, prefix="/v1")
app.include_router(imports_router.router, prefix="/v1")
app.include_router(jobs_router.router, prefix="/v1")
app.include_router(models_router.router, prefix="/v1")
app.include_router(prelabel_router.router, prefix="/v1")
app.include_router(projects_router.router, prefix="/v1")
app.include_router(samples_router.router, prefix="/v1")
app.include_router(training_router.router, prefix="/v1")
app.include_router(users_router.router, prefix="/v1")


class Sonda(BaseModel):
    status: str


@app.get("/healthz", tags=["ops"], response_model=Sonda)
async def healthz():
    """Vivo: o processo responde. NÃO toca no banco, de propósito.

    Esta é a sonda que o orquestrador usa para decidir REINICIAR. Consultar o
    banco aqui faria de uma oscilação do Postgres um interruptor do processo:
    a API sã seria derrubada em série, e reiniciar não traz o banco de volta —
    só apaga o que ela ainda conseguia servir (tela, estático, o 503 honesto).
    """
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"], response_model=Sonda)
async def readyz():
    """Pronto: dá para servir requisição que precisa do banco.

    Esta é a sonda de tráfego — a que tira a réplica do balanceador enquanto o
    banco não volta, sem matá-la. 503 e não 500: é indisponibilidade
    temporária, e o detalhe do erro fica no log, sob o código da requisição.
    """
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except Exception:
        logging.getLogger("visionyard").exception("banco indisponível no readyz")
        return JSONResponse(status_code=503,
                            content={"status": "sem banco"})
    return {"status": "ok"}


STATIC = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
