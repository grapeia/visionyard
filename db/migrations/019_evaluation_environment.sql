-- Onde a avaliação foi medida, e com que versão do agente se treinou.
--
-- O tempo de inferência de uma avaliação só se compara com outro medido no
-- mesmo hardware, com o mesmo driver e o mesmo runtime. `evaluation` é
-- imutável (008), então o ambiente entra no INSERT, como foto do momento da
-- medição — ler de `device` na hora de mostrar daria o driver de hoje, não o
-- daquela medição.
--
-- `device.agent_version` guarda o que o agente já mandava no registro e a API
-- descartava. É de onde sai `training_run.code_version`, que existia desde a
-- 002 e nunca foi preenchida, e o fallback do ambiente do agente 0.2, que não
-- o relata.
BEGIN;

ALTER TABLE device ADD COLUMN agent_version text;

-- ADD COLUMN com DEFAULT constante não reescreve a tabela nem dispara o
-- trigger de UPDATE da 008.
ALTER TABLE evaluation ADD COLUMN environment jsonb NOT NULL DEFAULT '{}';

COMMIT;
