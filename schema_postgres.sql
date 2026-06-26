-- ===========================================================
-- INITIALISATION DES EXTENSIONS
-- ===========================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- Pour les IDs uniques
CREATE EXTENSION IF NOT EXISTS "ltree";       -- Pour la hiérarchie performante
CREATE EXTENSION IF NOT EXISTS "vector";      -- Pour la recherche sémantique (RAG)
CREATE EXTENSION IF NOT EXISTS "btree_gist";  -- Pour les contraintes de temps (exclude)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- Recherche floue (trigram) : variantes morphologiques & fautes de frappe
CREATE EXTENSION IF NOT EXISTS "unaccent";    -- Normalisation des accents pour la recherche floue

-- Wrapper IMMUTABLE autour de unaccent() (forme à dictionnaire explicite) :
-- indispensable pour l'utiliser dans les expressions de recherche floue. Voir
-- la recherche Bibliothèque (strict_word_similarity sur f_unaccent(contenu)).
CREATE OR REPLACE FUNCTION f_unaccent(text)
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $func$ SELECT public.unaccent('public.unaccent', $1) $func$;

-- NETTOYAGE (Attention: supprime les données existantes)
DROP TABLE IF EXISTS audits CASCADE;
DROP TABLE IF EXISTS personal_access_tokens CASCADE;
DROP TABLE IF EXISTS failed_jobs CASCADE;
DROP TABLE IF EXISTS job_batches CASCADE;
DROP TABLE IF EXISTS jobs CASCADE;
DROP TABLE IF EXISTS cache_locks CASCADE;
DROP TABLE IF EXISTS cache CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS password_reset_tokens CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS migrations CASCADE;

DROP TABLE IF EXISTS taggables CASCADE;
DROP TABLE IF EXISTS article_tag CASCADE; -- Old table
DROP TABLE IF EXISTS tags CASCADE;
DROP TABLE IF EXISTS curation_flags CASCADE;
DROP TABLE IF EXISTS document_relations CASCADE;
DROP TABLE IF EXISTS text_extractions CASCADE;
DROP TABLE IF EXISTS extraction_runs CASCADE;
DROP TABLE IF EXISTS article_versions CASCADE;
DROP TABLE IF EXISTS articles CASCADE;
DROP TABLE IF EXISTS structure_nodes CASCADE;
DROP TABLE IF EXISTS media_files CASCADE;
DROP TABLE IF EXISTS legal_documents CASCADE;
DROP TABLE IF EXISTS institutions CASCADE;
DROP TABLE IF EXISTS document_types CASCADE;
DROP TABLE IF EXISTS agent_conversation_messages CASCADE;
DROP TABLE IF EXISTS agent_conversations CASCADE;
DROP TABLE IF EXISTS devices CASCADE;
DROP TABLE IF EXISTS mobile_profiles CASCADE;
DROP TABLE IF EXISTS notifications CASCADE;
DROP TABLE IF EXISTS official_journals CASCADE;
DROP TABLE IF EXISTS model_has_permissions CASCADE;
DROP TABLE IF EXISTS model_has_roles CASCADE;
DROP TABLE IF EXISTS role_has_permissions CASCADE;
DROP TABLE IF EXISTS permissions CASCADE;
DROP TABLE IF EXISTS roles CASCADE;

-- ===========================================================
-- 0. TABLES SYSTÈME (LARAVEL DEFAULT)
-- ===========================================================

CREATE TABLE IF NOT EXISTS migrations (
    id SERIAL PRIMARY KEY,
    migration VARCHAR(255) NOT NULL,
    batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    email_verified_at TIMESTAMP(0) WITHOUT TIME ZONE,
    password VARCHAR(255) NOT NULL,
    two_factor_secret TEXT,
    two_factor_recovery_codes TEXT,
    two_factor_confirmed_at TIMESTAMP(0) WITHOUT TIME ZONE,
    remember_token VARCHAR(100),
    status VARCHAR(255),
    last_seen_at TIMESTAMP(0) WITHOUT TIME ZONE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE,
    deleted_at TIMESTAMP(0) WITHOUT TIME ZONE
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    email VARCHAR(255) PRIMARY KEY,
    token VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE
);

