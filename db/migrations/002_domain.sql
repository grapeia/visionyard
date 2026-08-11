-- 002_domain.sql — as cinco camadas de docs/modelo-de-dominio.md.
--
-- Ordem: Project → LabelSchema → Sample/Annotation → Dataset/Version →
--        Device/Job → Experiment/Run → Model/Version/Alias/Build → Evaluation
--        → Deployment. Cada nível só depende dos anteriores.
--
-- Atribuição: toda entidade causada por uma pessoa carrega created_by. Jobs
-- distinguem created_by (quem pediu) de device_id (quem executou).

BEGIN;

-- ===========================================================================
-- PROJETO E VOCABULÁRIO
-- ===========================================================================

CREATE TABLE project (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid,                    -- reservado p/ multi-tenant futuro
    slug        text        NOT NULL UNIQUE,
    name        text        NOT NULL,
    task_type   text        NOT NULL CHECK (task_type IN ('detect','segment','classify')),
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid        REFERENCES app_user(id),
    archived_at timestamptz
);

ALTER TABLE project_member
    ADD CONSTRAINT project_member_project_fk
    FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE;


-- Classes versionadas. Nunca se edita uma versão: cria a próxima.
CREATE TABLE label_schema (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    version      integer     NOT NULL,
    -- [{"id":0,"name":"classe_a","color":"#00ff00"}, …]
    classes      jsonb       NOT NULL,
    -- true quando a mudança só ACRESCENTOU classes no fim: anotações da versão
    -- anterior seguem válidas. Renomear/remover/reordenar => false, e as
    -- anotações afetadas viram needs_review.
    back_compatible boolean  NOT NULL DEFAULT true,
    change_note  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   uuid        REFERENCES app_user(id),
    UNIQUE (project_id, version)
);
CREATE TRIGGER label_schema_immutable BEFORE UPDATE ON label_schema
    FOR EACH ROW EXECUTE FUNCTION deny_update();


-- ===========================================================================
-- DADOS
-- ===========================================================================

CREATE TABLE data_source (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    kind       text        NOT NULL CHECK (kind IN ('gcs','upload','camera','import')),
    uri        text,                     -- gs://bucket/prefixo
    config     jsonb       NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid        REFERENCES app_user(id)
);


-- A imagem. Imutável e endereçada por conteúdo.
CREATE TABLE sample (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    source_id    uuid        REFERENCES data_source(id),
    sha256       bytea       NOT NULL,
    uri          text        NOT NULL,
    width        integer     NOT NULL,
    height       integer     NOT NULL,
    bytes        bigint,
    captured_at  timestamptz,
    -- turno, linha, câmera, lote: é o que permite filtrar o dataset depois.
    -- Sem metadata rica, "treinar só com o turno da noite" vira trabalho manual.
    metadata     jsonb       NOT NULL DEFAULT '{}',
    imported_at  timestamptz NOT NULL DEFAULT now(),
    imported_by  uuid        REFERENCES app_user(id),
    -- o mesmo arquivo importado duas vezes é um sample só
    UNIQUE (project_id, sha256)
);
CREATE INDEX sample_project_idx  ON sample (project_id, captured_at DESC);
CREATE INDEX sample_metadata_idx ON sample USING gin (metadata);
CREATE TRIGGER sample_immutable BEFORE UPDATE ON sample
    FOR EACH ROW EXECUTE FUNCTION deny_update();


