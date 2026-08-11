-- 001_identity.sql — usuários, sessões, tokens, papéis e auditoria.
--
-- Convenções usadas em todo o schema:
--   * PK uuid via gen_random_uuid() (nativo no PG13+, sem pgcrypto)
--   * timestamptz sempre, nunca timestamp
--   * enums como text + CHECK, não como tipo ENUM: acrescentar valor numa
--     constraint é um ALTER simples, enquanto remover/reordenar valor de um
--     tipo ENUM no Postgres é doloroso. Schema que ainda vai evoluir usa CHECK.
--   * segredo nunca é guardado em claro — só o sha256 do token.

BEGIN;

CREATE TABLE app_user (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           text        NOT NULL,
    display_name    text        NOT NULL,
    -- string com algoritmo embutido: "$argon2id$v=19$m=...$salt$hash".
    -- Guardar o algoritmo junto permite migrar de hash sem quebrar logins
    -- antigos (verifica com o algoritmo do registro, re-hasheia no login).
    password_hash   text,
    status          text        NOT NULL DEFAULT 'invited'
                    CHECK (status IN ('invited','active','disabled')),
    is_superuser    boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid        REFERENCES app_user(id),
    last_login_at   timestamptz,
    disabled_at     timestamptz
);

-- Email case-insensitive sem depender da extensão citext.
CREATE UNIQUE INDEX app_user_email_key ON app_user (lower(email));

COMMENT ON COLUMN app_user.password_hash IS
    'NULL enquanto status=invited (usuário ainda não definiu senha)';


-- Sessão de navegador. Token opaco em cookie httpOnly, estado no servidor.
--
-- Escolha deliberada de sessão server-side em vez de JWT: JWT não é revogável
-- antes de expirar, então "desativar usuário" vira um problema. Com tabela de
-- sessão, revogar é um UPDATE. No volume desta aplicação o custo do SELECT por
-- request é irrelevante e a operação fica muito mais simples.
CREATE TABLE user_session (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    token_sha256 bytea       NOT NULL UNIQUE,
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,
    ip           inet,
    user_agent   text
);
CREATE INDEX user_session_user_idx ON user_session (user_id)
    WHERE revoked_at IS NULL;


-- Token de API pessoal: script/CLI agindo COMO um usuário (herda os papéis
-- dele). Não confundir com device_token (002), que é um principal de máquina.
CREATE TABLE api_token (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name         text        NOT NULL,
    prefix       text        NOT NULL,   -- 8 chars visíveis p/ identificar
    token_sha256 bytea       NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    last_used_at timestamptz,
    revoked_at   timestamptz,
    UNIQUE (user_id, name)
);

COMMENT ON TABLE api_token IS
    'Token exibido UMA vez na criação; o banco guarda só o sha256.';


-- ---------------------------------------------------------------------------
-- Papéis
--
-- RBAC escopado por projeto. Um usuário pode ser annotator num projeto e
-- engineer noutro — que é o caso real quando entra gente terceirizada só
-- para rotular.
--
--   owner     tudo, incl. gerenciar membros e arquivar o projeto
--   admin     tudo menos arquivar/deletar o projeto
--   engineer  datasets, treinos, modelos, builds, deploy
--   annotator anotar e revisar; leitura no resto
--   viewer    só leitura
-- ---------------------------------------------------------------------------
CREATE TABLE project_member (
    project_id uuid        NOT NULL,   -- FK adicionada em 002 (project vem lá)
    user_id    uuid        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role       text        NOT NULL
               CHECK (role IN ('owner','admin','engineer','annotator','viewer')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by uuid        REFERENCES app_user(id),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX project_member_user_idx ON project_member (user_id);


-- ---------------------------------------------------------------------------
-- Auditoria
--
-- Cobre o que é mutável-mas-auditável (mover alias, promover deployment,
-- conceder papel, mudar schema de labels). O que é imutável não precisa de
-- auditoria — a própria versão é o registro. E annotation_history (002) é
-- separado por ser alto volume e ter formato próprio.
--
-- actor_type distingue os dois tipos de principal do sistema: o humano e a
-- máquina. Sem isso não dá para responder "quem promoveu esse modelo" quando
-- metade das ações vem de agentes automáticos.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor_type  text        NOT NULL CHECK (actor_type IN ('user','device','system')),
    actor_id    uuid,                    -- app_user.id ou device.id
    actor_label text        NOT NULL,    -- denormalizado: sobrevive a delete
    project_id  uuid,
    action      text        NOT NULL,    -- 'model.alias.move', 'member.grant', …
    entity_type text        NOT NULL,
    entity_id   uuid,
    before      jsonb,
    after       jsonb,
    ip          inet,
    note        text
);
CREATE INDEX audit_log_entity_idx  ON audit_log (entity_type, entity_id, at DESC);
CREATE INDEX audit_log_project_idx ON audit_log (project_id, at DESC);
CREATE INDEX audit_log_actor_idx   ON audit_log (actor_type, actor_id, at DESC);


-- ---------------------------------------------------------------------------
-- Imutabilidade no banco, não só na aplicação.
--
-- DatasetVersion, ModelVersion, ModelBuild, Evaluation e TrainingRun concluído
-- são imutáveis por contrato (ver docs/modelo-de-dominio.md §8). Deixar isso
-- só na camada de aplicação significa que um UPDATE manual no psql às 2h da
-- manhã destrói silenciosamente a reprodutibilidade. O trigger é a rede de
-- proteção.
-- ---------------------------------------------------------------------------
CREATE FUNCTION deny_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'registro imutável: % id=% não pode ser alterado (crie uma nova versão)',
        TG_TABLE_NAME, OLD.id;
END $$;

COMMIT;
