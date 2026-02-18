-- =============================================
-- eProc Scraper 2.0 - Schema Supabase (v2)
-- 4 tabelas: processos, eventos, documentos, sync_log
-- Assuntos e partes embutidos como JSONB em processos
-- =============================================

-- LIMPEZA: Dropar tudo antes de recriar
DROP VIEW IF EXISTS v_processo_completo;
DROP TABLE IF EXISTS documentos CASCADE;
DROP TABLE IF EXISTS representantes CASCADE;
DROP TABLE IF EXISTS partes CASCADE;
DROP TABLE IF EXISTS assuntos CASCADE;
DROP TABLE IF EXISTS eventos CASCADE;
DROP TABLE IF EXISTS processos CASCADE;
DROP TABLE IF EXISTS sync_log CASCADE;

-- Tabela central: cada processo com prazo aberto
CREATE TABLE processos (
    id                      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    cnj                     TEXT NOT NULL UNIQUE,
    classe                  TEXT,
    competencia             TEXT,
    data_autuacao           DATE,
    situacao                TEXT,
    orgao_julgador          TEXT,
    juiz                    TEXT,
    juizo                   TEXT,
    lado_advogado           TEXT,
    processos_relacionados  TEXT[] DEFAULT '{}',

    -- JSONB (antes eram tabelas separadas)
    assuntos                JSONB DEFAULT '[]',
    partes                  JSONB DEFAULT '[]',

    -- Dados do prazo ativo (da tabela "Prazos Abertos")
    prazo_evento_descricao  TEXT,
    prazo_data_envio        TIMESTAMPTZ,
    prazo_inicio            TIMESTAMPTZ,
    prazo_final             TIMESTAMPTZ,

    -- Metadata de sync
    first_seen_at           TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_processos_cnj ON processos (cnj);
CREATE INDEX idx_processos_prazo_final ON processos (prazo_final);

-- Eventos/movimentações do processo
CREATE TABLE eventos (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    processo_id         UUID NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
    numero_evento       INTEGER NOT NULL,
    data_hora           TIMESTAMPTZ NOT NULL,
    descricao           TEXT NOT NULL,
    usuario             TEXT,
    tem_prazo           BOOLEAN DEFAULT FALSE,
    prazo_dias          INTEGER,
    prazo_status        TEXT,
    prazo_data_inicial  TIMESTAMPTZ,
    prazo_data_final    TIMESTAMPTZ,
    evento_referencia   INTEGER,
    urgente             BOOLEAN DEFAULT FALSE,
    UNIQUE (processo_id, numero_evento)
);

CREATE INDEX idx_eventos_processo ON eventos (processo_id);
CREATE INDEX idx_eventos_numero ON eventos (processo_id, numero_evento);

-- Documentos (PDFs linkados ao Supabase Storage)
CREATE TABLE documentos (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    processo_id     UUID NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
    evento_id       UUID NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
    numero_evento   INTEGER NOT NULL,
    nome_original   TEXT NOT NULL,
    tipo            TEXT,
    url_eproc       TEXT NOT NULL,
    storage_path    TEXT,
    storage_url     TEXT,
    tamanho_bytes   BIGINT,
    hash_sha256     TEXT,
    UNIQUE (processo_id, numero_evento, url_eproc)
);

CREATE INDEX idx_documentos_processo ON documentos (processo_id);
CREATE INDEX idx_documentos_evento ON documentos (evento_id);

-- Log de cada execução do sync
CREATE TABLE sync_log (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              TEXT DEFAULT 'running',
    processos_added     INTEGER DEFAULT 0,
    processos_removed   INTEGER DEFAULT 0,
    processos_updated   INTEGER DEFAULT 0,
    documentos_uploaded INTEGER DEFAULT 0,
    error_message       TEXT
);

-- Trigger para atualizar updated_at automaticamente
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_processos_updated
    BEFORE UPDATE ON processos
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- View completa para N8N: 1 query = tudo do processo
CREATE OR REPLACE VIEW v_processo_completo AS
SELECT
    p.id AS processo_id,
    p.cnj,
    p.classe,
    p.competencia,
    p.data_autuacao,
    p.situacao,
    p.orgao_julgador,
    p.juiz,
    p.juizo,
    p.lado_advogado,
    p.assuntos,
    p.partes,
    p.prazo_evento_descricao,
    p.prazo_inicio,
    p.prazo_final,
    p.last_synced_at,

    (SELECT COALESCE(json_agg(json_build_object(
        'numero', e.numero_evento,
        'data_hora', e.data_hora,
        'descricao', e.descricao,
        'usuario', e.usuario,
        'tem_prazo', e.tem_prazo,
        'prazo_status', e.prazo_status,
        'prazo_data_final', e.prazo_data_final,
        'urgente', e.urgente,
        'documentos', (
            SELECT COALESCE(json_agg(json_build_object(
                'nome', d.nome_original,
                'tipo', d.tipo,
                'storage_url', d.storage_url
            )), '[]'::json) FROM documentos d WHERE d.evento_id = e.id
        )
    ) ORDER BY e.numero_evento DESC), '[]'::json) FROM eventos e WHERE e.processo_id = p.id) AS eventos

FROM processos p;
