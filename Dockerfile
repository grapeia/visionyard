# Imagem da Control API. Serve também para os passos de manutenção
# (`db.migrate`, `db.seed`), que rodam o mesmo código com outro comando.
#
# O agente de execução NÃO sai daqui: ele roda nas máquinas de GPU e de borda,
# onde o torch vem do fornecedor da plataforma. Ver agent/README.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements antes do código: a camada de dependências só reconstrói quando
# o arquivo muda, não a cada edição de rota.
COPY requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

COPY api/ api/
COPY db/ db/
# contracts/ é o payload de job que a API grava e o agente lê — a API o importa.
COPY contracts/ contracts/

# Usuário sem privilégio. `data/` é ponto de montagem (manifestos de versão e
# dumps de importação) e precisa ser dele, não do root.
RUN useradd --create-home --uid 10001 visionyard \
    && mkdir -p /app/data \
    && chown -R visionyard:visionyard /app
USER visionyard

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
