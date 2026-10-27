-- O que o modelo previu em cada imagem do benchmark, para a galeria da
-- avaliação.
--
-- As imagens não são copiadas: o benchmark é uma versão congelada, e as
-- imagens dela já estão no bucket, com a URI em `sample`. O que se guarda é a
-- predição e o gabarito por imagem, que são pequenos, de TODAS as imagens e
-- não só das que erraram. É isso que permite comparar duas avaliações imagem
-- a imagem, já que o que uma acerta a outra pode errar.
--
-- O gabarito vem do manifesto que o agente mediu, e não da anotação de hoje.
-- `dataset_version_item.annotation_id` aponta para uma anotação que continua
-- editável depois do congelamento, e a galeria tem de mostrar contra o que a
-- avaliação mediu de fato.
--
-- Só INSERT, como a própria `evaluation` (008): reavaliar cria outra avaliação.
-- A função é própria, e não `deny_update()`: aquela cita `OLD.id` na mensagem,
-- e esta tabela não tem `id`. Ela ainda barraria, mas com um erro de campo
-- inexistente no lugar do motivo; é o mesmo caso de `dataset_version_item`.
BEGIN;

CREATE TABLE evaluation_prediction (
    evaluation_id uuid    NOT NULL REFERENCES evaluation(id) ON DELETE CASCADE,
    sample_id     uuid    NOT NULL REFERENCES sample(id),
    -- linhas do manifesto: [cls, cx, cy, w, h], ou [cls, x1, y1, ...] em
    -- segmentação; coordenadas do quadro cheio
    gt            jsonb   NOT NULL,
    -- [cls, conf, cx, cy, w, h], do quadro cheio mesmo com ROI
    pred          jsonb   NOT NULL,
    fp            integer NOT NULL CHECK (fp >= 0),
    fn            integer NOT NULL CHECK (fn >= 0),
    swapped       integer NOT NULL CHECK (swapped >= 0),
    PRIMARY KEY (evaluation_id, sample_id)
);
CREATE FUNCTION deny_change_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'predições de uma avaliação não podem ser alteradas';
END $$;
CREATE TRIGGER evaluation_prediction_immutable
    BEFORE UPDATE OR DELETE ON evaluation_prediction
    FOR EACH ROW EXECUTE FUNCTION deny_change_prediction();

COMMIT;