-- Os boxes. Mutável, com histórico e autor.
CREATE TABLE annotation (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sample_id     uuid        NOT NULL REFERENCES sample(id) ON DELETE CASCADE,
    schema_id     uuid        NOT NULL REFERENCES label_schema(id),
    -- [{"cls":0,"cx":0.51,"cy":0.42,"w":0.08,"h":0.12}] — YOLO normalizado,
    -- centro e dimensões relativas à imagem.
    -- Array vazio = anotado, sem objetos (≠ não anotado, que é ausência de linha).
    boxes         jsonb       NOT NULL,
    origin        text        NOT NULL DEFAULT 'manual'
                  CHECK (origin IN ('manual','prelabel','import','correction')),
    -- de qual modelo veio, quando origin='prelabel': rastreia auto-rotulagem
    model_version_id uuid,
    status        text        NOT NULL DEFAULT 'submitted'
                  CHECK (status IN ('draft','submitted','reviewed','needs_review')),
    author_id     uuid        REFERENCES app_user(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    uuid        REFERENCES app_user(id),
    -- Um autor tem no máximo uma anotação viva por sample. Autores diferentes
    -- coexistem — é o que habilita medir concordância entre rotuladores.
    UNIQUE (sample_id, author_id)
);
CREATE INDEX annotation_sample_idx ON annotation (sample_id);
CREATE INDEX annotation_status_idx ON annotation (status);


CREATE TABLE annotation_review (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    annotation_id uuid        NOT NULL REFERENCES annotation(id) ON DELETE CASCADE,
    reviewer_id   uuid        NOT NULL REFERENCES app_user(id),
    verdict       text        NOT NULL CHECK (verdict IN ('approved','rejected','changes_requested')),
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);


-- Append-only: responde "quem mexeu nesse box e quando", que é a primeira
-- pergunta quando um modelo regride sem explicação.
CREATE TABLE annotation_history (
    id            bigserial PRIMARY KEY,
    annotation_id uuid        NOT NULL REFERENCES annotation(id) ON DELETE CASCADE,
    at            timestamptz NOT NULL DEFAULT now(),
    actor_id      uuid        REFERENCES app_user(id),
    action        text        NOT NULL CHECK (action IN ('create','update','delete')),
    boxes_before  jsonb,
    boxes_after   jsonb
);
CREATE INDEX annotation_history_idx ON annotation_history (annotation_id, at DESC);


-- ===========================================================================
-- DATASET: seleção viva (dataset) × snapshot imutável (dataset_version)
-- ===========================================================================

CREATE TABLE dataset (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug       text        NOT NULL,
    name       text        NOT NULL,
    kind       text        NOT NULL DEFAULT 'train'
               CHECK (kind IN ('train','eval','benchmark')),
    -- regra de seleção, não lista de arquivos:
    -- {"filter":{"metadata.linha":"A"},"require_annotation":true,
    --  "annotation_status":["reviewed"],"exclude_samples":["…"],
    --  "resolution":"latest_reviewed"}
    selection  jsonb       NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid        REFERENCES app_user(id),
    archived_at timestamptz,
    UNIQUE (project_id, slug)
);


CREATE TABLE dataset_version (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id   uuid        NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    version      integer     NOT NULL,
    schema_id    uuid        NOT NULL REFERENCES label_schema(id),
    manifest_uri text        NOT NULL,
    content_hash bytea       NOT NULL,
    -- {"method":"by_group","seed":42,"val_ratio":0.2,"group_by":"metadata.video"}
    --
    -- by_group é o default correto aqui: frames consecutivos do mesmo vídeo são
    -- quase idênticos, e split aleatório vaza train→val e infla o mAP.
    split_policy jsonb       NOT NULL,
    -- n_train, n_val, boxes por classe, samples sem box
    stats        jsonb       NOT NULL DEFAULT '{}',
    notes        text,
    frozen_at    timestamptz NOT NULL DEFAULT now(),
    created_by   uuid        REFERENCES app_user(id),
    UNIQUE (dataset_id, version),
    -- congelar duas vezes o mesmo conteúdo não cria versão nova
    UNIQUE (dataset_id, content_hash)
);
CREATE TRIGGER dataset_version_immutable BEFORE UPDATE ON dataset_version
    FOR EACH ROW EXECUTE FUNCTION deny_update();

-- Quais samples entraram — permite diff entre v6 e v7 sem baixar manifesto.
CREATE TABLE dataset_version_item (
    dataset_version_id uuid NOT NULL REFERENCES dataset_version(id) ON DELETE CASCADE,
    sample_id          uuid NOT NULL REFERENCES sample(id),
    annotation_id      uuid REFERENCES annotation(id),
    split              text NOT NULL CHECK (split IN ('train','val','test')),
    PRIMARY KEY (dataset_version_id, sample_id)
);
CREATE INDEX dsv_item_sample_idx ON dataset_version_item (sample_id);


-- ===========================================================================
-- EXECUÇÃO: devices e fila de jobs
-- ===========================================================================

CREATE TABLE device (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid,
    slug           text        NOT NULL UNIQUE,
    name           text        NOT NULL,
    device_class   text        NOT NULL,   -- 'orin-nano', 'workstation-4090'
    -- {"gpu":"Orin","vram_gb":8,"cuda":"12.2","jetpack":"6.0","trt":"8.6","dla":true}
    capabilities   jsonb       NOT NULL DEFAULT '{}',
    labels         text[]      NOT NULL DEFAULT '{}',
    token_sha256   bytea       UNIQUE,
    status         text        NOT NULL DEFAULT 'offline'
                   CHECK (status IN ('offline','idle','busy','draining','disabled')),
    last_heartbeat timestamptz,
    registered_at  timestamptz NOT NULL DEFAULT now(),
    registered_by  uuid        REFERENCES app_user(id)
);


CREATE TABLE job (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    type          text        NOT NULL
                  CHECK (type IN ('train','export','evaluate','prelabel','import')),
    payload       jsonb       NOT NULL DEFAULT '{}',
    -- capabilities mínimas; o claim só entrega a device compatível
    requires      jsonb       NOT NULL DEFAULT '{}',
    status        text        NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','claimed','running','succeeded',
                                    'failed','canceled')),
    priority      integer     NOT NULL DEFAULT 100,
    attempts      integer     NOT NULL DEFAULT 0,
    max_attempts  integer     NOT NULL DEFAULT 2,
    cancel_requested boolean  NOT NULL DEFAULT false,
    -- quem PEDIU (pessoa) × quem EXECUTOU (máquina): os dois principals
    created_by    uuid        REFERENCES app_user(id),
    device_id     uuid        REFERENCES device(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    claimed_at    timestamptz,
    started_at    timestamptz,
    finished_at   timestamptz,
    heartbeat_at  timestamptz,
    error         text
);
-- Suporta o claim atômico: UPDATE … WHERE status='queued' … RETURNING
CREATE INDEX job_queue_idx ON job (status, priority, created_at)
    WHERE status IN ('queued','claimed','running');
