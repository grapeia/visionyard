-- 005_job_result.sql — resultado e progresso de job.
--
-- Todo job produz algo que o usuário precisa ler depois: a importação produz um
-- relatório (o que entrou, o que foi pulado e por quê), o treino produz
-- métricas, o export produz o build. Guardar isso em `job` evita uma tabela de
-- resultado por tipo de job.
--
-- `progress` é sobrescrito durante a execução (quantos de quantos);
-- `result` só é escrito no fim.

BEGIN;

ALTER TABLE job
    ADD COLUMN progress jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN result   jsonb;

-- Importação é sempre relativa a uma origem de dados; o dry-run não cria
-- DataSource, então a coluna é opcional.
ALTER TABLE job
    ADD COLUMN data_source_id uuid REFERENCES data_source(id);

COMMIT;
