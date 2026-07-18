# PRD: Ingestão no Limite — Pipeline de Empresas

**Produto:** Pipeline de ingestão batch de dados públicos de empresas (RFB)
**Versão:** 1.0
**Data:** 2026-07-17
**Participante:** `roberton003`
**Repositório Solução:** `Roberton003/ingestao-empresas-solucao`

---

## 1. Objetivo

Ingerir **68.629.148 linhas** de dados de empresas (RFB) a partir de **10 arquivos ZIP** em **PostgreSQL**, derivando **6 colunas analíticas** adicionais (total 13 colunas), passando por **13 gates de qualidade de dados (DQ)**, e minimizando o score composto da competição.

**Métrica-objetivo:** Score ≤ **456** (vs líder atual **854,1**)

---

## 2. Restrições (Hard Constraints)

### 2.1 Ambiente de Execução
| Recurso | Limite |
|---------|--------|
| CPU | 2 vCPU |
| RAM | **1 GB (sem swap)** |
| Timeout | **60 minutos** (exit 0 obrigatório) |
| Container | `--cpus=2.0 --memory=1g --memory-swap=1g` |
| Network | `homelab_net` (mesma do PostgreSQL e MinIO) |
| Storage | Tabela no PostgreSQL + bucket S3 (MinIO) |

### 2.2 Consequências de Falha
- **exit 137** (OOM kill): **Desclassificação automática**
- **exit != 0** ou **tempo > 60min**: Gate G2 reprovado
- Qualquer DQ gate com erro > 0: Gate G4 reprovado

### 2.3 Variáveis de Ambiente (injetadas pelo avaliador)
| Variável | Valor |
|----------|-------|
| `PARTICIPANTE` | `roberton003` |
| `PG_TABLE` | `public.roberton003_empresas` |
| `PG_HOST` | `postgres_db` |
| `PG_PORT` | `5432` |
| `PG_USER` | `homelab_postgres` |
| `PG_PASSWORD` | *(injetado)* |
| `PG_DB` | `db_empresas` |
| `S3_ENDPOINT` | `http://minio:9000` |
| `AWS_ACCESS_KEY_ID` | *(injetado)* |
| `AWS_SECRET_ACCESS_KEY` | *(injetado)* |
| `MINIO_BUCKET` | `marketing-leads` |

---

## 3. Dataset

### 3.1 Fonte
- 10 arquivos `.zip` em `/data/`
- `Empresas0.zip` — 28.175.408 linhas (~2,1 GB descomprimido)
- `Empresas1.zip` a `Empresas9.zip` — 4.494.860 linhas cada
- **Total: 68.629.148 linhas** (faixa aceita: 68,56M – 68,70M)

### 3.2 Formato
| Propriedade | Valor |
|-------------|-------|
| Encoding | **ISO-8859-1** (deve ser convertido para UTF-8) |
| Separador | `;` (ponto-e-vírgula) |
| Quote | Aspas duplas (`"`) |
| Cabeçalho | **Sem cabeçalho** |
| Tamanho | 1,26 GB comprimido / ~5,0 GB descomprimido |

### 3.3 Colunas Origem (7)
| # | Campo | Tipo esperado |
|---|-------|---------------|
| 1 | `cnpj_basico` | VARCHAR(8) — 8 dígitos zero-padded |
| 2 | `razao_social` | VARCHAR — UPPER, TRIM (espaços internos permitidos) |
| 3 | `natureza_juridica` | VARCHAR(4) — 4 dígitos numéricos |
| 4 | `qualificacao_responsavel` | VARCHAR — NOT NULL |
| 5 | `capital_social` | DOUBLE PRECISION — vírgula BR → ponto |
| 6 | `porte_codigo` | VARCHAR(2) — 00/01/03/05 |
| 7 | `ente_federativo` | VARCHAR — vazio → NULL |

### 3.4 Características Conhecidas
| Característica | Quantidade |
|----------------|------------|
| Linhas com `;` dentro de aspas | 323 |
| Linhas com bytes não-ASCII | 6.933 |
| Valores vazios em `porte_codigo` | 4.063 |
| `capital_social` com vírgula BR | **100%** |
| `capital_social` = 0,00 | ~25,6% |
| `ente_federativo` vazio | **99,9%** |

---

## 4. Schema Final (13 Colunas)

### 4.1 DDL Otimizado

