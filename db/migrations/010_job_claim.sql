-- 010 — o que falta para o agente reclamar e executar jobs (Fase 3.1).
--
-- O schema do ciclo de treino já existe desde a 002 (job, device, training_run,
-- run_metric, model_version…). Esta migration só acrescenta o que o *claim*
-- precisa para ser correto e barato, e fecha a FK que a 002 deixou adiante.

BEGIN;

-- O claim busca o job de maior prioridade na fila. Sem índice parcial, cada
-- poll de agente varre a tabela inteira de jobs (que cresce e nunca encolhe) só
-- para achar os poucos em 'queued'. O índice parcial indexa apenas a fila viva.
CREATE INDEX IF NOT EXISTS job_queued_idx
    ON job (priority, created_at) WHERE status = 'queued';

-- Reaproveitar job órfão: agente que morreu no meio deixa o job em
-- 'claimed'/'running' com heartbeat velho. O claim também procura por esses,
-- e este índice evita varredura para achá-los.
CREATE INDEX IF NOT EXISTS job_running_idx
    ON job (heartbeat_at) WHERE status IN ('claimed', 'running');

-- Stream do run: a tela lê eventos e métricas incrementalmente (desde o último
-- seq/epoch visto). Sem estes, cada atualização relê tudo.
CREATE INDEX IF NOT EXISTS job_event_job_idx ON job_event (job_id, seq);
CREATE INDEX IF NOT EXISTS run_metric_run_idx ON run_metric (run_id, epoch);

-- training_run.base_model_version_id nasceu sem FK ("FK adiante" na 002, porque
-- model_version é criada mais abaixo no mesmo arquivo). Agora as duas existem:
-- fechar a FK impede apontar um treino para uma versão de modelo que não existe.
-- NULL continua válido (base externa: 'yolo11n.pt' fica em base_model_name).
--
-- Guardado por IF NOT EXISTS porque em algum ambiente a FK já foi criada à mão:
-- ADD CONSTRAINT cru abortaria a migration lá, e o objetivo é o estado final,
-- não recriar o objeto.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'training_run_base_model_fk') THEN
        ALTER TABLE training_run
            ADD CONSTRAINT training_run_base_model_fk
            FOREIGN KEY (base_model_version_id) REFERENCES model_version(id);
    END IF;
END $$;

COMMIT;
