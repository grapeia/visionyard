-- O nome do token de API só precisa ser único entre os VIVOS.
--
-- O UNIQUE (user_id, name) da 001 cobria linha revogada, mas `GET /me/tokens`
-- lista só `revoked_at IS NULL`: revogar um token fazia o nome sumir da tela e
-- continuar bloqueando. O usuário via a lista vazia e recebia "já existe token
-- chamado 'ci'" sobre algo que não conseguia enxergar em lugar nenhum.
--
-- Com o índice parcial, a restrição passa a dizer a mesma coisa que a tela. As
-- linhas revogadas seguem no banco — elas são o histórico de quem teve acesso.
BEGIN;

ALTER TABLE api_token DROP CONSTRAINT api_token_user_id_name_key;

CREATE UNIQUE INDEX api_token_nome_vivo_idx ON api_token (user_id, name)
    WHERE revoked_at IS NULL;

COMMIT;