```sql
CREATE TABLE public.roberton003_empresas (
    cnpj_basico           VARCHAR(8) NOT NULL,
    razao_social          VARCHAR(200) NOT NULL,
    natureza_juridica     VARCHAR(4) NOT NULL,
    qualificacao_responsavel VARCHAR(2) NOT NULL,
    capital_social        DOUBLE PRECISION NOT NULL,
    porte_codigo          VARCHAR(2) NOT NULL,
    porte_descricao       VARCHAR(20) NOT NULL,
    ente_federativo       VARCHAR(100),
    capital_social_faixa  VARCHAR(20) NOT NULL,
    is_mei                BOOLEAN NOT NULL,
    natureza_juridica_grupo VARCHAR(30) NOT NULL,
    ente_federativo_presente BOOLEAN NOT NULL,
    data_processamento    TIMESTAMP NOT NULL
) WITH (fillfactor=100, autovacuum_enabled=false);
```

### 4.2 Justificativa de Tipos
| Coluna | Tipo | Motivo |
|--------|------|--------|
| `cnpj_basico` | VARCHAR(8) | 8 dígitos, zero-padded, sem cálculo |
| `razao_social` | VARCHAR(200) | 200 caracteres (TOAST threshold ~2KB) |
| `natureza_juridica` | VARCHAR(4) | 4 dígitos numéricos |
| `qualificacao_responsavel` | VARCHAR(2) | Código de 2 dígitos |
| `capital_social` | DOUBLE PRECISION | Precisão para valores monetários |
| `porte_codigo` | VARCHAR(2) | 00/01/03/05 |
| `porte_descricao` | VARCHAR(20) | Rótulo descritivo curto |
| `ente_federativo` | VARCHAR(100) | Nulo quando vazio |
| `capital_social_faixa` | VARCHAR(20) | Faixa categórica |
| `is_mei` | BOOLEAN | 1 byte vs texto |
| `natureza_juridica_grupo` | VARCHAR(30) | Rótulo de grupo |
| `ente_federativo_presente` | BOOLEAN | 1 byte |
| `data_processamento` | TIMESTAMP | Carimbo de ingestão |

### 4.3 Regras NOT NULL
- **TODAS** as colunas são NOT NULL **exceto** `ente_federativo`
- `ente_federativo` vazio na origem → `NULL`

---

## 5. Derivações SQL (6 Colunas)

### 5.1 `porte_descricao` (DQ-07)
```sql
CASE porte_codigo
    WHEN '00' THEN 'NÃO INFORMADO'
    WHEN '01' THEN 'MICRO EMPRESA'
    WHEN '03' THEN 'EMPRESA DE PEQUENO PORTE'
    WHEN '05' THEN 'DEMAIS'
    ELSE 'NÃO INFORMADO'
END
```
Normalização: `porte_codigo` vazio → `'00'` antes do CASE.

### 5.2 `capital_social_faixa` (DQ-05)
```sql
CASE
    WHEN capital_social = 0 THEN 'SEM CAPITAL'
    WHEN capital_social <= 1000 THEN 'ATÉ 1K'
    WHEN capital_social <= 10000 THEN '1K A 10K'
    WHEN capital_social <= 100000 THEN '10K A 100K'
    WHEN capital_social <= 1000000 THEN '100K A 1M'
    ELSE 'ACIMA DE 1M'
END
```

### 5.3 `is_mei` (DQ-08)
```sql
CASE
    WHEN razao_social ~ '\d{11}$' THEN TRUE
    ELSE FALSE
END
```
MEI = razão social termina em 11 dígitos (CPF do titular).

### 5.4 `natureza_juridica_grupo` (DQ-11)
```sql
CASE SUBSTRING(natureza_juridica FROM 1 FOR 1)
    WHEN '1' THEN 'ADMIN PÚBLICA'
    WHEN '2' THEN 'ENT EMPRESARIAIS'
    WHEN '3' THEN 'ENT S/ FINS LUCRATIVOS'
    WHEN '4' THEN 'PESSOAS FÍSICAS'
    WHEN '5' THEN 'ORG INTERNACIONAIS'
    ELSE 'OUTROS'
END
```

### 5.5 `ente_federativo_presente` (DQ-12)
```sql
CASE
    WHEN ente_federativo IS NOT NULL AND ente_federativo != '' THEN TRUE
    ELSE FALSE
END
```

### 5.6 `data_processamento` (DQ-13)
```sql
CURRENT_TIMESTAMP  -- carimbo fixo no momento da inserção
```

---

## 6. DQ Gates (13 Validações)

Todas as queries devem retornar **0** (zero) para aprovação.

### DQ-01: `cnpj_basico` 8 dígitos
```sql
SELECT COUNT(*) FROM empresas
WHERE LENGTH(cnpj_basico) != 8 OR cnpj_basico ~ '\D';
```

