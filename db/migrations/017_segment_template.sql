-- Gabarito de cortes da segmentação: as linhas que dividem uma classe
-- principal (a banda) nos recortes dela, uma semente por pedaço com a
-- classe, e as regiões desenhadas à parte (laços: picanha, maminha) com a
-- classe delas. Em coordenadas 0–1 da caixa da principal, para servir a
-- qualquer banda; o anotador aplica e a pessoa ajusta.
--
-- Um por (projeto, classe principal), sobrescrito a cada gravação: é
-- ferramenta de anotação, não dado versionado — nenhuma anotação ou
-- manifesto aponta para ele. `class_index` é o índice no LabelSchema, que é
-- append-only, então o índice de uma classe não muda.
BEGIN;

CREATE TABLE segment_template (
    project_id  uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    class_index int         NOT NULL CHECK (class_index >= 0),
    lines       jsonb       NOT NULL,
    seeds       jsonb       NOT NULL,
    shapes      jsonb       NOT NULL DEFAULT '[]',
    updated_by  uuid        REFERENCES app_user(id) ON DELETE SET NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, class_index)
);

COMMIT;