CREATE TABLE IF NOT EXISTS sessions (
    id VARCHAR(255) PRIMARY KEY,
    user_id UUID,
    ip_address VARCHAR(45),
    user_agent TEXT,
    payload TEXT NOT NULL,
    last_activity INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity);

CREATE TABLE IF NOT EXISTS cache (
    key VARCHAR(255) PRIMARY KEY,
    value TEXT NOT NULL,
    expiration INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_locks (
    key VARCHAR(255) PRIMARY KEY,
    owner VARCHAR(255) NOT NULL,
    expiration INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    queue VARCHAR(255) NOT NULL,
    payload TEXT NOT NULL,
    attempts SMALLINT NOT NULL,
    reserved_at INTEGER,
    available_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(queue);

CREATE TABLE IF NOT EXISTS job_batches (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    total_jobs INTEGER NOT NULL,
    pending_jobs INTEGER NOT NULL,
    failed_jobs INTEGER NOT NULL,
    failed_job_ids TEXT NOT NULL,
    options TEXT,
    cancelled_at INTEGER,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS failed_jobs (
    id BIGSERIAL PRIMARY KEY,
    uuid VARCHAR(255) NOT NULL UNIQUE,
    connection TEXT NOT NULL,
    queue TEXT NOT NULL,
    payload TEXT NOT NULL,
    exception TEXT NOT NULL,
    failed_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id BIGSERIAL PRIMARY KEY,
    tokenable_type VARCHAR(255) NOT NULL,
    tokenable_id UUID NOT NULL,
    name TEXT NOT NULL,
    token VARCHAR(64) NOT NULL UNIQUE,
    abilities TEXT,
    last_used_at TIMESTAMP(0) WITHOUT TIME ZONE,
    expires_at TIMESTAMP(0) WITHOUT TIME ZONE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_pat_tokenable ON personal_access_tokens(tokenable_type, tokenable_id);
CREATE INDEX IF NOT EXISTS idx_pat_expires_at ON personal_access_tokens(expires_at);

CREATE TABLE IF NOT EXISTS audits (
    id BIGSERIAL PRIMARY KEY,
    user_type VARCHAR(255),
    user_id UUID,
    event VARCHAR(255) NOT NULL,
    auditable_type VARCHAR(255) NOT NULL,
    auditable_id UUID NOT NULL,
    old_values TEXT,
    new_values TEXT,
    url TEXT,
    ip_address INET,
    user_agent VARCHAR(1023),
    tags VARCHAR(255),
    created_at TIMESTAMP(0) WITHOUT TIME ZONE,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_audits_user ON audits(user_id, user_type);
CREATE INDEX IF NOT EXISTS idx_audits_auditable ON audits(auditable_type, auditable_id);

-- ===========================================================
-- 1. TABLE : METADONNÉES DES DOCUMENTS (Le contenant)
-- ===========================================================
CREATE TABLE document_types (
    code VARCHAR(10) PRIMARY KEY,
    nom VARCHAR(50) NOT NULL,
    niveau_hierarchique INT DEFAULT 0,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE institutions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nom VARCHAR(200) NOT NULL,
    sigle VARCHAR(50),
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE official_journals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title VARCHAR(255) NOT NULL,
    publication_date DATE NOT NULL,
    file_path VARCHAR(255) NOT NULL,
    transcription_status VARCHAR(255),
    is_published BOOLEAN DEFAULT FALSE,
    number VARCHAR(255),
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP(0) WITHOUT TIME ZONE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_official_journals_pubdate_number
ON official_journals(publication_date, number)
WHERE number IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE legal_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    type_code VARCHAR(10) REFERENCES document_types(code),
    institution_id UUID REFERENCES institutions(id),
    official_journal_id UUID REFERENCES official_journals(id),

    document_key TEXT,
    document_role VARCHAR(20) NOT NULL CHECK (document_role IN ('STOCK', 'FLUX')) DEFAULT 'FLUX',
    consolidation_as_of DATE,
    stock_code VARCHAR(100),

    titre_officiel TEXT NOT NULL,
    reference_nor VARCHAR(50),

    date_signature DATE,
    date_publication DATE,
    date_entree_vigueur DATE,

    statut VARCHAR(20) CHECK (statut IN ('vigueur', 'abroge', 'projet')) DEFAULT 'vigueur',
    legal_scope VARCHAR(20) NOT NULL CHECK (legal_scope IN ('national', 'ohada', 'communautaire')) DEFAULT 'national',
    curation_status VARCHAR(255) DEFAULT 'draft',
    extraction_status VARCHAR(20),

    metadata JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP(0) WITHOUT TIME ZONE
);

CREATE INDEX idx_legal_docs_metadata ON legal_documents USING GIN (metadata);
CREATE INDEX IF NOT EXISTS legal_documents_legal_scope_index ON legal_documents(legal_scope);

CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_documents_document_key
ON legal_documents(document_key)
WHERE document_key IS NOT NULL AND deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_documents_stock_code
ON legal_documents(stock_code)
WHERE stock_code IS NOT NULL AND deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_documents_reference_nor
ON legal_documents(reference_nor)
WHERE reference_nor IS NOT NULL AND deleted_at IS NULL;

ALTER TABLE legal_documents
    ADD CONSTRAINT chk_legal_documents_role_logic
    CHECK (
        (document_role = 'STOCK' AND consolidation_as_of IS NOT NULL AND official_journal_id IS NULL AND stock_code IS NOT NULL)
        OR
        (document_role = 'FLUX' AND consolidation_as_of IS NULL)
    );

CREATE TABLE media_files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES legal_documents(id) ON DELETE CASCADE,
    file_path VARCHAR(512) NOT NULL,
    storage_provider VARCHAR(20) NOT NULL DEFAULT 'MINIO',
    bucket_name VARCHAR(100) NOT NULL DEFAULT 'mibeko-documents',
    object_key VARCHAR(512) NOT NULL,
    original_filename VARCHAR(255),
    mime_type VARCHAR(100),
    file_category VARCHAR(50) NOT NULL CHECK (file_category IN ('SOURCE_PDF', 'EXTRACTION_MARKDOWN', 'EXTRACTION_JSON')),
    file_size BIGINT,
    checksum_sha256 VARCHAR(64),
    description VARCHAR(255),
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_files_document_object_key
ON media_files(document_id, object_key);

-- ===========================================================
-- 1.b TABLES : EXTRACTION (Le flux brut depuis les PDF)
-- ===========================================================
CREATE TABLE extraction_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES legal_documents(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL CHECK (source IN ('MINERU', 'MANUAL_UPLOAD', 'PARSING')) DEFAULT 'MINERU',
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'partial')) DEFAULT 'queued',
    started_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP(0) WITHOUT TIME ZONE,
    source_media_file_id UUID REFERENCES media_files(id) ON DELETE SET NULL,
    markdown_media_file_id UUID REFERENCES media_files(id) ON DELETE SET NULL,
    json_media_file_id UUID REFERENCES media_files(id) ON DELETE SET NULL,
    meta JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_meta ON extraction_runs USING GIN (meta);

-- ===========================================================
-- 2. TABLE : SQUELETTE STRUCTUREL (Materialized Path)
-- ===========================================================
CREATE TABLE structure_nodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES legal_documents(id) ON DELETE CASCADE,
    type_unite VARCHAR(50) NOT NULL,
    numero VARCHAR(50),
    titre TEXT,
    tree_path ltree NOT NULL,
    validation_status VARCHAR(255) DEFAULT 'pending',
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_structure_path ON structure_nodes USING GIST (tree_path);
CREATE INDEX idx_structure_doc ON structure_nodes(document_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_structure_nodes_document_path
ON structure_nodes(document_id, tree_path);

-- ===========================================================
-- 3. TABLE : ARTICLES (L'identité stable)
-- ===========================================================
CREATE TABLE articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES legal_documents(id) ON DELETE CASCADE,
    parent_node_id UUID REFERENCES structure_nodes(id),
    numero_article VARCHAR(50) NOT NULL,
    ordre_affichage INT DEFAULT 0,
    validation_status VARCHAR(20) DEFAULT 'pending',
    deleted_at TIMESTAMP(0) WITHOUT TIME ZONE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_articles_updated_at ON articles(updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_document_numero
ON articles(document_id, numero_article)
WHERE deleted_at IS NULL;

-- ===========================================================
-- 4. TABLE : VERSIONS D'ARTICLES (Le contenu & RAG)
-- ===========================================================
CREATE TABLE article_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    article_id UUID NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    validity_period DATERANGE NOT NULL DEFAULT daterange(CURRENT_DATE, 'infinity'::date, '[)'),
    contenu_texte TEXT NOT NULL,
    embedding_context TEXT,
    embedding VECTOR(1024),
    search_tsv TSVECTOR,
    modifie_par_document_id UUID REFERENCES legal_documents(id) ON DELETE SET NULL,
    source_run_id UUID REFERENCES extraction_runs(id) ON DELETE SET NULL,
    source_media_file_id UUID REFERENCES media_files(id) ON DELETE SET NULL,
    source_locator JSONB DEFAULT '{}'::jsonb,
    validation_status VARCHAR(255) DEFAULT 'pending',
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_article_versions_validity_not_empty CHECK (NOT isempty(validity_period)),
    EXCLUDE USING GIST (
        article_id WITH =,
        validity_period WITH &&
    )
);

COMMENT ON COLUMN article_versions.validity_period IS
'Période de validité (daterange) : si inconnue au moment de l''ingestion PDF/OCR, l''application peut laisser la valeur par défaut daterange(CURRENT_DATE, ''infinity'', ''[)'') afin que Postgres accepte l''insert. La période doit ensuite être affinée/validée (juriste/IA) en UPDATE sur la version concernée pour éviter des conflits avec la contrainte EXCLUDE (chevauchements).';

CREATE INDEX idx_versions_search ON article_versions USING GIN(search_tsv);

-- Index GIN trigram (contenu désaccentué) : accélère le filet de recherche
-- floue de la Bibliothèque (opérateur %>> / strict_word_similarity), qui rattrape
-- les variantes morphologiques (dot ↔ dotal) et les fautes de frappe.
CREATE INDEX idx_versions_content_trgm ON article_versions
USING gin (f_unaccent(contenu_texte) gin_trgm_ops);

CREATE INDEX idx_versions_embedding ON article_versions
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Seuil par défaut du filet flou : l'opérateur %>> l'utilise. 0.35 garde
-- « dotal » (0.375) et écarte « dotation » (0.27) pour une recherche « dote ».
DO $$ BEGIN
    EXECUTE format('ALTER DATABASE %I SET pg_trgm.strict_word_similarity_threshold = 0.35', current_database());
END $$;

-- ===========================================================
-- 5. TABLE : LIENS ET CITATIONS (Le Graph Juridique)
-- ===========================================================
CREATE TABLE document_relations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_doc_id UUID REFERENCES legal_documents(id) ON DELETE CASCADE,
    target_doc_id UUID REFERENCES legal_documents(id) ON DELETE CASCADE,
    source_article_id UUID REFERENCES articles(id) ON DELETE CASCADE,
    target_article_id UUID REFERENCES articles(id) ON DELETE CASCADE,
    relation_type VARCHAR(50),
    commentaire TEXT,
    effective_date DATE,
    confidence NUMERIC(5,4) CHECK (confidence >= 0 AND confidence <= 1),
    meta JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE document_relations
    ADD CONSTRAINT document_relations_relation_type_check
    CHECK (relation_type IN ('CREE', 'MODIFIE', 'ABROGE', 'CITE', 'COMPLETE', 'RENUMEROTE'));

ALTER TABLE document_relations
    ADD CONSTRAINT chk_document_relations_endpoints
    CHECK (
        (source_doc_id IS NOT NULL OR source_article_id IS NOT NULL)
        AND
        (target_doc_id IS NOT NULL OR target_article_id IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS idx_document_relations_meta ON document_relations USING GIN (meta);

-- ===========================================================
-- 6. TABLE : DRAPEAUX DE CURATION (Signalement d'erreurs)
-- ===========================================================
CREATE TABLE curation_flags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID REFERENCES legal_documents(id) ON DELETE CASCADE,
    article_id UUID REFERENCES articles(id) ON DELETE CASCADE,
    -- Division ciblée (anomalies structurelles : division vide/sans titre).
    node_id UUID REFERENCES structure_nodes(id) ON DELETE CASCADE,
    -- Origine : heuristic (numérotation) | structural | llm | human. Les flags
    -- 'human' ne sont jamais purgés par les détecteurs automatiques.
    source VARCHAR(20) DEFAULT 'human',
    type_probleme VARCHAR(50),
    -- Seul 'blocking' empêche la publication ; 'warning'/'info' informent.
    severity VARCHAR(20) DEFAULT 'blocking',
    description TEXT,
    -- Proposition de correction (LLM) — jamais appliquée automatiquement.
    suggestion JSONB,
    -- Ancrage : { page, char_start, char_end, tree_path } pour le surlignage.
    anchor JSONB,
    confidence NUMERIC(5,4),
    -- Rattache un lot de détection (idempotence des re-runs).
    run_id UUID,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMP(0) WITHOUT TIME ZONE,
    resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_curation_flags_doc_resolved_source
    ON curation_flags(document_id, resolved, source);

-- ===========================================================
-- 7. TAGS POLYMORPHES (Pour Sandrine / Simplification)
-- ===========================================================
CREATE TABLE tags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL UNIQUE,
    slug VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE taggables (
    tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    taggable_id UUID NOT NULL,
    taggable_type VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tag_id, taggable_id, taggable_type)
);

CREATE INDEX idx_taggables_item ON taggables(taggable_id, taggable_type);

-- ===========================================================
-- 8. NOUVELLES TABLES (AGENTS, NOTIFICATIONS, PROFILS, ROLES)
-- ===========================================================

-- Agent Conversations
CREATE TABLE agent_conversations (
    id VARCHAR(36) PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255),
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_conversation_messages (
    id VARCHAR(36) PRIMARY KEY,
    conversation_id VARCHAR(36) REFERENCES agent_conversations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    agent VARCHAR(255),
    role VARCHAR(25),
    content TEXT,
    attachments TEXT,
    tool_calls TEXT,
    tool_results TEXT,
    usage TEXT,
    meta TEXT,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Devices
CREATE TABLE devices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id VARCHAR(255) NOT NULL,
    push_token VARCHAR(255),
    platform VARCHAR(255),
    status VARCHAR(255),
    last_registered_at TIMESTAMP(0) WITHOUT TIME ZONE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Mobile Profiles
CREATE TABLE mobile_profiles (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    phone VARCHAR(255),
    dob DATE,
    gender VARCHAR(255),
    profession VARCHAR(255),
    company VARCHAR(255),
    legal_interests TEXT,
    app_preferences JSON,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notifications
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    type VARCHAR(255),
    data JSON,
    read_at TIMESTAMP(0) WITHOUT TIME ZONE,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Permissions & Roles (Spatie)
CREATE TABLE permissions (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    guard_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE roles (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    guard_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP(0) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE model_has_permissions (
    permission_id BIGINT REFERENCES permissions(id) ON DELETE CASCADE,
    model_type VARCHAR(255) NOT NULL,
    model_id UUID NOT NULL,
    PRIMARY KEY (permission_id, model_id, model_type)
);

CREATE TABLE model_has_roles (
    role_id BIGINT REFERENCES roles(id) ON DELETE CASCADE,
    model_type VARCHAR(255) NOT NULL,
    model_id UUID NOT NULL,
    PRIMARY KEY (role_id, model_id, model_type)
);

CREATE TABLE role_has_permissions (
    permission_id BIGINT REFERENCES permissions(id) ON DELETE CASCADE,
    role_id BIGINT REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (permission_id, role_id)
);


-- ===========================================================
-- 9. TRIGGERS : REFRESH SEARCH TSV (Full Text Search)
-- ===========================================================
CREATE OR REPLACE FUNCTION fn_refresh_article_version_tsv()
RETURNS TRIGGER AS $$
DECLARE
    article_id_to_update UUID;
    tags_text TEXT;
BEGIN
    IF (TG_RELNAME = 'article_versions') THEN
        article_id_to_update := NEW.article_id;
    ELSE
        IF (TG_OP = 'DELETE') THEN
            IF (OLD.taggable_type != 'App\Models\Article') THEN RETURN NULL; END IF;
            article_id_to_update := OLD.taggable_id;
        ELSE
            IF (NEW.taggable_type != 'App\Models\Article') THEN RETURN NEW; END IF;
            article_id_to_update := NEW.taggable_id;
        END IF;
    END IF;

    SELECT COALESCE(string_agg(name, ' '), '') INTO tags_text
    FROM tags
    JOIN taggables ON tags.id = taggables.tag_id
    WHERE taggables.taggable_id = article_id_to_update
      AND taggables.taggable_type = 'App\Models\Article';

    IF (TG_RELNAME = 'article_versions') THEN
        NEW.search_tsv := (
            setweight(to_tsvector('french', COALESCE(NEW.contenu_texte, '')), 'A') ||
            setweight(to_tsvector('french', tags_text), 'B')
        );
        RETURN NEW;
    ELSE
        UPDATE article_versions
        SET search_tsv = (
            setweight(to_tsvector('french', COALESCE(contenu_texte, '')), 'A') ||
            setweight(to_tsvector('french', tags_text), 'B')
        )
        WHERE article_id = article_id_to_update;
        RETURN NULL;
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION fn_refresh_article_version_tsv() IS
'Met à jour search_tsv sur article_versions lors des INSERT/UPDATE de contenu_texte et lors des changements de tags pour les entités Article.';

-- ===========================================================
-- 10. TRIGGERS : COHÉRENCE STRUCTURE (parent_node_id ↔ document_id)
-- ===========================================================
CREATE OR REPLACE FUNCTION fn_enforce_article_parent_same_document()
RETURNS TRIGGER AS $$
DECLARE
    parent_document_id UUID;
BEGIN
    IF NEW.parent_node_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT document_id INTO parent_document_id
    FROM structure_nodes
    WHERE id = NEW.parent_node_id;

    IF parent_document_id IS NULL THEN
        RAISE EXCEPTION 'parent_node_id % inexistant', NEW.parent_node_id;
    END IF;

    IF parent_document_id <> NEW.document_id THEN
        RAISE EXCEPTION 'parent_node_id % appartient au document %, mais l''article appartient au document %', NEW.parent_node_id, parent_document_id, NEW.document_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION fn_enforce_article_parent_same_document() IS
'Empêche de référencer un structure_nodes d''un autre document via articles.parent_node_id (source d''incohérences lors de l''import PDF).';

CREATE TRIGGER trg_articles_parent_node_same_document
BEFORE INSERT OR UPDATE OF parent_node_id, document_id ON articles
FOR EACH ROW EXECUTE FUNCTION fn_enforce_article_parent_same_document();

CREATE TRIGGER trg_refresh_tsv_on_version
BEFORE INSERT OR UPDATE OF contenu_texte ON article_versions
FOR EACH ROW EXECUTE FUNCTION fn_refresh_article_version_tsv();

CREATE TRIGGER trg_refresh_tsv_on_tags
AFTER INSERT OR DELETE OR UPDATE ON taggables
FOR EACH ROW EXECUTE FUNCTION fn_refresh_article_version_tsv();
