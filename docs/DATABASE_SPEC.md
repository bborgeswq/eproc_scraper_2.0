# eProc Scraper 2.0 — Database Specification

## Visao geral

Sistema de extracao automatizada do portal judicial **eProc 1G TJRS** (Tribunal de Justica do Rio Grande do Sul). O scraper faz login via Keycloak SSO + TOTP 2FA, extrai todos os processos com prazos abertos do advogado logado, e sincroniza com um backend **Supabase** (PostgreSQL + Storage).

### Stack
- **Scraper:** Python 3.12 + Playwright (browser automation)
- **Database:** Supabase (PostgreSQL 15)
- **Storage:** Supabase Storage (bucket `process-documents`)
- **Deploy:** Docker container no Easypanel (loop continuo, 1 processo por ciclo)
- **Integracao:** View `v_processo_completo` pronta para consumo por N8N e dashboards

### Ciclo de sync
1. Login no eProc via Keycloak SSO + TOTP
2. Navega para "Prazos Abertos" e extrai lista de processos (CNJ + dados do prazo)
3. Compara com DB: adiciona novos, atualiza existentes, remove os que sairam
4. Para cada processo novo: scrape completo (header, partes, assuntos, eventos, documentos)
5. Para cada processo existente: atualiza campos de prazo + busca eventos novos
6. Documentos sao baixados do eProc e uploadados para Supabase Storage
7. Repete ate completar todos, depois aguarda 24h

---

## Tabelas

### 1. `processos` — Tabela central

Cada linha = um processo judicial com prazo em aberto para o advogado logado.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno, gerado automaticamente |
| `cnj` | TEXT (UNIQUE) | Numero CNJ do processo. Formato: `NNNNNNN-NN.NNNN.N.NN.NNNN` (ex: `5001531-15.2025.8.21.0094`) |
| `classe` | TEXT | Classe processual (ex: "Inventario", "Cumprimento de Sentenca") |
| `competencia` | TEXT | Competencia do juizo (ex: "Civel", "Familia") |
| `data_autuacao` | DATE | Data de autuacao do processo (formato ISO: `YYYY-MM-DD`) |
| `situacao` | TEXT | Situacao atual (ex: "Em andamento", "Suspenso") |
| `orgao_julgador` | TEXT | Orgao julgador (ex: "1a Vara Civel de Tramandai") |
| `juiz` | TEXT | Nome do juiz(a) responsavel |
| `juizo` | TEXT | Juizo (extraido da tabela de prazos, pode ser diferente do orgao_julgador) |
| `lado_advogado` | TEXT | Lado do advogado logado no processo: `"AUTOR"`, `"REU"`, `"REQUERENTE"`, `"EXEQUENTE"`, etc. Vazio se nao identificado |
| `processos_relacionados` | TEXT[] | Array de CNJs de processos relacionados |
| `assuntos` | JSONB | Array de assuntos do processo (ver estrutura abaixo) |
| `partes` | JSONB | Array de partes e representantes (ver estrutura abaixo) |
| `prazo_evento_descricao` | TEXT | Descricao do evento que gerou o prazo ativo (ex: "Intimacao Eletronica - Expedida/Certificada") |
| `prazo_data_envio` | TIMESTAMPTZ | Data/hora de envio da intimacao |
| `prazo_inicio` | TIMESTAMPTZ | Inicio da contagem do prazo |
| `prazo_final` | TIMESTAMPTZ | **Data limite do prazo** (critico para alertas!) |
| `first_seen_at` | TIMESTAMPTZ | Quando o processo apareceu pela primeira vez no sistema |
| `last_synced_at` | TIMESTAMPTZ | Ultimo sync bem-sucedido |
| `created_at` | TIMESTAMPTZ | Criacao do registro |
| `updated_at` | TIMESTAMPTZ | Ultima atualizacao (trigger automatico) |

**Indices:** `cnj` (unique), `prazo_final` (para queries de prazos proximos)

