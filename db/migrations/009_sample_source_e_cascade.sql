-- 009 — pertencimento a várias origens, e cascade que apagava linhagem.
--
-- (1) `sample.source_id` é escalar: um sample pertence a UMA origem. Combinado
--     com a deduplicação por md5, isso produz perda silenciosa — a mesma foto
--     presente em `gs://b/lote-jan/` e `gs://b/curadoria/` fica presa à origem
--     de onde veio primeiro. Adicionar só a origem curada a um dataset resolve
--     zero imagens, e a tela mostra "0 img" sem explicar por quê. Foi o que o
--     usuário viu na primeira tela do produto.
--
--     `sample.source_id` passa a significar PROCEDÊNCIA (onde foi visto pela
--     primeira vez) e `sample_source` passa a significar PERTENCIMENTO.
--
-- (2) Apagar um `model` cascateava para versões, builds e avaliações — as três
--     entidades que o plano declara imutáveis, destruídas por um comando. E
--     `annotation_history`, que existe justamente para provar quem mexeu num
--     box, era apagado junto com o box. Arquivar já é o caminho previsto
--     (`model.archived_at`), então apagar passa a ser recusado.

BEGIN;

-- --------------------------------------------------------------- (1) -------

CREATE TABLE sample_source (
    sample_id      uuid        NOT NULL REFERENCES sample(id) ON DELETE CASCADE,
    data_source_id uuid        NOT NULL REFERENCES data_source(id) ON DELETE RESTRICT,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sample_id, data_source_id)
);
CREATE INDEX sample_source_source_idx ON sample_source (data_source_id);

-- Backfill: o que já existe pertence à origem de onde veio.
INSERT INTO sample_source (sample_id, data_source_id, first_seen_at)
SELECT id, source_id, imported_at FROM sample WHERE source_id IS NOT NULL
ON CONFLICT DO NOTHING;

COMMENT ON COLUMN sample.source_id IS
    'Procedência: onde a imagem foi vista pela primeira vez. Para saber a '
    'quais origens ela pertence hoje, use sample_source.';

-- --------------------------------------------------------------- (2) -------

ALTER TABLE model_version DROP CONSTRAINT model_version_model_id_fkey;
ALTER TABLE model_version ADD CONSTRAINT model_version_model_id_fkey
    FOREIGN KEY (model_id) REFERENCES model(id) ON DELETE RESTRICT;

ALTER TABLE model_build DROP CONSTRAINT model_build_model_version_id_fkey;
ALTER TABLE model_build ADD CONSTRAINT model_build_model_version_id_fkey
    FOREIGN KEY (model_version_id) REFERENCES model_version(id) ON DELETE RESTRICT;

ALTER TABLE evaluation DROP CONSTRAINT evaluation_model_version_id_fkey;
ALTER TABLE evaluation ADD CONSTRAINT evaluation_model_version_id_fkey
    FOREIGN KEY (model_version_id) REFERENCES model_version(id) ON DELETE RESTRICT;

-- Descomissionar uma Jetson não pode apagar o histórico de qual build rodou
-- nela: é a ponta esquerda da cadeia de linhagem.
ALTER TABLE deployment DROP CONSTRAINT deployment_device_id_fkey;
ALTER TABLE deployment ADD CONSTRAINT deployment_device_id_fkey
    FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE RESTRICT;

-- O histórico de anotação sobrevive à anotação. Mesmo padrão do audit_log, que
-- não tem FK e denormaliza o rótulo do ator justamente para não sumir junto.
ALTER TABLE annotation_history DROP CONSTRAINT annotation_history_annotation_id_fkey;
ALTER TABLE annotation_history ADD COLUMN sample_id uuid;
UPDATE annotation_history h SET sample_id = a.sample_id
  FROM annotation a WHERE a.id = h.annotation_id AND h.sample_id IS NULL;

COMMIT;
