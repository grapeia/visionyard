-- Parâmetros da instalação que o superusuário edita na tela.
--
-- Só o que muda com o uso e não é cerca de segurança: buckets, onde publicar
-- modelos, validade da URL do agente. O que decide como o processo sobe
-- (DSN, cookie, proxies, import_root) continua em variável de ambiente, e
-- quando a variável está definida ela vence esta tabela — o serviço trava o
-- campo na tela em vez de deixar um clique desfazer um deploy declarativo.
--
-- Credencial do GCS não entra aqui de propósito: guardar a chave no banco faz
-- de um dump ou backup um acesso ao bucket. Na nuvem a API usa a identidade da
-- própria VM; a tela só mostra qual é e o que ela consegue fazer.
BEGIN;

CREATE TABLE installation_setting (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- SET NULL e não CASCADE: excluir quem mudou o parâmetro não pode desfazer
    -- a mudança. Quem foi fica no audit_log.
    updated_by uuid        REFERENCES app_user(id) ON DELETE SET NULL
);

COMMIT;
