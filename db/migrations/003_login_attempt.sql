-- 003_login_attempt.sql — throttling de login.
--
-- Sem isto, a tela de login é um oráculo de força bruta. Contagem em tabela
-- (e não em memória do processo) porque a API vai rodar com mais de um worker
-- e um contador por processo não limita nada.

BEGIN;

CREATE TABLE login_attempt (
    id         bigserial PRIMARY KEY,
    email      text        NOT NULL,   -- como digitado, em minúsculas
    ip         inet,
    at         timestamptz NOT NULL DEFAULT now(),
    successful boolean     NOT NULL
);

-- Consulta é sempre "tentativas falhas recentes para este email/IP".
CREATE INDEX login_attempt_email_idx ON login_attempt (email, at DESC)
    WHERE NOT successful;
CREATE INDEX login_attempt_ip_idx ON login_attempt (ip, at DESC)
    WHERE NOT successful;

COMMIT;