CREATE INDEX job_project_idx ON job (project_id, created_at DESC);


-- Log em append com seq: dá idempotência e retomada após queda de rede.
CREATE TABLE job_event (
    job_id  uuid        NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    seq     bigint      NOT NULL,
    at      timestamptz NOT NULL DEFAULT now(),
    level   text        NOT NULL DEFAULT 'info'
            CHECK (level IN ('debug','info','warn','error')),
    message text        NOT NULL,
    PRIMARY KEY (job_id, seq)
);


-- ===========================================================================
-- EXPERIMENTAÇÃO
-- ===========================================================================

CREATE TABLE experiment (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug        text        NOT NULL,
    name        text        NOT NULL,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid        REFERENCES app_user(id),
    UNIQUE (project_id, slug)
);


-- Nó central da linhagem: aponta p/ trás (dados, modelo base) e é apontado
-- pela frente (model_version).
CREATE TABLE training_run (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id         uuid        NOT NULL REFERENCES experiment(id),
    job_id                uuid        UNIQUE REFERENCES job(id),
    dataset_version_id    uuid        NOT NULL REFERENCES dataset_version(id),
    schema_id             uuid        NOT NULL REFERENCES label_schema(id),
    base_model_version_id uuid,       -- FK adiante (model_version vem depois)
    base_model_name       text,       -- p/ base externa: 'yolo11n.pt'
    -- epochs, imgsz, batch, patience, augmentation, device
    params                jsonb       NOT NULL DEFAULT '{}',
    -- melhores valores: mAP50, mAP50-95, precision, recall
    metrics               jsonb       NOT NULL DEFAULT '{}',
    best_epoch            integer,
    status                text        NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','finished','failed','stopped')),
    -- git sha do agente: sem isto a reprodutibilidade fica incompleta
    code_version          text,
    started_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz,
    duration_s            integer,
    created_by            uuid        REFERENCES app_user(id)
);
CREATE INDEX training_run_exp_idx ON training_run (experiment_id, started_at DESC);
CREATE INDEX training_run_dsv_idx ON training_run (dataset_version_id);


-- Série por época — alimentada pelo callback on_fit_epoch_end do treino.
CREATE TABLE run_metric (
    run_id uuid             NOT NULL REFERENCES training_run(id) ON DELETE CASCADE,
    epoch  integer          NOT NULL,
    key    text             NOT NULL,
    value  double precision NOT NULL,
    at     timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, epoch, key)
);


CREATE TABLE run_artifact (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     uuid        NOT NULL REFERENCES training_run(id) ON DELETE CASCADE,
    kind       text        NOT NULL,   -- 'weights','plot','confusion_matrix','samples'
    name       text        NOT NULL,
    uri        text        NOT NULL,
    sha256     bytea,
    bytes      bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);


-- ===========================================================================
-- MODELOS
-- ===========================================================================

CREATE TABLE model (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug        text        NOT NULL,
    name        text        NOT NULL,
    task_type   text        NOT NULL CHECK (task_type IN ('detect','segment','classify')),
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid        REFERENCES app_user(id),
    archived_at timestamptz,
    UNIQUE (project_id, slug)
);


