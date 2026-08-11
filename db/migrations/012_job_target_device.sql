-- 012_job_target_device.sql — fixar um job numa máquina específica.
--
-- Até aqui o roteamento era só por capacidade: qualquer máquina cujo
-- `capabilities @> job.requires` podia reclamar o job, e o claim entregava para
-- a primeira livre do pool. É o que se quer na maioria das vezes.
--
-- Às vezes, porém, o engenheiro quer ESTA máquina — a 4090 para um treino
-- pesado, um device de borda específico para reproduzir um bug. Aí não basta a
-- capacidade: o job tem de esperar aquela máquina, mesmo que ela esteja ocupada
-- com outro job agora.
--
-- `target_device_id` expressa esse desejo. É distinto de `device_id` (que grava
-- quem de fato EXECUTOU): um é o alvo pedido, o outro o executor real.
--
--   * NULL           — sem preferência; qualquer máquina capaz pega (pool).
--   * = device X      — só a máquina X reclama. Se X está ocupada, o job fica
--                       'queued' até ela fazer poll estando livre — a espera
--                       "até liberar" cai naturalmente do modelo de claim, sem
--                       fila extra.
--
-- O filtro entra no SELECT do claim (api/routers/agents.py):
--   AND (j.target_device_id IS NULL OR j.target_device_id = <máquina que pede>)

ALTER TABLE job
    ADD COLUMN target_device_id uuid REFERENCES device(id);

COMMENT ON COLUMN job.target_device_id IS
    'máquina desejada: quando não-nula, só ela reclama o job (espera liberar). '
    'Distinta de device_id, que grava quem executou.';
