"""Cliente HTTP do agente para a Control API.

Fino de propósito: cada método é uma rota do protocolo (api/routers/agents.py).
Síncrono (requests) porque o agente é um loop simples — não há concorrência a
espremer aqui, e sync é uma dependência a menos de entender no dispositivo de borda.
"""
from __future__ import annotations

import time
from typing import Any

import requests


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 tentativas: int = 4):
        self._base = base_url
        self._timeout = timeout
        self._tentativas = tentativas
        self._s = requests.Session()
        self._s.headers["X-Agent-Token"] = token

    def _url(self, path: str) -> str:
        return f"{self._base}/v1{path}"

    def _send(self, method: str, path: str, stream: bool = False,
              **kw) -> requests.Response:
        """Um blip de rede ou 5xx no meio de um job (horas de treino) não pode
        derrubá-lo: erro transitório é tentado de novo com backoff. As rotas do
        protocolo são idempotentes (ON CONFLICT/upsert), então repetir é seguro.
        4xx é erro real do cliente — sobe na hora."""
        ultimo: Exception | None = None
        for i in range(self._tentativas):
            try:
                r = self._s.request(method, self._url(path),
                                    timeout=self._timeout, stream=stream, **kw)
            except requests.exceptions.RequestException as e:
                ultimo = e
            else:
                if r.status_code < 400:
                    return r
                if r.status_code < 500:      # erro do cliente: não adianta repetir
                    raise ApiError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
                ultimo = ApiError(f"{method} {path} → {r.status_code}")
            if i < self._tentativas - 1:
                time.sleep(min(2 ** i, 8))
        raise ApiError(f"{method} {path} falhou após {self._tentativas} tentativas: {ultimo}")

    def _post(self, path: str, json: dict | None = None) -> Any:
        r = self._send("POST", path, json=json or {})
        return r.json() if r.content else None

    def _get(self, path: str, stream: bool = False) -> requests.Response:
        return self._send("GET", path, stream=stream)

    # ------------------------------------------------------------------ #
    # Presença e fila
    # ------------------------------------------------------------------ #

    def register(self, capabilities: dict, agent_version: str | None = None) -> dict:
        return self._post("/agent/register",
                          {"capabilities": capabilities,
                           "agent_version": agent_version})

    def device_heartbeat(self, status: str = "idle") -> None:
        self._post("/agent/heartbeat", {"status": status})

    def claim(self) -> dict | None:
        return (self._post("/agent/claim") or {}).get("job")

    # ------------------------------------------------------------------ #
    # Job em andamento
    # ------------------------------------------------------------------ #

    def manifest_meta(self, job_id: str) -> dict:
        return self._get(f"/agent/jobs/{job_id}/manifest").json()

    def download_items(self, job_id: str, destino: str) -> None:
        r = self._get(f"/agent/jobs/{job_id}/manifest/items", stream=True)
        with open(destino, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)

    def job_heartbeat(self, job_id: str, progress: dict) -> bool:
        """Devolve True se o usuário pediu cancelamento."""
        r = self._post(f"/agent/jobs/{job_id}/heartbeat", {"progress": progress})
        return bool(r.get("cancel"))

    def events(self, job_id: str, eventos: list[dict]) -> None:
        # Telemetria é best-effort: um log perdido não pode abortar o job. (O
        # heartbeat, esse sim, precisa chegar — não é engolido.)
        if eventos:
            try:
                self._post(f"/agent/jobs/{job_id}/events", {"eventos": eventos})
            except ApiError:
                pass

    def metrics(self, job_id: str, metricas: list[dict]) -> None:
        if metricas:
            try:
                self._post(f"/agent/jobs/{job_id}/metrics", {"metricas": metricas})
            except ApiError:
                pass

    def targets(self, job_id: str) -> dict:
        return self._get(f"/agent/jobs/{job_id}/targets").json()

    def prelabel(self, job_id: str, predicoes: list[dict]) -> dict:
        return self._post(f"/agent/jobs/{job_id}/prelabel",
                          {"predicoes": predicoes})

    def register_model(self, job_id: str, weights_uri: str, sha256: str,
                       nbytes: int, metrics: dict) -> dict:
        return self._post(f"/agent/jobs/{job_id}/model",
                          {"weights_uri": weights_uri, "sha256": sha256,
                           "bytes": nbytes, "metrics": metrics})

    def register_build(self, job_id: str, fmt: str, uri: str, sha256: str,
                       nbytes: int, target: dict, bench: dict) -> dict:
        return self._post(f"/agent/jobs/{job_id}/build",
                          {"format": fmt, "uri": uri, "sha256": sha256,
                           "bytes": nbytes, "target": target, "bench": bench})

    def evaluation(self, job_id: str, metrics: dict,
                   artifacts_uri: str | None = None) -> dict:
        return self._post(f"/agent/jobs/{job_id}/evaluation",
                          {"metrics": metrics, "artifacts_uri": artifacts_uri})

    def finish(self, job_id: str, status: str, error: str | None = None,
               result: dict | None = None) -> None:
        self._post(f"/agent/jobs/{job_id}/finish",
                   {"status": status, "error": error, "result": result or {}})

    # ------------------------------------------------------------------ #
    # Deployment
    # ------------------------------------------------------------------ #

    def pending_deployments(self) -> list[dict]:
        return self._get("/agent/deployments/pending").json().get("deployments", [])

    def deployment_state(self, dep_id: str, state: str,
                         error: str | None = None) -> None:
        self._post(f"/agent/deployments/{dep_id}/state",
                   {"state": state, "error": error})