CREATE TABLE model_version (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id    uuid        NOT NULL REFERENCES model(id) ON DELETE CASCADE,
    version     integer     NOT NULL,
    run_id      uuid        REFERENCES training_run(id),
    schema_id   uuid        NOT NULL REFERENCES label_schema(id),
    weights_uri text        NOT NULL,
    sha256      bytea       NOT NULL,
    bytes       bigint,
    -- copiadas do run: a versão precisa ser legível sozinha
    metrics     jsonb       NOT NULL DEFAULT '{}',
    status      text        NOT NULL DEFAULT 'ready'
                CHECK (status IN ('pending','ready','deprecated')),
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid        REFERENCES app_user(id),
    UNIQUE (model_id, version)
);
CREATE INDEX model_version_run_idx ON model_version (run_id);

ALTER TABLE training_run
    ADD CONSTRAINT training_run_base_model_fk
    FOREIGN KEY (base_model_version_id) REFERENCES model_version(id);
ALTER TABLE annotation
    ADD CONSTRAINT annotation_model_version_fk
    FOREIGN KEY (model_version_id) REFERENCES model_version(id);


-- Alias móvel no lugar de stage. O MLflow depreciou Staging/Production
-- justamente porque enum fixo não serve para todo mundo e a transição perde
-- histórico. Alias aponta p/ uma versão, mover é auditado, rollback é reaponte.
CREATE TABLE model_alias (
    model_id         uuid        NOT NULL REFERENCES model(id) ON DELETE CASCADE,
    alias            text        NOT NULL,   -- 'champion','challenger','baseline'
    model_version_id uuid        NOT NULL REFERENCES model_version(id),
    moved_at         timestamptz NOT NULL DEFAULT now(),
    moved_by         uuid        REFERENCES app_user(id),
    PRIMARY KEY (model_id, alias)
);


-- O nível que MLflow não tem: .engine do TensorRT só carrega na máquina que
-- compilou. Uma versão tem N builds, um por alvo.
CREATE TABLE model_build (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id uuid        NOT NULL REFERENCES model_version(id) ON DELETE CASCADE,
    format           text        NOT NULL CHECK (format IN ('pt','onnx','engine')),
    -- {"device_class":"orin-nano","jetpack":"6.0","trt":"8.6",
    --  "precision":"int8","imgsz":640,"dla":false}
    target           jsonb       NOT NULL DEFAULT '{}',
    uri              text        NOT NULL,
    sha256           bytea       NOT NULL,
    bytes            bigint,
    -- latência p50/p95 medida NO alvo
    bench            jsonb       NOT NULL DEFAULT '{}',
    built_by_job_id  uuid        REFERENCES job(id),
    built_on_device  uuid        REFERENCES device(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       uuid        REFERENCES app_user(id),
    UNIQUE (model_version_id, format, target)
);
CREATE TRIGGER model_build_immutable BEFORE UPDATE ON model_build
    FOR EACH ROW EXECUTE FUNCTION deny_update();


-- Comparabilidade: mesma versão de dataset, modelos diferentes. Métrica de
-- validação vinda do treino não serve p/ isso — cada run viu um split.
CREATE TABLE evaluation (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id   uuid        NOT NULL REFERENCES model_version(id) ON DELETE CASCADE,
    model_build_id     uuid        REFERENCES model_build(id),
    dataset_version_id uuid        NOT NULL REFERENCES dataset_version(id),
    job_id             uuid        REFERENCES job(id),
    metrics            jsonb       NOT NULL DEFAULT '{}',
    artifacts_uri      text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         uuid        REFERENCES app_user(id),
    UNIQUE (model_version_id, model_build_id, dataset_version_id)
);
CREATE TRIGGER evaluation_immutable BEFORE UPDATE ON evaluation
    FOR EACH ROW EXECUTE FUNCTION deny_update();


-- ===========================================================================
-- DEPLOYMENT — fecha o ciclo
-- ===========================================================================

CREATE TABLE deployment (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id      uuid        NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    model_build_id uuid        NOT NULL REFERENCES model_build(id),
    state          text        NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','active','failed','rolled_back','superseded')),
    requested_at   timestamptz NOT NULL DEFAULT now(),
    requested_by   uuid        REFERENCES app_user(id),
    activated_at   timestamptz,
    ended_at       timestamptz,
    error          text
);
-- No máximo um deployment ativo por device.
CREATE UNIQUE INDEX deployment_active_idx ON deployment (device_id)
    WHERE state = 'active';

COMMIT;