#### Estrutura JSONB: `assuntos`
```json
[
  {
    "codigo": "14815",
    "descricao": "Inventario e Partilha"
  }
]
```

#### Estrutura JSONB: `partes`
```json
[
  {
    "tipo": "AUTOR",
    "nome": "JOAO DA SILVA",
    "cpf_cnpj": "123.456.789-00",
    "qualificacao": "Inventariante",
    "representantes": [
      {
        "nome": "JAIME DARLAN MARTINS",
        "oab": "RS053253",
        "tipo": "Advogado"
      }
    ]
  },
  {
    "tipo": "REU",
    "nome": "MARIA DA SILVA",
    "cpf_cnpj": "987.654.321-00",
    "qualificacao": "",
    "representantes": [
      {
        "nome": "VANESSA BARBOSA",
        "oab": "RS116097",
        "tipo": "Advogado"
      }
    ]
  }
]
```

**Tipos de parte comuns:** `AUTOR`, `REU`, `REQUERENTE`, `REQUERIDO`, `EXEQUENTE`, `EXECUTADO`, `HERDEIRO`, `REPRESENTANTE LEGAL`, `MINISTERIO PUBLICO`

**Tipos de representante:** `Advogado` (OAB), `DPE` (Defensoria Publica)

**Nota:** Os campos `prazo_*` na tabela `processos` guardam o **prazo mais urgente** (menor `prazo_final`). Para ver todos os prazos, use a tabela `prazos_abertos`.

---

### 2. `prazos_abertos` — Prazos abertos por processo (N:1)

Cada linha = um prazo aberto. Um processo pode ter **multiplos prazos simultaneos** (ex: intimacao + citacao com datas diferentes).

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno |
| `processo_id` | UUID (FK -> processos.id) | Processo pai. CASCADE on delete |
| `evento_descricao` | TEXT | Descricao do evento que gerou o prazo (ex: "Intimacao Eletronica - Expedida/Certificada") |
| `data_envio` | TIMESTAMPTZ | Data/hora de envio da intimacao |
| `prazo_inicio` | TIMESTAMPTZ | Inicio da contagem do prazo |
| `prazo_final` | TIMESTAMPTZ | **Data limite do prazo** (NOT NULL) |

**Constraint:** UNIQUE (processo_id, evento_descricao, prazo_final)
**Indices:** `processo_id`, `prazo_final`

#### Relacao com processos
- A tabela `processos` mantem campos `prazo_*` com o **prazo mais urgente** (retrocompatibilidade)
- A tabela `prazos_abertos` contem **todos** os prazos do processo
- A cada sync, os prazos antigos sao deletados e os atuais reinseridos

#### Query: Todos os prazos de um processo
```sql
SELECT pa.evento_descricao, pa.data_envio, pa.prazo_inicio, pa.prazo_final
FROM prazos_abertos pa
JOIN processos p ON pa.processo_id = p.id
WHERE p.cnj = '5001531-15.2025.8.21.0094'
ORDER BY pa.prazo_final ASC;
```

#### Query: Processos com mais de 1 prazo aberto
```sql
SELECT p.cnj, COUNT(*) AS total_prazos, MIN(pa.prazo_final) AS prazo_mais_urgente
FROM prazos_abertos pa
JOIN processos p ON pa.processo_id = p.id
GROUP BY p.cnj
HAVING COUNT(*) > 1
ORDER BY prazo_mais_urgente ASC;
```

---

### 3. `eventos` — Movimentacoes do processo

