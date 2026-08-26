"""Login, sessão, convite e token de API."""
from __future__ import annotations

import asyncio
import contextlib

import psycopg
import pytest

from api import db
from api.config import settings

from .conftest import TEST_DSN

pytestmark = pytest.mark.anyio


async def test_me_sem_sessao_401(client):
    assert (await client.get("/v1/me")).status_code == 401


async def test_login_e_me(client, superuser):
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 200
    assert r.json()["email"] == superuser["email"]
    assert r.json()["is_superuser"] is True

    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie          # inacessível a JS
    assert "SameSite=lax" in cookie      # mitiga CSRF

    me = await client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["via"] == "session"


async def test_login_senha_errada(client, superuser):
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": "senha-errada-123"})
    assert r.status_code == 401


async def test_erro_nao_revela_se_email_existe(client, superuser):
    """Email inexistente e senha errada devem dar a MESMA resposta."""
    a = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": "senha-errada-123"})
    b = await client.post("/v1/login", json={"email": "ninguem@teste.com",
                                             "password": "senha-errada-123"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


async def test_login_usuario_desativado(client, superuser):
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("UPDATE app_user SET status='disabled' WHERE id=%s",
                     (superuser["id"],))
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 401


async def test_logout_invalida_sessao(admin):
    assert (await admin.post("/v1/logout")).status_code == 204
    assert (await admin.get("/v1/me")).status_code == 401


async def test_desativar_usuario_derruba_sessao(admin, client):
    """Desativar precisa cortar o acesso agora, não quando a sessão expirar."""
    r = await admin.post("/v1/users", json={
        "email": "alvo@teste.com", "display_name": "Alvo",
        "password": "senha-do-alvo-123"})
    alvo_id = r.json()["id"]

    async with client.__class__(transport=client._transport,
                                base_url="http://test") as vitima:
        await vitima.post("/v1/login", json={"email": "alvo@teste.com",
                                             "password": "senha-do-alvo-123"})
        assert (await vitima.get("/v1/me")).status_code == 200
        await admin.post(f"/v1/users/{alvo_id}/status?status_=disabled")
        assert (await vitima.get("/v1/me")).status_code == 401


async def test_backoff_por_email_nao_tranca_permanentemente(client, superuser):
    """Throttle por email é atraso progressivo, não lockout.

    A trava dura fica no IP; por email o usuário legítimo espera segundos, não
    a janela inteira — senão qualquer um trancaria a conta de qualquer um.
    """
    for _ in range(4):
        await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": "errada-de-prop123"})
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 429
    assert "aguarde" in r.json()["detail"]

    # o atraso é de segundos e se auto-resolve — não é uma trava de 15 min
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("UPDATE login_attempt SET at = at - interval '2 minutes'")
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 200


async def test_teto_duro_por_ip(client, superuser):
    for i in range(10):
        await client.post("/v1/login", json={"email": f"u{i}@teste.com",
                                             "password": "errada-de-prop123"})
        # espalha no tempo pra não esbarrar no backoff por email
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute("UPDATE login_attempt SET at = at - interval '1 minute'")
    r = await client.post("/v1/login", json={"email": superuser["email"],
                                             "password": superuser["password"]})
    assert r.status_code == 429
    assert "endereço" in r.json()["detail"]


async def test_xff_forjado_nao_escapa_do_teto_por_ip(client, superuser):
    """X-Forwarded-For só vale atrás de proxy declarado.

    Sem isso, trocar o header a cada tentativa daria um "IP" novo toda vez e o
    teto por IP nunca acumularia — o controle viraria decoração.
    """
    for i in range(12):
        await client.post("/v1/login",
                          headers={"X-Forwarded-For": f"203.0.113.{i}"},
                          json={"email": f"v{i}@teste.com",
                                "password": "errada-de-prop123"})
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute("UPDATE login_attempt SET at = at - interval '1 minute'")

    r = await client.post("/v1/login",
                          headers={"X-Forwarded-For": "203.0.113.250"},
                          json={"email": superuser["email"],
                                "password": superuser["password"]})
    assert r.status_code == 429

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        ips = conn.execute("SELECT DISTINCT ip FROM login_attempt").fetchall()
    assert len(ips) == 1, f"header forjado virou IP distinto: {ips}"


async def test_xff_que_nao_e_ip_nao_derruba_a_escrita(admin, client):
    """`audit_log.ip` e `login_attempt.ip` são `inet`: um salto que não é
    endereço (nginx emite `unknown`) tem de cair no peer, não virar texto.

    Sem isso o INSERT estoura no cast, e como a auditoria agora vive DENTRO da
    transação de quem escreve, o estouro leva a escrita junto: aceitar convite
    devolveria 500 e deixaria a conta sem senha para sempre.
    """
    from api.config import settings

    novo = await admin.post("/v1/users", json={"email": "xff@teste.com",
                                               "display_name": "XFF"})
    uid = novo.json()["id"]
    token = (await admin.post(f"/v1/users/{uid}/invite")).json()["token"]

    anterior = settings.trusted_proxies
    settings.trusted_proxies = ["127.0.0.0/8"]   # o peer do ASGITransport
    try:
        r = await client.post("/v1/invite/accept",
                              headers={"X-Forwarded-For": "unknown"},
                              json={"token": token, "password": "senha-123456"})
    finally:
        settings.trusted_proxies = anterior

    assert r.status_code == 200, r.text
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        estado = conn.execute("SELECT status FROM app_user WHERE id = %s",
                              (uid,)).fetchone()
        registro = conn.execute(
            "SELECT ip FROM audit_log WHERE action = 'user.invite.accept'"
            "   AND entity_id = %s", (uid,)).fetchone()
    assert estado[0] == "active"
    # cai no peer da conexão, que é o único fato que não veio do header
    assert registro is not None and str(registro[0]) == "127.0.0.1"


async def test_trocar_senha_derruba_outras_sessoes(admin, client, superuser):
    async with client.__class__(transport=client._transport,
                                base_url="http://test") as outra:
        await outra.post("/v1/login", json={"email": superuser["email"],
                                            "password": superuser["password"]})
        assert (await outra.get("/v1/me")).status_code == 200

        r = await admin.post("/v1/me/password", json={
            "current_password": superuser["password"],
            "new_password": "nova-senha-bem-longa"})
        assert r.status_code == 204
        # a outra sessão morre; a de quem trocou continua (ganhou cookie novo)
        assert (await outra.get("/v1/me")).status_code == 401
        assert (await admin.get("/v1/me")).status_code == 200


async def test_senha_curta_rejeitada(admin, superuser):
    r = await admin.post("/v1/me/password", json={
        "current_password": superuser["password"], "new_password": "curta"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Convite
# --------------------------------------------------------------------------- #

async def test_fluxo_de_convite(admin, client):
    r = await admin.post("/v1/users", json={"email": "novo@teste.com",
                                            "display_name": "Novo"})
    assert r.status_code == 201
    assert r.json()["status"] == "invited"
    novo_id = r.json()["id"]

    # sem senha definida, não loga
    r = await client.post("/v1/login", json={"email": "novo@teste.com",
                                             "password": "qualquer-coisa123"})
    assert r.status_code == 401

    r = await admin.post(f"/v1/users/{novo_id}/invite")
    assert r.status_code == 201
    token = r.json()["token"]

    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c2:
        r = await c2.post("/v1/invite/accept",
                          json={"token": token, "password": "senha-nova-123"})
        assert r.status_code == 200
        assert (await c2.get("/v1/me")).status_code == 200

        # uso único
        r = await c2.post("/v1/invite/accept",
                          json={"token": token, "password": "outra-senha-123"})
        assert r.status_code == 400


async def test_convite_invalido(client):
    r = await client.post("/v1/invite/accept",
                          json={"token": "nao-existe", "password": "senha-123456"})
    assert r.status_code == 400


async def test_emitir_convite_invalida_o_anterior(admin, client):
    r = await admin.post("/v1/users", json={"email": "n2@teste.com",
                                            "display_name": "N2"})
    uid = r.json()["id"]
    primeiro = (await admin.post(f"/v1/users/{uid}/invite")).json()["token"]
    segundo = (await admin.post(f"/v1/users/{uid}/invite")).json()["token"]

    r = await client.post("/v1/invite/accept",
                          json={"token": primeiro, "password": "senha-123456"})
    assert r.status_code == 400
    r = await client.post("/v1/invite/accept",
                          json={"token": segundo, "password": "senha-123456"})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Token de API
# --------------------------------------------------------------------------- #

async def test_token_de_api_autentica(admin, client):
    r = await admin.post("/v1/me/tokens", json={"name": "agente-ci"})
    assert r.status_code == 201
    token = r.json()["token"]

    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c2:
        r = await c2.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["via"] == "api_token"


async def test_token_revogado_para_de_funcionar(admin, client):
    r = await admin.post("/v1/me/tokens", json={"name": "temporario"})
    token, token_id = r.json()["token"], r.json()["id"]
    assert (await admin.delete(f"/v1/me/tokens/{token_id}")).status_code == 204

    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c2:
        r = await c2.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


async def test_token_nao_e_guardado_em_claro(admin):
    r = await admin.post("/v1/me/tokens", json={"name": "sigiloso"})
    token = r.json()["token"]
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        rows = conn.execute("SELECT token_sha256 FROM api_token").fetchall()
    assert all(token.encode() not in bytes(row[0]) for row in rows)


async def test_mesmo_email_cadastrado_duas_vezes_ao_mesmo_tempo(admin):
    """Ler-e-inserir não vira atômico por estar numa transação.

    Em READ COMMITTED as duas requisições veem "não existe" e a segunda bate no
    `app_user_email_key`. Sem tratar, a violação sobe pela rede de
    `logs.correlacionar` e o usuário recebe 500 no lugar do 409 que o handler
    pretendia dar.
    """
    import asyncio

    corpo = {"email": "gemeo@teste.com", "display_name": "Gêmeo",
             "password": "senha-de-teste-123"}
    r1, r2 = await asyncio.gather(admin.post("/v1/users", json=corpo),
                                 admin.post("/v1/users", json=corpo))
    assert sorted([r1.status_code, r2.status_code]) == [201, 409], \
        (r1.status_code, r1.text, r2.status_code, r2.text)


async def test_dois_convites_ao_mesmo_tempo_deixam_um_vivo(admin):
    """`revoga o anterior, insere o novo` é ler-modificar-escrever.

    O índice parcial `user_invite_live_idx` garante que só um fique vivo — mas
    garantir com violação de unicidade é 500 na cara de quem clicou duas vezes,
    não o "emitir um novo invalida o anterior" que a rota promete.
    """
    import asyncio

    r = await admin.post("/v1/users", json={"email": "convidado@teste.com",
                                            "display_name": "Convidado"})
    uid = r.json()["id"]
    a, b = await asyncio.gather(admin.post(f"/v1/users/{uid}/invite"),
                                admin.post(f"/v1/users/{uid}/invite"))
    assert [a.status_code, b.status_code] == [201, 201], (a.text, b.text)

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        vivos = conn.execute(
            "SELECT count(*) FROM user_invite"
            " WHERE user_id = %s AND used_at IS NULL AND revoked_at IS NULL",
            (uid,)).fetchone()[0]
    assert vivos == 1


async def test_mesmo_nome_de_token_criado_duas_vezes_ao_mesmo_tempo(admin):
    """`api_token` tem UNIQUE (user_id, name), e o handler lia antes de inserir.

    As duas requisições leem "não existe" — READ COMMITTED — e a segunda
    estoura no índice: 500 em vez do 409 que o handler pretendia dar.
    """
    import asyncio

    corpo = {"name": "ci"}
    r1, r2 = await asyncio.gather(admin.post("/v1/me/tokens", json=corpo),
                                  admin.post("/v1/me/tokens", json=corpo))
    assert sorted([r1.status_code, r2.status_code]) == [201, 409], \
        (r1.status_code, r1.text, r2.status_code, r2.text)
    assert len((await admin.get("/v1/me/tokens")).json()) == 1


# --------------------------------------------------------------------------- #
# /me/sessions — a tela que não tinha teste nenhum
# --------------------------------------------------------------------------- #

async def test_listar_sessoes_pelo_cookie(admin):
    """`user_session.ip` é `inet`, e o psycopg devolve `IPv4Address`.

    O `response_model` declara `str`, então toda sessão com IP — ou seja, toda
    sessão de verdade — reprovava na validação e virava 500. Ninguém pegou
    porque esta rota não tinha teste.
    """
    r = await admin.get("/v1/me/sessions")
    assert r.status_code == 200, r.text
    sessoes = r.json()
    assert len(sessoes) == 1
    assert sessoes[0]["current"] is True
    assert isinstance(sessoes[0]["ip"], str)


async def test_listar_sessoes_por_token_de_api(admin, client):
    """Quem entra por Bearer não tem sessão — e isso não pode ser 500.

    `(id = %s) AS current` com `session_id` nulo dá NULL em toda linha, e o
    modelo declara `bool`. A pergunta "qual destas é a minha?" tem resposta:
    nenhuma.

    Cliente sem cookie de propósito: `principal_from_request` prefere o cookie,
    então mandar o Bearer de um cliente logado não exercitaria este caminho.
    """
    tok = (await admin.post("/v1/me/tokens",
                            json={"name": "leitor"})).json()["token"]
    async with client.__class__(transport=client._transport,
                                base_url="http://test") as c2:
        r = await c2.get("/v1/me/sessions",
                         headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json() and all(s["current"] is False for s in r.json())


async def test_revogar_sessao_duas_vezes_e_idempotente(admin, client, superuser):
    """Repetir o DELETE é o cliente que perdeu a resposta, não erro dele.

    O 404 aqui diria "essa sessão não existe" sobre uma que existe e está
    revogada — e um script que trata 404 como "alguém apagou por baixo" agiria
    em cima de uma informação falsa.
    """
    async with client.__class__(transport=client._transport,
                                base_url="http://test") as outro:
        await outro.post("/v1/login", json={"email": superuser["email"],
                                            "password": superuser["password"]})
        alvo = next(s for s in (await admin.get("/v1/me/sessions")).json()
                    if not s["current"])

    assert (await admin.delete(
        f"/v1/me/sessions/{alvo['id']}")).status_code == 204
    assert (await admin.delete(
        f"/v1/me/sessions/{alvo['id']}")).status_code == 204


async def test_nome_de_token_revogado_pode_ser_reusado(admin):
    """O UNIQUE cobria linha revogada; a listagem, não.

    O nome sumia da tela e seguia bloqueando: o usuário via `[]` e recebia
    "já existe token chamado 'ci'" sobre algo que não conseguia enxergar em
    lugar nenhum.
    """
    tid = (await admin.post("/v1/me/tokens", json={"name": "ci"})).json()["id"]
    assert (await admin.delete(f"/v1/me/tokens/{tid}")).status_code == 204
    assert (await admin.get("/v1/me/tokens")).json() == []

    r = await admin.post("/v1/me/tokens", json={"name": "ci"})
    assert r.status_code == 201, r.text


async def test_sessao_ociosa_fica_revogada_no_banco(admin):
    """O 401 por inatividade tem de DEIXAR a sessão revogada.

    Revogar na conexão de quem vai levantar a exceção não vale: o pool faz
    `ROLLBACK` ao vê-la subir, e a sessão segue `revoked_at IS NULL` para
    sempre — continua aparecendo aberta em `/me/sessions` e cada requisição
    repete a mesma escrita desfeita. É a armadilha de `record_attempt`, cem
    linhas abaixo no mesmo arquivo.
    """
    assert (await admin.get("/v1/me")).status_code == 200
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("UPDATE user_session SET last_seen_at = now()"
                     " - make_interval(hours => %s)",
                     (settings.session_idle_hours + 1,))

    assert (await admin.get("/v1/me")).status_code == 401
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        vivas = conn.execute("SELECT count(*) FROM user_session"
                             " WHERE revoked_at IS NULL").fetchone()[0]
    assert vivas == 0, "sessão ociosa continuou aberta no banco"


@contextlib.asynccontextmanager
async def _contando_conexoes(monkeypatch):
    """Profundidade máxima de conexões que UMA task segura ao mesmo tempo.

    Por task, e não global: o laço de importação embutido na API tem conexão
    própria e polla a cada dois segundos, então um contador único mediria a
    coincidência de dois fluxos independentes em vez do aninhamento de um só.
    """
    original = db.connection
    por_task: dict[int, int] = {}
    maxima = 0

    @contextlib.asynccontextmanager
    async def contando():
        nonlocal maxima
        task = id(asyncio.current_task())
        por_task[task] = por_task.get(task, 0) + 1
        maxima = max(maxima, por_task[task])
        try:
            async with original() as conn:
                yield conn
        finally:
            por_task[task] -= 1

    monkeypatch.setattr(db, "connection", contando)
    medida: list[int] = []
    yield medida
    medida.append(maxima)


@pytest.mark.parametrize("senha, esperado", [("senha-de-teste-123", 200),
                                             ("errada", 401)])
async def test_login_nunca_segura_duas_conexoes_do_pool(client, superuser,
                                                        monkeypatch, senha,
                                                        esperado):
    """Uma conexão de cada vez, nos dois desfechos do login.

    `record_attempt` pega conexão própria de propósito. Chamada de dentro de
    uma conexão já tomada, ela vira a segunda: com dez logins simultâneos as
    dez vagas do pool ficam presas esperando a décima primeira, e o
    `PoolTimeout` transforma dez 401 em dez 500 — justamente sob a rajada que
    o throttle existe para atender.
    """
    async with _contando_conexoes(monkeypatch) as medida:
        r = await client.post("/v1/login", json={"email": superuser["email"],
                                                 "password": senha})
    assert r.status_code == esperado
    assert medida[0] == 1, f"o login segurou {medida[0]} conexões ao mesmo tempo"
