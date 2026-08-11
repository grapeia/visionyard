-- 007_dataset_source.sql — as origens de um dataset.
--
-- Um dataset é composto por várias origens (prefixos de bucket, importações do
-- Label Studio, uploads). Guardar a referência ao `data_source` em vez do texto
-- do prefixo tem duas consequências boas:
--
--   * congelar resolve por consulta ao banco, sem relistar o bucket — que é
--     caro em prefixo amplo;
--   * remover uma origem é só apagar a linha daqui: nenhum sample e nenhuma
--     anotação são tocados, e as versões já congeladas seguem íntegras.
--     "Remover" significa "parar de incluir nas próximas versões".

BEGIN;

CREATE TABLE dataset_source (
    dataset_id     uuid        NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    data_source_id uuid        NOT NULL REFERENCES data_source(id) ON DELETE RESTRICT,
    added_at       timestamptz NOT NULL DEFAULT now(),
    added_by       uuid        REFERENCES app_user(id),
    PRIMARY KEY (dataset_id, data_source_id)
);
CREATE INDEX dataset_source_source_idx ON dataset_source (data_source_id);

-- Nome legível da origem, para listar na tela sem depender do URI cru.
ALTER TABLE data_source ADD COLUMN name text;
UPDATE data_source SET name = uri WHERE name IS NULL;

-- Uma origem é identificada pelo que ela aponta: reimportar o mesmo prefixo
-- deve reaproveitar a origem existente em vez de criar uma duplicada, senão
-- o mesmo bucket vira duas entradas na tela do dataset.
CREATE UNIQUE INDEX data_source_project_uri_key
    ON data_source (project_id, kind, uri) WHERE uri IS NOT NULL;

COMMIT;
