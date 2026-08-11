-- 006_sample_md5.sql — identidade de conteúdo que não exige baixar a imagem.
--
-- O schema original exigia sha256 do sample. Isso funciona quando o arquivo
-- está em disco, mas quebra no caso principal: importar um prefixo de bucket.
-- Calcular sha256 de 500 mil objetos significa **baixar todos** — centenas de
-- gigabytes só para catalogar.
--
-- O GCS já expõe o md5 de cada objeto nos metadados, de graça, na mesma
-- listagem. Então md5 vira a chave de deduplicação (é o que toda origem
-- consegue fornecer barato) e sha256 fica opcional, preenchido quando os bytes
-- passaram pela nossa mão de qualquer jeito.
--
-- md5 aqui não é escolha de segurança: o papel dele é deduplicar, detectar
-- troca acidental de conteúdo e servir de chave de cache. Não há adversário
-- forjando colisão contra o próprio bucket da empresa.

BEGIN;

ALTER TABLE sample ADD COLUMN md5 bytea;

-- Preenche o que já existe (importado de arquivo local, tem sha256 mas não md5)
-- com um placeholder derivado, para poder aplicar o NOT NULL. Em base nova
-- não há linha nenhuma; em base com dados, reimportar recalcula.
UPDATE sample SET md5 = substring(sha256 from 1 for 16) WHERE md5 IS NULL;

ALTER TABLE sample ALTER COLUMN md5 SET NOT NULL;
ALTER TABLE sample ALTER COLUMN sha256 DROP NOT NULL;

-- A unicidade passa a ser por md5: é a chave que toda origem fornece.
ALTER TABLE sample DROP CONSTRAINT sample_project_id_sha256_key;
ALTER TABLE sample ADD CONSTRAINT sample_project_md5_key UNIQUE (project_id, md5);

-- sha256 continua único quando presente, mas não é mais obrigatório.
CREATE UNIQUE INDEX sample_project_sha256_key ON sample (project_id, sha256)
    WHERE sha256 IS NOT NULL;

-- Mesma razão: descobrir largura/altura exige abrir a imagem. Catalogar um
-- prefixo não pode depender disso. Ficam nulas até alguém abrir o arquivo
-- (anotar já preenche), e o congelamento avisa se faltarem.
ALTER TABLE sample ALTER COLUMN width  DROP NOT NULL;
ALTER TABLE sample ALTER COLUMN height DROP NOT NULL;

COMMENT ON COLUMN sample.md5 IS
    'Identidade de conteúdo. Vem dos metadados do GCS (sem download) ou é '
    'calculada localmente. Chave de deduplicação.';
COMMENT ON COLUMN sample.sha256 IS
    'Opcional: preenchido só quando os bytes foram lidos localmente.';

COMMIT;