Cada linha = um evento/movimentacao processual. Ordenados por `numero_evento` (crescente = mais antigo primeiro).

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno |
| `processo_id` | UUID (FK -> processos.id) | Processo pai. CASCADE on delete |
| `numero_evento` | INTEGER | Numero sequencial do evento no processo (1, 2, 3...) |
| `data_hora` | TIMESTAMPTZ | Data e hora do evento (fuso America/Sao_Paulo, armazenado em UTC) |
| `descricao` | TEXT | Texto completo do evento. Pode conter informacoes de prazo inline |
| `usuario` | TEXT | Usuario que registrou o evento (nome do servidor/juiz) |
| `tem_prazo` | BOOLEAN | Se o evento contem informacao de prazo (`true` quando tem "Prazo:" e "Status:" na descricao) |
| `prazo_dias` | INTEGER | Quantidade de dias do prazo (ex: 5, 15, 30) |
| `prazo_status` | TEXT | Status do prazo: `"ABERTO"` ou `"FECHADO"` |
| `prazo_data_inicial` | TIMESTAMPTZ | Data de inicio da contagem do prazo |
| `prazo_data_final` | TIMESTAMPTZ | Data final do prazo |
| `evento_referencia` | INTEGER | Numero do evento ao qual este se refere (ex: "Refer. ao Evento 34") |
| `urgente` | BOOLEAN | Se o evento contem a marcacao "URGENTE" |

**Constraint:** UNIQUE (processo_id, numero_evento)
**Indices:** `processo_id`, `(processo_id, numero_evento)`

#### Exemplos de descricao com prazo
```
Intimacao Eletronica - Expedida/Certificada - Prazo: 15 dias -
Status:ABERTO (34 - REPLICA) - Data inicial da contagem do prazo:
11/02/2026 00:00:00 - Data final: 19/02/2026 23:59:59
```

#### Exemplos de descricao com referencia
```
Peticao - Refer. ao Evento 34
```

---

### 4. `documentos` — Arquivos anexados aos eventos

Cada linha = um documento (PDF, imagem, video, audio) vinculado a um evento. Os arquivos estao no Supabase Storage.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno |
| `processo_id` | UUID (FK -> processos.id) | Processo pai. CASCADE on delete |
| `evento_id` | UUID (FK -> eventos.id) | Evento pai. CASCADE on delete |
| `numero_evento` | INTEGER | Numero do evento (desnormalizado para facilitar queries) |
| `nome_original` | TEXT | Nome do documento no eProc (ex: "PET1", "SENT1", "EXTR2", "FOTO23") |
| `tipo` | TEXT | Tipo detectado por magic bytes: `"PDF"`, `"IMG"`, `"VIDEO"`, `"AUDIO"`, `"HTML"`, `"ARQUIVO"`, `"OUTRO"` |
| `url_eproc` | TEXT | URL relativa do documento no eProc (para re-download se necessario) |
| `storage_path` | TEXT | Caminho no Supabase Storage (ex: `5001531-15.2025.8.21.0094/evt_01/PET1.pdf`) |
| `storage_url` | TEXT | URL publica do documento no Supabase Storage |
| `tamanho_bytes` | BIGINT | Tamanho do arquivo em bytes |
| `hash_sha256` | TEXT | Hash SHA-256 do arquivo (para verificacao de integridade) |

**Constraint:** UNIQUE (processo_id, numero_evento, url_eproc)
**Indices:** `processo_id`, `evento_id`

#### Estrutura do Storage
```
process-documents/
  {cnj}/
    evt_01/
      PET1.pdf
      PROC2.pdf
    evt_05/
      SENT1.pdf
      ACOR2.pdf
    evt_10/
      FOTO23.jpg
      VIDEO25.mp4
```

#### Nomes de documentos comuns no eProc
| Prefixo | Significado |
|---------|-------------|
| PET | Peticao |
| INIC | Peticao Inicial |
| SENT | Sentenca |
| ACOR | Acordao |
| DESPADEC | Despacho/Decisao |
| ATOORD | Ato Ordinatorio |
| CERT / CERTOBT | Certidao |
| OFIC | Oficio |
| EXTR | Extrato / Documento externo |
| COMP | Comprovante |
| CALC | Calculo |
| PROC | Procuracao |
| RG | Documento de identidade |
| AR | Aviso de Recebimento |
| CARTA | Carta precatoria/rogatoria |
| EDITAL | Edital |
| SISBAJUD | Ordem SISBAJUD (bloqueio judicial) |
| CUSTAS | Custas processuais |
| FOTO | Fotografia |
| VIDEO | Video |
| BOC | Boletim de Ocorrencia |
| LAUDO | Laudo pericial |
| REPLICA | Replica |
| CONTRSOCIAL | Contrato Social |
| CNPJ | Cartao CNPJ |
| TERMCOMPR | Termo de Compromisso |
| MATRIMOVEL | Matricula de imovel |
| HABILITACAO | Habilitacao |
| ESCRITURA | Escritura |
| FICHIND | Ficha individual |
| DECLPOBRE | Declaracao de pobreza |
| TIT_EXEC_JUD | Titulo executivo judicial |

