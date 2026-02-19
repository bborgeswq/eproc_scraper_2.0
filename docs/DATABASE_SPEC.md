# eProc Scraper 2.0 — Database Specification

## Visao geral

Sistema de extracao automatizada do portal judicial **eProc 1G TJRS**. O scraper faz login via Keycloak SSO + TOTP 2FA, extrai todos os processos com prazos abertos do advogado, e sincroniza com **Supabase** (PostgreSQL + Storage).

### Stack

- **Scraper:** Python 3.12 + Playwright
- **Database:** Supabase (PostgreSQL 15)
- **Storage:** Supabase Storage (bucket `process-documents`)
- **Deploy:** Docker container no Easypanel

### Ciclo de sync

1. Login no eProc via Keycloak SSO + TOTP
2. Navega para "Prazos Abertos" e extrai lista de processos (CNJ + dados de prazo)
3. Compara com DB: adiciona novos, remove os que sairam
4. Sincroniza `prazos_abertos` para TODOS os processos (rapido, sem abrir paginas)
5. Para cada processo: scrape completo (header, partes, assuntos, eventos, documentos)
6. Eventos com prazo aberto sao identificados pela **cor amarela** da celula no eProc
7. Documentos sao baixados do eProc e uploadados para Supabase Storage
8. Aguarda 24h e repete

---

## Tabelas

### 1. `processos` — Tabela central (PK = CNJ)

Cada linha = um processo judicial com prazo em aberto.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `cnj` | TEXT (PK) | Numero CNJ do processo. Formato: `NNNNNNN-NN.NNNN.N.NN.NNNN` |
| `classe` | TEXT | Classe processual (ex: "Inventario", "Cumprimento de Sentenca") |
| `competencia` | TEXT | Competencia do juizo (ex: "Civel", "Familia") |
| `data_autuacao` | DATE | Data de autuacao (formato ISO: `YYYY-MM-DD`) |
| `situacao` | TEXT | Situacao atual (ex: "Em andamento", "Suspenso") |
| `orgao_julgador` | TEXT | Orgao julgador (ex: "1a Vara Civel de Tramandai") |
| `juiz` | TEXT | Nome do juiz(a) responsavel |
| `juizo` | TEXT | Juizo (extraido da tabela de prazos) |
| `lado_advogado` | TEXT | Lado do advogado no processo: `"AUTOR"`, `"REU"`, `"REQUERENTE"`, etc. |
| `processos_relacionados` | TEXT[] | Array de CNJs de processos relacionados |
| `assuntos` | JSONB | Array de assuntos do processo |
| `partes` | JSONB | Array de partes e representantes |
| `first_seen_at` | TIMESTAMPTZ | Quando o processo apareceu pela primeira vez |
| `last_synced_at` | TIMESTAMPTZ | Ultimo sync bem-sucedido |
| `created_at` | TIMESTAMPTZ | Criacao do registro |
| `updated_at` | TIMESTAMPTZ | Ultima atualizacao (trigger automatico) |

#### Estrutura JSONB: `assuntos`

```json
[
  { "codigo": "14815", "descricao": "Inventario e Partilha" }
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
      { "nome": "JAIME DARLAN MARTINS", "oab": "RS053253", "tipo": "Advogado" }
    ]
  }
]
```

---

### 2. `prazos_abertos` — Prazos ativos (N por processo)

Cada linha = um prazo aberto. Um processo pode ter **multiplos prazos** simultaneos.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `cnj` | TEXT (PK, FK) | CNJ do processo |
| `evento_descricao` | TEXT (PK) | Descricao do evento que gerou o prazo |
| `data_envio` | TIMESTAMPTZ | Data/hora de envio da intimacao |
| `prazo_inicio` | TIMESTAMPTZ | Inicio da contagem do prazo |
| `prazo_final` | TIMESTAMPTZ (PK) | **Data limite do prazo** |

**PK:** (cnj, evento_descricao, prazo_final)

#### Origem dos dados