### DQ-02: `razao_social` UPPER (espaços internos permitidos, TRIM nas bordas)
```sql
SELECT COUNT(*) FROM empresas
WHERE razao_social != UPPER(TRIM(razao_social));
```

### DQ-03: `natureza_juridica` 4 dígitos numéricos
```sql
SELECT COUNT(*) FROM empresas
WHERE LENGTH(natureza_juridica) != 4 OR natureza_juridica ~ '\D';
```

### DQ-04: `qualificacao_responsavel` NOT NULL/vazio
```sql
SELECT COUNT(*) FROM empresas
WHERE qualificacao_responsavel IS NULL OR qualificacao_responsavel = '';
```

### DQ-05: `capital_social_faixa` consistente (mesma lógica CASE da derivação)
```sql
SELECT COUNT(*) FROM empresas
WHERE (capital_social = 0 AND capital_social_faixa != 'SEM CAPITAL')
   OR (capital_social > 0 AND capital_social <= 1000 AND capital_social_faixa != 'ATÉ 1K')
   OR (capital_social > 1000 AND capital_social <= 10000 AND capital_social_faixa != '1K A 10K')
   OR (capital_social > 10000 AND capital_social <= 100000 AND capital_social_faixa != '10K A 100K')
   OR (capital_social > 100000 AND capital_social <= 1000000 AND capital_social_faixa != '100K A 1M')
   OR (capital_social > 1000000 AND capital_social_faixa != 'ACIMA DE 1M')
   OR capital_social_faixa IS NULL;
```

### DQ-06: `porte_codigo` ∈ {00, 01, 03, 05}
```sql
SELECT COUNT(*) FROM empresas
WHERE porte_codigo NOT IN ('00', '01', '03', '05');
```

### DQ-07: `porte_descricao` consistente
```sql
SELECT COUNT(*) FROM empresas
WHERE (porte_codigo = '00' AND porte_descricao != 'NÃO INFORMADO')
   OR (porte_codigo = '01' AND porte_descricao != 'MICRO EMPRESA')
   OR (porte_codigo = '03' AND porte_descricao != 'EMPRESA DE PEQUENO PORTE')
   OR (porte_codigo = '05' AND porte_descricao != 'DEMAIS');
```

### DQ-08: `is_mei` consistente (11 dígitos finais)
```sql
SELECT COUNT(*) FROM empresas
WHERE (is_mei = TRUE AND razao_social !~ '\d{11}$')
   OR (is_mei = FALSE AND razao_social ~ '\d{11}$');
```

### DQ-09: `cnpj_basico` único (rodado no DuckDB pré-INSERT, sem índice necessário)
```sql
SELECT COUNT(*) FROM (
    SELECT cnpj_basico FROM empresas
    GROUP BY cnpj_basico HAVING COUNT(*) > 1
) t;
```

### DQ-10: `razao_social` NOT NULL + encoding UTF-8 válido
```sql
SELECT COUNT(*) FROM empresas
WHERE razao_social IS NULL
   OR razao_social ~ '[\x80-\xFF]';  -- bytes não-ASCII indicam ISO-8859-1 não convertido
```

### DQ-11: `natureza_juridica_grupo` consistente
```sql
SELECT COUNT(*) FROM empresas
WHERE (SUBSTRING(natureza_juridica FROM 1 FOR 1) = '1' AND natureza_juridica_grupo != 'ADMIN PÚBLICA')
   OR (SUBSTRING(natureza_juridica FROM 1 FOR 1) = '2' AND natureza_juridica_grupo != 'ENT EMPRESARIAIS')
   OR (SUBSTRING(natureza_juridica FROM 1 FOR 1) = '3' AND natureza_juridica_grupo != 'ENT S/ FINS LUCRATIVOS')
   OR (SUBSTRING(natureza_juridica FROM 1 FOR 1) = '4' AND natureza_juridica_grupo != 'PESSOAS FÍSICAS')
   OR (SUBSTRING(natureza_juridica FROM 1 FOR 1) = '5' AND natureza_juridica_grupo != 'ORG INTERNACIONAIS')
   OR (SUBSTRING(natureza_juridica FROM 1 FOR 1) NOT IN ('1','2','3','4','5') AND natureza_juridica_grupo != 'OUTROS');
```

### DQ-12: `ente_federativo_presente` consistente
```sql
SELECT COUNT(*) FROM empresas
WHERE (ente_federativo IS NOT NULL AND ente_federativo != '' AND ente_federativo_presente != TRUE)
   OR ((ente_federativo IS NULL OR ente_federativo = '') AND ente_federativo_presente != FALSE);
```