---

### 5. `sync_log` — Log de execucao do sync

Cada linha = uma execucao do algoritmo de sync. Util para monitoramento e debug.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno |
| `started_at` | TIMESTAMPTZ | Inicio da execucao |
| `finished_at` | TIMESTAMPTZ | Fim da execucao (null se ainda rodando) |
| `status` | TEXT | `"running"`, `"success"`, `"partial"` (com erros), `"error"` (falha total) |
| `processos_added` | INTEGER | Processos novos adicionados neste ciclo |
| `processos_removed` | INTEGER | Processos removidos (prazo encerrado) |
| `processos_updated` | INTEGER | Processos atualizados (campos de prazo + eventos novos) |
| `documentos_uploaded` | INTEGER | Documentos baixados e uploadados neste ciclo |
| `error_message` | TEXT | Mensagem de erro (truncada em 500 chars) se status != success |

---

## View: `v_processo_completo`

View materializada que retorna **tudo de um processo em uma unica query**. Projetada para consumo direto por N8N e dashboards.

```sql
SELECT * FROM v_processo_completo;
-- ou filtrar:
SELECT * FROM v_processo_completo WHERE prazo_final < NOW() + INTERVAL '3 days';
```

### Colunas retornadas
| Coluna | Tipo | Origem |
|--------|------|--------|
| `processo_id` | UUID | processos.id |
| `cnj` | TEXT | processos.cnj |
| `classe` | TEXT | processos.classe |
| `competencia` | TEXT | processos.competencia |
| `data_autuacao` | DATE | processos.data_autuacao |
| `situacao` | TEXT | processos.situacao |
| `orgao_julgador` | TEXT | processos.orgao_julgador |
| `juiz` | TEXT | processos.juiz |
| `juizo` | TEXT | processos.juizo |
| `lado_advogado` | TEXT | processos.lado_advogado |
| `assuntos` | JSONB | processos.assuntos |
| `partes` | JSONB | processos.partes |
| `prazo_evento_descricao` | TEXT | processos.prazo_evento_descricao |
| `prazo_inicio` | TIMESTAMPTZ | processos.prazo_inicio (mais urgente) |
| `prazo_final` | TIMESTAMPTZ | processos.prazo_final (mais urgente) |
| `last_synced_at` | TIMESTAMPTZ | processos.last_synced_at |
| `prazos` | JSON | Subquery agregada de prazos_abertos (ver abaixo) |
| `eventos` | JSON | Subquery agregada (ver abaixo) |

### Estrutura da coluna `prazos` (JSON agregado)

```json
[
  {
    "evento_descricao": "Intimacao Eletronica - Expedida/Certificada",
    "data_envio": "2026-02-06T09:09:00-03:00",
    "prazo_inicio": "2026-02-11T00:00:00-03:00",
    "prazo_final": "2026-02-19T23:59:59-03:00"
  },
  {
    "evento_descricao": "Citacao - Prazo para Contestar",
    "data_envio": "2026-02-10T14:00:00-03:00",
    "prazo_inicio": "2026-02-12T00:00:00-03:00",
    "prazo_final": "2026-02-27T23:59:59-03:00"
  }
]
```

Os prazos vem ordenados por `prazo_final ASC` (mais urgente primeiro).

