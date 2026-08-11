-- 004_user_invite.sql — convite e redefinição de senha.
--
-- Um usuário criado sem senha nasce 'invited' e precisa de um caminho para
-- definir a primeira senha. O mesmo mecanismo serve para "esqueci minha senha",
-- então a tabela carrega `purpose` em vez de existir duas vezes.
--
-- Token de uso único, com validade: guardamos só o sha256, como todo segredo
-- deste schema.

BEGIN;

CREATE TABLE user_invite (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    purpose      text        NOT NULL CHECK (purpose IN ('invite','password_reset')),
    token_sha256 bytea       NOT NULL UNIQUE,
    expires_at   timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   uuid        REFERENCES app_user(id),
    used_at      timestamptz,
    revoked_at   timestamptz
);

-- Só um convite vivo por usuário/propósito: emitir um novo invalida o anterior
-- (a aplicação revoga antes de inserir; o índice garante).
CREATE UNIQUE INDEX user_invite_live_idx ON user_invite (user_id, purpose)
    WHERE used_at IS NULL AND revoked_at IS NULL;

COMMIT;