### DQ-13: `data_processamento` NOT NULL
```sql
SELECT COUNT(*) FROM empresas
WHERE data_processamento IS NULL;
```

---

## 7. Arquitetura do Pipeline

### 7.1 Stack
| Componente | Função |
|------------|--------|
| **DuckDB 1.5+** | Parsing CSV vetorizado, encoding ISO-8859-1, derivações SQL |
| **psycopg2-binary** | COPY PostgreSQL via conexão nativa |
| **Docker** | Containerização única (Python + DuckDB + psycopg2) |
| **PostgreSQL** | Tabela final `public.roberton003_empresas` |

### 7.2 Fluxo
```
ZIP (10 arquivos)
  │
  ▼
[Python zipfile] → bytes ISO-8859-1
  │
  ▼
[DuckDB read_csv]
  ├─ encoding='iso-8859-1'
  ├─ sep=';', quote='"', header=false
  └─ schema explícito (all_varchar=true)
  │
  ▼
[DuckDB SQL]
  ├─ UPPER(TRIM(razao_social))
  ├─ LPAD(cnpj_basico, 8, '0')
  ├─ REPLACE(REPLACE(capital_social, '.', ''), ',', '.')::DOUBLE  -- remove ponto milhar, troca vírgula
  ├─ CASE porte_codigo → 6 derivações
  └─ CURRENT_TIMESTAMP
  │
  ▼
[DuckDB COPY TO STDOUT] (sem arquivo intermediário)
  └─ Buffer StringIO em memória (50k linhas por chunk)
  │
  ▼
[psycopg2 copy_expert]
  ├─ COPY FROM STDIN WITH CSV
  ├─ synchronous_commit = off
  └─ Chunks de 50k linhas (evita OOM no buffer Python)
  │
  ▼
[PostgreSQL] (sem índices durante LOAD)
  └─ Índices pós-LOAD (se necessário)
```

### 7.3 Configuração DuckDB
```sql
SET memory_limit = '400MB';
SET temp_directory = '/app/duckdb_temp';
SET threads = 2;
```

### 7.4 Configuração PostgreSQL (session-level)
```sql
SET synchronous_commit = off;
SET maintenance_work_mem = '256MB';
```

### 7.5 Idempotência
- **TRUNCATE** antes de cada LOAD
- Se o pipeline falhar no meio: TRUNCATE + restart do chunk atual
- Sem checkpoint parcial (pela faixa de linhas, TRUNCATE é rápido)

---

## 8. Score-Alvo

### 8.1 Fórmula do Score
```
score = 1000 × (0.60 × tempo_seg / 3600 + 0.25 × peak_ram_mb / 1024 + 0.15 × storage_total_mb / 4096)
```

**Menor score = Melhor**

### 8.2 Cenários (estimativa storage revisada — ver Apêndice A)
| Cenário | Tempo (s) | RAM (MB) | Storage (MB) | Score |
|---------|-----------|----------|--------------|-------|
| **Líder atual** | 2.215 | 56 | 12.872 | **854** |
| **Realista** | **1.200** | **300** | **8.783** | **595** |
| Agressivo | 900 | 250 | 8.783 | **535** |
| <span title="Se COPY otimizado + tipos compactos + ente_federativo domina NULL">Otimista</span> | 1.200 | 300 | 6.500 | **504** |

### 8.3 Estratégia de Pontuação
| Componente | Peso | Estratégia |
|------------|------|------------|
| Tempo (60%) | 360 pts | DuckDB C++ engine + COPY streaming, chunks 50k |
| RAM (25%) | 150 pts | `memory_limit='400MB'`, streaming, `/app/duckdb_temp` |
| Storage (15%) | 90 pts | Tipos compactos, fillfactor=100, sem TOAST, sem índices |

### 8.4 Armadilha do Líder
O líder atual gasta **12.872 MB storage** — ~187 bytes/linha, provavelmente por índices extras
e storage S3 adicional. Nosso schema sem índices + fillfactor=100 deve atingir **~128 bytes/linha**,
ou **~8.783 MB** — ainda assim **~32% menos** que o líder em storage (311 pts vs 471 pts).

---

## 9. Riscos e Mitigações