### Estrutura da coluna `eventos` (JSON agregado)
```json
[
  {
    "numero": 35,
    "data_hora": "2026-02-15T14:30:00-03:00",
    "descricao": "Peticao - Refer. ao Evento 34",
    "usuario": "JOAO SERVIDOR",
    "tem_prazo": false,
    "prazo_status": null,
    "prazo_data_final": null,
    "urgente": false,
    "documentos": [
      {
        "nome": "PET1",
        "tipo": "PDF",
        "storage_url": "https://xxx.supabase.co/storage/v1/object/public/process-documents/..."
      }
    ]
  }
]
```
Os eventos vem ordenados por `numero_evento DESC` (mais recente primeiro).

---

## Queries uteis

### Processos com prazo vencendo nos proximos 3 dias
```sql
SELECT cnj, classe, lado_advogado, prazo_final,
       prazo_final - NOW() AS tempo_restante
FROM processos
WHERE prazo_final BETWEEN NOW() AND NOW() + INTERVAL '3 days'
ORDER BY prazo_final ASC;
```

### Processos com prazo vencido (nao respondido)
```sql
SELECT cnj, classe, prazo_final
FROM processos
WHERE prazo_final < NOW()
ORDER BY prazo_final DESC;
```

### Todos os documentos de um processo
```sql
SELECT d.nome_original, d.tipo, d.tamanho_bytes, d.storage_url,
       e.numero_evento, e.data_hora, e.descricao
FROM documentos d
JOIN eventos e ON d.evento_id = e.id
WHERE d.processo_id = 'UUID_AQUI'
ORDER BY e.numero_evento DESC;
```

### Estatisticas gerais
```sql
SELECT
  (SELECT COUNT(*) FROM processos) AS total_processos,
  (SELECT COUNT(*) FROM processos WHERE lado_advogado = 'AUTOR') AS como_autor,
  (SELECT COUNT(*) FROM processos WHERE lado_advogado LIKE '%R_U%' OR lado_advogado LIKE '%REQUERIDO%' OR lado_advogado LIKE '%EXECUTADO%') AS como_reu,
  (SELECT COUNT(*) FROM eventos) AS total_eventos,
  (SELECT COUNT(*) FROM documentos) AS total_documentos,
  (SELECT COALESCE(SUM(tamanho_bytes), 0) FROM documentos) AS total_bytes;
```

### Ultimo sync
```sql
SELECT * FROM sync_log
ORDER BY started_at DESC
LIMIT 1;
```

### Historico de sync (ultimos 10)
```sql
SELECT started_at, finished_at,
       finished_at - started_at AS duracao,
       status, processos_added, processos_updated, documentos_uploaded,
       error_message
FROM sync_log
ORDER BY started_at DESC
LIMIT 10;
```

### Processos agrupados por classe
```sql
SELECT classe, COUNT(*) AS total,
       COUNT(*) FILTER (WHERE lado_advogado = 'AUTOR') AS como_autor,
       COUNT(*) FILTER (WHERE lado_advogado != 'AUTOR') AS como_reu
FROM processos
GROUP BY classe
ORDER BY total DESC;
```

### Prazos abertos vencendo nos proximos 3 dias (todos, nao apenas o mais urgente)

```sql
SELECT p.cnj, p.classe, pa.evento_descricao, pa.prazo_final,
       pa.prazo_final - NOW() AS tempo_restante
FROM prazos_abertos pa
JOIN processos p ON pa.processo_id = p.id
WHERE pa.prazo_final BETWEEN NOW() AND NOW() + INTERVAL '3 days'
ORDER BY pa.prazo_final ASC;
```

### Eventos urgentes
```sql
SELECT p.cnj, e.numero_evento, e.data_hora, e.descricao
FROM eventos e
JOIN processos p ON e.processo_id = p.id
WHERE e.urgente = true
ORDER BY e.data_hora DESC;
```