Os dados vem da pagina "Prazos Abertos" do eProc (`citacao_intimacao_prazo_aberto_listar`), que lista apenas prazos ativos. Quando o advogado responde ou o prazo expira, o eProc remove da lista. A cada sync, prazos antigos sao deletados e os atuais reinseridos.

**Para alertas e dashboards, use `prazos_abertos`.** Para historico de prazos passados, use `eventos` com `prazo_aberto = true`.

---

### 3. `eventos` — Movimentacoes do processo

Cada linha = um evento/movimentacao processual.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `cnj` | TEXT (PK, FK) | CNJ do processo |
| `numero_evento` | INTEGER (PK) | Numero sequencial do evento (1, 2, 3...) |
| `data_hora` | TIMESTAMPTZ | Data e hora do evento |
| `descricao` | TEXT | Texto completo do evento |
| `usuario` | TEXT | Usuario que registrou o evento |
| `prazo_aberto` | BOOLEAN | **Detectado pela cor amarela** da celula no eProc. `true` = prazo em aberto |
| `prazo_dias` | INTEGER | Quantidade de dias do prazo (ex: 5, 15, 30) |
| `prazo_status` | TEXT | `"ABERTO"` ou `"FECHADO"` (extraido do texto) |
| `prazo_data_inicial` | TIMESTAMPTZ | Data de inicio da contagem |
| `prazo_data_final` | TIMESTAMPTZ | Data final do prazo |
| `evento_referencia` | INTEGER | Numero do evento referenciado (ex: "Refer. ao Evento 34") |
| `urgente` | BOOLEAN | Se o evento contem "URGENTE" |

**PK:** (cnj, numero_evento)

#### Deteccao de prazo aberto

O campo `prazo_aberto` e detectado **visualmente**: no eProc, a celula de descricao do evento tem fundo amarelo quando o prazo esta em aberto. O scraper le o `backgroundColor` via JavaScript. Isso e mais confiavel que parsing de texto.

---

### 4. `documentos` — Arquivos dos eventos

Cada linha = um documento (PDF, imagem, video, audio) vinculado a um evento.

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `cnj` | TEXT (PK, FK) | CNJ do processo |
| `numero_evento` | INTEGER (PK, FK) | Numero do evento |
| `nome_original` | TEXT | Nome do documento no eProc (ex: "PET1", "SENT1") |
| `tipo` | TEXT | Tipo detectado: `"PDF"`, `"IMG"`, `"VIDEO"`, `"AUDIO"`, `"HTML"`, `"OUTRO"` |
| `url_eproc` | TEXT (PK) | URL relativa do documento no eProc |
| `storage_path` | TEXT | Caminho no Supabase Storage |
| `storage_url` | TEXT | URL publica do documento |
| `tamanho_bytes` | BIGINT | Tamanho do arquivo em bytes |
| `hash_sha256` | TEXT | Hash SHA-256 do arquivo |

**PK:** (cnj, numero_evento, url_eproc)

**FK:** (cnj, numero_evento) -> eventos(cnj, numero_evento) ON DELETE CASCADE

#### Estrutura do Storage

```
process-documents/
  {cnj}/
    evt_01/
      PET1.pdf
      PROC2.pdf
    evt_05/
      SENT1.pdf
    evt_10/
      FOTO23.jpg
```

---

### 5. `sync_log` — Log de execucao

| Coluna | Tipo | Descricao |
|--------|------|-----------|
| `id` | UUID (PK) | ID interno |
| `started_at` | TIMESTAMPTZ | Inicio da execucao |
| `finished_at` | TIMESTAMPTZ | Fim da execucao |
| `status` | TEXT | `"running"`, `"success"`, `"partial"`, `"error"` |
| `processos_total` | INTEGER | Total de processos no eProc |
| `processos_novos` | INTEGER | Processos novos adicionados |
| `processos_removidos` | INTEGER | Processos removidos |
| `documentos_baixados` | INTEGER | Documentos baixados neste ciclo |
| `erros` | INTEGER | Quantidade de erros |
| `error_message` | TEXT | Mensagem de erro (se aplicavel) |

