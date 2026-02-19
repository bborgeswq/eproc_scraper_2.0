-- =============================================
-- eProc Scraper 2.0 - Schema Supabase (v3)
-- CNJ como PK, sem UUIDs intermediários
-- 5 tabelas: processos, prazos_abertos, eventos, documentos, sync_log
-- =============================================

-- LIMPEZA: Dropar tudo antes de recriar
DROP VIEW IF EXISTS v_processo_completo;
DROP TABLE IF EXISTS documentos CASCADE;
DROP TABLE IF EXISTS prazos_abertos CASCADE;
DROP TABLE IF EXISTS eventos CASCADE;
DROP TABLE IF EXISTS processos CASCADE;
DROP TABLE IF EXISTS sync_log CASCADE;

-- Tabela central: cada processo com prazo aberto
CREATE TABLE processos (
    cnj                     TEXT PRIMARY KEY,
    classe                  TEXT,
    competencia             TEXT,
    data_autuacao           DATE,
    situacao                TEXT,
    orgao_julgador          TEXT,
    juiz                    TEXT,
    juizo                   TEXT,
    lado_advogado           TEXT,
    processos_relacionados  TEXT[] DEFAULT '{}',
    assuntos                JSONB DEFAULT '[]',
    partes                  JSONB DEFAULT '[]',
    first_seen_at           TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Prazos abertos (N por processo, extraídos da lista do eProc)
CREATE TABLE prazos_abertos (
    cnj                 TEXT NOT NULL REFERENCES processos(cnj) ON DELETE CASCADE,
    evento_descricao    TEXT NOT NULL,
    data_envio          TIMESTAMPTZ,
    prazo_inicio        TIMESTAMPTZ,
    prazo_final         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cnj, evento_descricao, prazo_final)
);

CREATE INDEX idx_prazos_abertos_final ON prazos_abertos (prazo_final);

-- Eventos/movimentações do processo
CREATE TABLE eventos (
    cnj                 TEXT NOT NULL REFERENCES processos(cnj) ON DELETE CASCADE,
    numero_evento       INTEGER NOT NULL,
    data_hora           TIMESTAMPTZ NOT NULL,
    descricao           TEXT NOT NULL,
    usuario             TEXT,
    prazo_aberto        BOOLEAN DEFAULT FALSE,
    prazo_dias          INTEGER,
    prazo_status        TEXT,
    prazo_data_inicial  TIMESTAMPTZ,
    prazo_data_final    TIMESTAMPTZ,
    evento_referencia   INTEGER,
    urgente             BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (cnj, numero_evento)
);

-- Documentos (arquivos anexados aos eventos)
CREATE TABLE documentos (
    cnj             TEXT NOT NULL,
    numero_evento   INTEGER NOT NULL,
    nome_original   TEXT NOT NULL,
    tipo            TEXT,
    url_eproc       TEXT NOT NULL,
    storage_path    TEXT,
    storage_url     TEXT,
    tamanho_bytes   BIGINT,
    hash_sha256     TEXT,
    PRIMARY KEY (cnj, numero_evento, url_eproc),
    FOREIGN KEY (cnj, numero_evento) REFERENCES eventos(cnj, numero_evento) ON DELETE CASCADE
);

-- Log de cada execução do sync
CREATE TABLE sync_log (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              TEXT DEFAULT 'running',
    processos_total     INTEGER DEFAULT 0,
    processos_novos     INTEGER DEFAULT 0,
    processos_removidos INTEGER DEFAULT 0,
    documentos_baixados INTEGER DEFAULT 0,
    erros               INTEGER DEFAULT 0,
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

-- View completa: 1 query = tudo do processo
CREATE VIEW v_processo_completo AS
SELECT
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
    p.last_synced_at,

    (SELECT COALESCE(json_agg(json_build_object(
        'evento_descricao', pa.evento_descricao,
        'data_envio', pa.data_envio,
        'prazo_inicio', pa.prazo_inicio,
        'prazo_final', pa.prazo_final
    ) ORDER BY pa.prazo_final ASC), '[]'::json)
    FROM prazos_abertos pa WHERE pa.cnj = p.cnj) AS prazos,

    (SELECT COALESCE(json_agg(json_build_object(
        'numero', e.numero_evento,
        'data_hora', e.data_hora,
        'descricao', e.descricao,
        'usuario', e.usuario,
        'prazo_aberto', e.prazo_aberto,
        'prazo_status', e.prazo_status,
        'prazo_data_final', e.prazo_data_final,
        'urgente', e.urgente,
        'evento_referencia', e.evento_referencia,
        'documentos', (
            SELECT COALESCE(json_agg(json_build_object(
                'nome', d.nome_original,
                'tipo', d.tipo,
                'storage_url', d.storage_url
            )), '[]'::json) FROM documentos d
            WHERE d.cnj = e.cnj AND d.numero_evento = e.numero_evento
        )
    ) ORDER BY e.numero_evento DESC), '[]'::json)
    FROM eventos e WHERE e.cnj = p.cnj) AS eventos

FROM processos p;
