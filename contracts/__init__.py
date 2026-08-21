"""O que a Control API e o agente combinam entre si.

Duas regras mantêm este pacote utilizável dos dois lados, e as duas são o
motivo de ele existir fora de `api/` e de `agent/`:

- **Não importa de `api` nem de `agent`.** Importar `api` traria fastapi e
  psycopg para o dispositivo de borda.
- **Só stdlib.** Cada dependência do agente é uma roda a achar para a
  arquitetura certa (aarch64 do JetPack, entre outras); `dataclasses` não custa
  nada. É por isso que aqui não há pydantic, embora a API o use em toda rota.
"""
