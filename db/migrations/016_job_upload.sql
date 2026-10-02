-- Os destinos de publicação que a API entregou a cada job.
--
-- O registro do modelo conferia o URI contra o caminho RECALCULADO na hora,
-- a partir de bucket e prefixo — que agora são parâmetros editáveis na tela,
-- com cache por processo. Se mudassem entre o `/upload` e o `/model` (ou se
-- os dois caíssem em workers com cache diferente), o peso de um treino de
-- horas subia no caminho velho e o registro respondia 422: job falho, peso
-- órfão no bucket. Conferir contra o que foi de fato entregue não depende do
-- valor do parâmetro no segundo seguinte.
BEGIN;

CREATE TABLE job_upload (
    job_id     uuid        NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    uri        text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, uri)
);

COMMIT;