---

## View: `v_processo_completo`

Retorna tudo de um processo em uma unica query.

```sql
SELECT * FROM v_processo_completo;
SELECT * FROM v_processo_completo WHERE cnj = '5001531-15.2025.8.21.0094';
```

### Colunas retornadas

| Coluna | Tipo | Origem |
|--------|------|--------|
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
| `prazos` | JSON | Agregado de prazos_abertos (ordenado por prazo_final ASC) |
| `eventos` | JSON | Agregado de eventos + documentos (ordenado por numero DESC) |

---

## Queries uteis

### Todos os prazos abertos (1 linha = 1 card no Trello)

```sql
SELECT p.cnj, p.classe, p.lado_advogado, p.juizo,
       pa.evento_descricao, pa.prazo_inicio, pa.prazo_final
FROM prazos_abertos pa
JOIN processos p ON pa.cnj = p.cnj
ORDER BY pa.prazo_final ASC;
```

### Prazos vencendo nos proximos 3 dias

```sql
SELECT p.cnj, p.classe, pa.evento_descricao, pa.prazo_final,
       pa.prazo_final - NOW() AS tempo_restante
FROM prazos_abertos pa
JOIN processos p ON pa.cnj = p.cnj
WHERE pa.prazo_final BETWEEN NOW() AND NOW() + INTERVAL '3 days'
ORDER BY pa.prazo_final ASC;
```

### Processos com mais de 1 prazo aberto

```sql
SELECT cnj, COUNT(*) AS total_prazos, MIN(prazo_final) AS mais_urgente
FROM prazos_abertos
GROUP BY cnj
HAVING COUNT(*) > 1;
```

### Eventos com prazo aberto (detectados pela cor amarela)

```sql
SELECT e.cnj, e.numero_evento, e.descricao, e.prazo_data_final, e.evento_referencia
FROM eventos e
WHERE e.prazo_aberto = true
ORDER BY e.prazo_data_final ASC;
```

### Todos os documentos de um processo

```sql
SELECT d.nome_original, d.tipo, d.tamanho_bytes, d.storage_url,
       e.numero_evento, e.data_hora
FROM documentos d
JOIN eventos e ON d.cnj = e.cnj AND d.numero_evento = e.numero_evento
WHERE d.cnj = '5001531-15.2025.8.21.0094'
ORDER BY e.numero_evento DESC;
```

### Estatisticas gerais

```sql
SELECT
  (SELECT COUNT(*) FROM processos) AS total_processos,
  (SELECT COUNT(*) FROM prazos_abertos) AS total_prazos,
  (SELECT COUNT(*) FROM eventos) AS total_eventos,
  (SELECT COUNT(*) FROM documentos) AS total_documentos,
  (SELECT COALESCE(SUM(tamanho_bytes), 0) FROM documentos) AS total_bytes;
```

---

## Supabase Storage

### Bucket: `process-documents`

- **Acesso:** Publico (URLs publicas para integracoes)
- **Estrutura:** `{cnj}/evt_{numero_evento:02d}/{nome_documento}.{ext}`
- **Content-types:** Automatico baseado na extensao

### Exemplo de URL

```
https://{project-id}.supabase.co/storage/v1/object/public/process-documents/5001531-15.2025.8.21.0094/evt_01/PET1.pdf
```

---

## API REST (para N8N)

```
GET  {SUPABASE_URL}/rest/v1/processos?select=*
GET  {SUPABASE_URL}/rest/v1/v_processo_completo?select=*
GET  {SUPABASE_URL}/rest/v1/prazos_abertos?select=*
GET  {SUPABASE_URL}/rest/v1/eventos?cnj=eq.{CNJ}
GET  {SUPABASE_URL}/rest/v1/documentos?cnj=eq.{CNJ}
GET  {SUPABASE_URL}/rest/v1/sync_log?order=started_at.desc&limit=1
```

Headers obrigatorios:

```
apikey: {SUPABASE_KEY}
Authorization: Bearer {SUPABASE_KEY}
```