### Eventos com prazo aberto
```sql
SELECT p.cnj, e.numero_evento, e.prazo_dias, e.prazo_status,
       e.prazo_data_inicial, e.prazo_data_final
FROM eventos e
JOIN processos p ON e.processo_id = p.id
WHERE e.tem_prazo = true AND e.prazo_status = 'ABERTO'
ORDER BY e.prazo_data_final ASC;
```

---

## Supabase Storage

### Bucket: `process-documents`
- **Acesso:** Publico (URLs publicas para integracoes)
- **Estrutura:** `{cnj}/evt_{numero_evento:02d}/{nome_documento}.{ext}`
- **Content-types:** Automatico baseado na extensao (PDF, PNG, JPG, MP4, MP3, etc.)

### Exemplo de URL
```
https://{project-id}.supabase.co/storage/v1/object/public/process-documents/5001531-15.2025.8.21.0094/evt_01/PET1.pdf
```

---

## Conexao Supabase

### Variaveis de ambiente necessarias
```
SUPABASE_URL=https://{project-id}.supabase.co
SUPABASE_KEY={service-role-key}
```

### Acesso via API REST (para N8N)
O Supabase expoe automaticamente uma API REST para todas as tabelas e views:

```
GET  {SUPABASE_URL}/rest/v1/processos?select=*
GET  {SUPABASE_URL}/rest/v1/v_processo_completo?select=*
GET  {SUPABASE_URL}/rest/v1/prazos_abertos?processo_id=eq.{UUID}
GET  {SUPABASE_URL}/rest/v1/eventos?processo_id=eq.{UUID}
GET  {SUPABASE_URL}/rest/v1/documentos?processo_id=eq.{UUID}
GET  {SUPABASE_URL}/rest/v1/sync_log?order=started_at.desc&limit=1
```

Headers obrigatorios:
```
apikey: {SUPABASE_KEY}
Authorization: Bearer {SUPABASE_KEY}
```

### Acesso via SDK Python
```python
from supabase import create_client
sb = create_client(SUPABASE_URL, SUPABASE_KEY)
processos = sb.table("processos").select("*").execute().data
```

---

## Comportamento do sync

### Adicao (processo novo)
1. Abre pagina do processo no eProc
2. Extrai header (classe, competencia, situacao, juiz, orgao julgador)
3. Extrai assuntos (JSONB)
4. Extrai partes e advogados (JSONB)
5. Identifica lado do advogado (AUTOR/REU)
6. UPSERT no `processos` (ON CONFLICT cnj) — campos `prazo_*` = prazo mais urgente
7. Sincroniza `prazos_abertos` (deleta antigos + insere todos os atuais)
8. Extrai todos os eventos
9. UPSERT cada evento (ON CONFLICT processo_id, numero_evento)
10. Para cada documento de cada evento: download + upload Storage + UPSERT documentos

### Atualizacao (processo existente)
1. Atualiza campos de prazo no `processos` (prazo mais urgente)
2. Sincroniza `prazos_abertos` (deleta antigos + insere todos os atuais)
3. Abre pagina do processo
4. Extrai eventos
5. Filtra apenas eventos novos (numero > ultimo conhecido)
6. UPSERT eventos novos + download documentos novos

### Remocao (processo sumiu do eProc)
1. Significa que o prazo foi respondido ou expirou
2. Deleta documentos do Storage (recursivo)
3. Deleta o processo da DB (CASCADE deleta eventos e documentos)
4. **Protecao:** se eProc retorna 0 processos mas DB tem dados, nao deleta nada (possivel erro de navegacao)

### Idempotencia
Todas as operacoes usam UPSERT com ON CONFLICT, tornando seguro re-executar o sync multiplas vezes sem duplicar dados.

---

## Volumes de dados tipicos

Para um escritorio com ~27 processos ativos:
- **Processos:** ~27 registros
- **Eventos:** ~2.000-5.000 registros (media de 100-200 eventos por processo)
- **Documentos:** ~1.500-3.000 registros
- **Storage:** ~500MB-2GB de arquivos
- **Tempo de sync completo:** ~2-4 horas (com proxy, 1 processo por ciclo de 30s)
