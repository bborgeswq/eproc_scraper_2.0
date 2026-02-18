-- =============================================
-- Migração: Adicionar tabela prazos_abertos
-- Rodar no SQL Editor do Supabase (NÃO dropa nada)
-- =============================================

-- Tabela de prazos abertos (N prazos por processo)
CREATE TABLE IF NOT EXISTS prazos_abertos (
    id                  UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    processo_id         UUID NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
    evento_descricao    TEXT NOT NULL,
    data_envio          TIMESTAMPTZ,
    prazo_inicio        TIMESTAMPTZ,
    prazo_final         TIMESTAMPTZ NOT NULL,
    UNIQUE (processo_id, evento_descricao, prazo_final)
);

CREATE INDEX IF NOT EXISTS idx_prazos_abertos_processo ON prazos_abertos (processo_id);
CREATE INDEX IF NOT EXISTS idx_prazos_abertos_final ON prazos_abertos (prazo_final);

-- Recriar view (DROP necessário porque adicionamos nova coluna 'prazos')
DROP VIEW IF EXISTS v_processo_completo;
CREATE VIEW v_processo_completo AS
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

    -- Todos os prazos abertos do processo
    (SELECT COALESCE(json_agg(json_build_object(
        'evento_descricao', pa.evento_descricao,
        'data_envio', pa.data_envio,
        'prazo_inicio', pa.prazo_inicio,
        'prazo_final', pa.prazo_final
    ) ORDER BY pa.prazo_final ASC), '[]'::json)
    FROM prazos_abertos pa WHERE pa.processo_id = p.id) AS prazos,

    -- Eventos com documentos
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
    ) ORDER BY e.numero_evento DESC), '[]'::json)
    FROM eventos e WHERE e.processo_id = p.id) AS eventos

FROM processos p;
