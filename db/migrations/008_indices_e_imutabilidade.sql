-- 008 — corrige a 006, adiciona índices e impõe a imutabilidade prometida.
--
-- A 006 tem dois defeitos que só aparecem em base COM dados:
--
--   1. Ela faz `UPDATE sample`, mas `sample` tem trigger de imutabilidade
--      (002). Em base com uma linha sequer, a migration aborta e o sistema
--      fica preso na 005. Verificado: falha com "registro imutável".
--   2. O `UPDATE` preenchia md5 com `substring(sha256 ...)` — um valor que NÃO
--      é o md5 do arquivo. Reimportar a mesma imagem calcularia o md5
--      verdadeiro, não acharia a linha, e criaria um SEGUNDO sample; as
--      anotações ficariam presas ao primeiro e a imagem sairia do dataset em
--      silêncio.
--
-- Corrigir a 006 no lugar quebraria o checksum nas bases que já a aplicaram —
-- e a recusa a migration editada é uma garantia que vale manter. Então a
-- correção vem para a frente: md5 passa a ser nullable (linha sem md5 é
-- pendente de backfill, identificada pelo sha256) e a unicidade vira índice
-- parcial.

BEGIN;

ALTER TABLE sample ALTER COLUMN md5 DROP NOT NULL;
ALTER TABLE sample DROP CONSTRAINT IF EXISTS sample_project_md5_key;
CREATE UNIQUE INDEX IF NOT EXISTS sample_project_md5_key
    ON sample (project_id, md5) WHERE md5 IS NOT NULL;

-- Denuncia linhas com o placeholder da 006, se alguma base tiver: md5 de 16
-- bytes que é prefixo do próprio sha256 não veio de arquivo nenhum.
DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM sample
     WHERE md5 IS NOT NULL AND sha256 IS NOT NULL
       AND md5 = substring(sha256 from 1 for 16);
    IF n > 0 THEN
        RAISE WARNING 'ATENÇÃO: % samples com md5 placeholder da migration 006. '
            'Reimportar cria duplicata em vez de reconhecer a imagem. '
            'Zere o md5 dessas linhas antes de reimportar.', n;
    END IF;
END $$;

-- `sample.source_id` era FK sem índice, no caminho mais quente do sistema:
-- resolve_items faz JOIN por ela (preview E congelamento), e a tela do dataset
-- roda um count(*) por origem. Com centenas de milhares de imagens, cada
-- abertura de tela virava varredura sequencial na maior tabela do schema.
CREATE INDEX IF NOT EXISTS sample_source_idx ON sample (source_id);

-- FKs sem índice usadas em consultas de linhagem e em DELETE do pai.
CREATE INDEX IF NOT EXISTS annotation_schema_idx  ON annotation (schema_id);
CREATE INDEX IF NOT EXISTS annotation_author_idx  ON annotation (author_id);
CREATE INDEX IF NOT EXISTS dsv_item_annotation_idx
    ON dataset_version_item (annotation_id);
CREATE INDEX IF NOT EXISTS evaluation_dsv_idx ON evaluation (dataset_version_id);
CREATE INDEX IF NOT EXISTS training_run_base_idx
    ON training_run (base_model_version_id);

-- Imutabilidade cobria só UPDATE. DELETE passava em todas as tabelas ditas
-- imutáveis — e apagar é pior que alterar, porque o cascade leva junto.
CREATE OR REPLACE FUNCTION deny_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'registro imutável: % id=% não pode ser % (crie uma nova versão)',
        TG_TABLE_NAME, OLD.id,
        CASE TG_OP WHEN 'DELETE' THEN 'apagado' ELSE 'alterado' END;
END $$;

DROP TRIGGER IF EXISTS label_schema_immutable ON label_schema;
CREATE TRIGGER label_schema_immutable BEFORE UPDATE OR DELETE ON label_schema
    FOR EACH ROW EXECUTE FUNCTION deny_update();
DROP TRIGGER IF EXISTS dataset_version_immutable ON dataset_version;
CREATE TRIGGER dataset_version_immutable
    BEFORE UPDATE OR DELETE ON dataset_version
    FOR EACH ROW EXECUTE FUNCTION deny_update();
DROP TRIGGER IF EXISTS model_build_immutable ON model_build;
CREATE TRIGGER model_build_immutable BEFORE UPDATE OR DELETE ON model_build
    FOR EACH ROW EXECUTE FUNCTION deny_update();
DROP TRIGGER IF EXISTS evaluation_immutable ON evaluation;
CREATE TRIGGER evaluation_immutable BEFORE UPDATE OR DELETE ON evaluation
    FOR EACH ROW EXECUTE FUNCTION deny_update();

-- model_version é um dos três artefatos que o plano declara imutáveis e era o
-- único sem trigger: um UPDATE reapontaria os pesos de uma versão já avaliada
-- e implantada, sem deixar rastro.
DROP TRIGGER IF EXISTS model_version_immutable ON model_version;
CREATE TRIGGER model_version_immutable
    BEFORE UPDATE OR DELETE ON model_version
    FOR EACH ROW EXECUTE FUNCTION deny_update();

-- O conteúdo da versão congelada também não pode mudar: content_hash protege o
-- manifesto no disco, mas o índice consultável que responde o diff não tinha
-- proteção nenhuma — diff e manifesto podiam divergir em silêncio.
CREATE OR REPLACE FUNCTION deny_change_item() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'itens de uma versão congelada não podem ser alterados';
END $$;
DROP TRIGGER IF EXISTS dsv_item_immutable ON dataset_version_item;
CREATE TRIGGER dsv_item_immutable
    BEFORE UPDATE OR DELETE ON dataset_version_item
    FOR EACH ROW EXECUTE FUNCTION deny_change_item();

COMMIT;