| # | Risco | Prob | Impacto | Mitigação |
|---|-------|------|---------|-----------|
| 1 | **OOM** - DuckDB materializa dataset inteiro | Média | **Fatal** | `memory_limit='400MB'`, `temp_directory='/app/duckdb_temp'` (fs regular, não tmpfs) |
| 2 | **COPY lento** - PostgreSQL I/O-bound | Alta | Alto | `synchronous_commit=off`, chunks 100k |
| 3 | **Encoding** - ISO-8859-1 mal convertido | Baixa | Alto | DuckDB `encoding='iso-8859-1'` nativo |
| 4 | **porte_codigo vazio** → DQ-06 falha | Média | Médio | Normalizar vazio → '00' antes do CASE |
| 5 | **; dentro de aspas** mal parseado | Baixa | Médio | DuckDB `quote='"'` nativo |
| 6 | **VARCHAR(200) pode ser insuficiente** | Muito Baixa | Médio | Se houver linhas com >200 chars, aumentar; monitorar |
| 7 | **PSYCOPG2 buffer** OOM no lado Python | Baixa | Médio | Chunks de 50-100k linhas |

### 9.1 Limiar de Aborto
- RAM > 800MB → abortar pipeline
- Tempo parcial > 45min → abortar (garantir margem para finalização)
- Erro de encoding em > 1% das linhas → abortar

---

## 10. Critérios de Aceite

### 10.1 Gates Obrigatórios (G0-G4)
| Gate | Critério | Verificação |
|------|----------|-------------|
| **G0** | JSON válido, git clone OK, Dockerfile na raiz, build ≤15min | `docker build` exit 0 |
| **G1** | Preflight PostgreSQL: `db_empresas` existe | `pg_isready` ou `\l` |
| **G2** | Exit 0 (não 137), ≤60min, tabela existe | `docker run` exit 0 |
| **G3** | Volume na faixa 68,56M–68,70M | `SELECT COUNT(*)` |
| **G4** | 13 DQ = 0 | 13 queries SQL |

### 10.2 Meta de Score
| Métrica | Alvo | Critério |
|---------|------|----------|
| Score | **≤ 595** (realista) ou **≤ 504** (otimista) | Superar líder atual (854) |
| Tempo | **≤ 1.200s** (20 min) | Weight 60% |
| RAM | **≤ 300 MB** | Weight 25% |
| Storage | **≤ 8.783 MB** (estimativa realista) | Weight 15% |

### 10.3 Anti-Critérios (Desclassificação)
- ❌ `UNLOGGED TABLE` (perde durability)
- ❌ `pg_bulkload` (extensão não-listada)
- ❌ `fsync=off` ou `ALTER SYSTEM SET` no PostgreSQL
- ❌ Modificar `postgresql.conf`
- ❌ Exit 137 (OOM kill)
- ❌ Tempo > 60 minutos
- ❌ Volume fora da faixa 68,56M–68,70M

---

## Apêndice A: Estimativa de Storage

| Coluna | Tipo | Bytes/linha | Total (MB) |
|--------|------|------------|------------|
| cnpj_basico | VARCHAR(8) | 8 | 524 |
| razao_social | VARCHAR(80) | 40 (médio) | 2.621 |
| natureza_juridica | VARCHAR(4) | 4 | 262 |
| qualificacao_responsavel | VARCHAR(2) | 2 | 131 |
| capital_social | DOUBLE | 8 | 524 |
| porte_codigo | VARCHAR(2) | 2 | 131 |
| porte_descricao | VARCHAR(20) | 8 (médio) | 524 |
| ente_federativo | VARCHAR(100) | 0 (99,9% NULL) | ~5 |
| capital_social_faixa | VARCHAR(20) | 10 (médio) | 655 |
| is_mei | BOOLEAN | 1 | 66 |
| natureza_juridica_grupo | VARCHAR(30) | 15 (médio) | 983 |
| ente_federativo_presente | BOOLEAN | 1 | 66 |
| data_processamento | TIMESTAMP | 8 | 524 |
| Overhead PostgreSQL (tupla + page) | — | ~27 | 1.769 |
| **Total Estimado** | | **~126** | **~8.783** |

> **Nota:** Esta estimativa (~8.783 MB) é a mais realista. PostgreSQL **não comprime páginas por padrão** (apenas TOAST para colunas >2KB, que não é o caso). `fillfactor=100` reduz overhead de página, `autovacuum_enabled=false` evita I/O extra, e sem índices o storage é puramente dados + overhead HeapTupleHeader (~27 bytes/linha). O cálculo de ~126 bytes/linha × 68,6M é a melhor aproximação disponível. Ajustar score-alvo de storage para **~8.783 MB** (~321 pts) em vez de 5.000 (~183 pts).

---

## Apêndice B: Dockerfile Esqueleto

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Diretório para spill do DuckDB (filesystem regular, não tmpfs)
RUN mkdir -p /app/duckdb_temp

# Instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fonte
COPY src/ .

# Pipeline entry point
CMD ["python", "main.py"]
```

### requirements.txt
```
duckdb>=1.5
psycopg2-binary>=2.9
```
