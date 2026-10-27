-- O que cada papel pode fazer, editável na tela do superusuário.
--
-- O catálogo de ações e o papel mínimo de cada uma moram no código
-- (`api/services/permissions.py`); esta tabela guarda só o que o superusuário
-- mudou em relação a esse padrão. Ação nova entra no código já com padrão e
-- vale sem linha nenhuma aqui — e "restaurar" é apagar a linha, não lembrar
-- qual era o padrão.
--
-- `owner` fica de fora do CHECK de propósito: owner pode tudo, sempre. Sem
-- isso uma exceção mal pensada deixaria um projeto sem ninguém capaz de
-- arquivá-lo ou de gerir os membros por dentro do produto.
BEGIN;

CREATE TABLE role_permission (
    role       text        NOT NULL
               CHECK (role IN ('viewer','annotator','engineer','admin')),
    action     text        NOT NULL,
    allowed    boolean     NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- SET NULL e não CASCADE: excluir quem mudou a permissão não pode desfazer
    -- a mudança. Quem foi fica no audit_log.
    updated_by uuid        REFERENCES app_user(id) ON DELETE SET NULL,
    PRIMARY KEY (role, action)
);

COMMIT;
