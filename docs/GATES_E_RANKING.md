# 🚦 Gates, Ranking e Juiz Automático

Este documento define o que **aprova** ou **reprova** uma submissão (gates) e como o **ranking** é calculado entre soluções classificadas.

Scripts SQL executáveis estão em [`evaluator/judge/sql/`](../evaluator/judge/sql/). Este documento descreve apenas a **lógica** — sem queries.

---

## Visão geral do fluxo

```
Merge na main (submissions/*.json)
  → evaluator/evaluator.sh (orquestrador bash)
  → venv do juiz (psycopg2, boto3) + preflight via evaluator/judge/validar.py (Gate G1)
  → git clone + docker build (Gate G0)
  → docker run com timeout 60 min / 2 CPU / 1 GB RAM (Gate G2 — bash)
  → evaluator/judge/validar.py avaliar (Gates G2–G4 + métricas)
  → Gravação em ranking_ingestao + recalcular_posicoes_ranking()
  → E-mail ao participante (campo `email` no JSON) com indicadores
  → Site de ranking + logs do workflow
```

### Divisão de responsabilidades

| Componente | Responsabilidade |
| :--- | :--- |
| `evaluator/evaluator.sh` | Clone, build (timeout 15 min), docker run, timeout pipeline, pico de RAM |
| `evaluator/judge/validar.py` | Gates SQL, métricas, INSERT ranking |
| `evaluator/judge/sql/` | Queries compartilhadas (gates, métricas, views, site) |

---

## 1. Gates (pass/fail)

Gates são **binários**. Falhou um = submissão **não classificada** (sem posição no ranking).

### Gate G0 — Estrutura da submissão

| Check | Condição | Status em caso de falha |
| :--- | :--- | :--- |
| JSON válido | Campos `participante` e `repositorio` presentes (`email` opcional para relatório) | `ERRO_JSON_INVALIDO` |
| Repositório clonável | `git clone` com sucesso | `ERRO_CLONE_GIT` |
| Dockerfile presente | Arquivo na raiz do repo | `DOCKERFILE_AUSENTE` |
| Build Docker | `docker build` com sucesso em ≤ 15 min | `ERRO_BUILD_DOCKER` / `ERRO_BUILD_TIMEOUT` |

### Gate G1 — Preflight

Executado antes do pipeline completo para evitar horas perdidas em soluções quebradas.

| Check | Condição | Status em caso de falha |
| :--- | :--- | :--- |
| Conectividade Postgres | Consegue conectar em `db_empresas` | `ERRO_PREFLIGHT_PG` |

Comando: `python3 evaluator/judge/validar.py preflight --participante <user>`

### Gate G2 — Execução do pipeline

| Check | Condição | Status em caso de falha |
| :--- | :--- | :--- |
| Sem OOM | Exit code ≠ 137 (limite **1 GB RAM**, sem swap) | `ERRO_OOM` |
| Sem crash | Exit code = 0 | `ERRO_EXECUCAO` |
| Timeout | Wall time ≤ **60 min** (hard cap) | `ERRO_TIMEOUT` |
| Tabela existe | Tabela `public.{participante}_empresas` criada | `ERRO_TABELA_AUSENTE` |

> Limites restritivos por design: **2 CPU / 1 GB RAM / 60 min** para processar **68,6M linhas**. Exige ~19.000 linhas/s sustentadas — ver [STACK_E_LIMITES.md](./STACK_E_LIMITES.md) e o perfil em [PERFIL_DATASET.md](./PERFIL_DATASET.md).

### Gate G3 — Sanidade de volume

A carga é **completa**: a resposta correta grava **todas** as ~68,6M linhas da origem. A faixa aceita é **estreita** (`VOLUME_MIN`/`VOLUME_MAX` = 68,56M / 68,70M) — confirme o total exato com um run do profiler antes de abrir a rodada.

| Condição | Status |
| :--- | :--- |
| `total = 0` | `ERRO_TABELA_VAZIA` |
| `total < 68.560.000` | `ERRO_POUCOS_REGISTROS` |
| `total > 68.700.000` | `ERRO_REGISTROS_DEMAIS` |
| `68,56M ≤ total ≤ 68,70M` | aprovado |

Script manual: `evaluator/judge/sql/metrics/volume_sanity.sql`

### Gate G4 — Data Quality

**Treze regras** de qualidade (DQ-01 a DQ-13). Cada uma conta registros inválidos — **todas devem retornar 0**. Nesta rodada de carga completa, DQ-05 e DQ-08 **mudaram de sentido** (agora validam consistência das flags `capital_social_faixa` e `is_mei`, não mais filtro), DQ-10 foi **relaxado** (razão NOT NULL + encoding, mas permite vazio `''`), e há **três novas**: DQ-11 (`natureza_juridica_grupo`), DQ-12 (`ente_federativo_presente`) e DQ-13 (`data_processamento`). Continuam: DQ-03 (numérico), DQ-07 (consistência linha a linha) e DQ-09 (`cnpj_basico` único).

| Resultado | Status |
| :--- | :--- |
| Todas as 13 regras = 0 erros | `CLASSIFICADO` |
| Qualquer regra > 0 | `ERRO_DATA_QUALITY` |

Detalhes de qual gate falhou ficam em `gate_dq_detalhes` (JSON).  
Regras em linguagem natural: [`REGRAS_E_CONTRATO.md`](./REGRAS_E_CONTRATO.md)  
Scripts: `evaluator/judge/sql/gates/dq-*.sql` e `evaluator/judge/sql/gates/run_all_dq_manual.sql`

---

## 2. Ranking por SCORE composto (apenas se CLASSIFICADO)

O ranking **não é mais uma corrida de velocidade pura**. A posição é definida por um **score composto** (menor = melhor) que recompensa eficiência holística: tempo, RAM e storage juntos. Isso premia o **cálculo de engenharia** — de nada adianta ser 2 min mais rápido se você pega 1 GB de RAM e escreve uma tabela inchada.

### Fórmula do score

Cada termo é normalizado pelo seu orçamento/referência (fica adimensional, ~1,0 no limite):

```
score = 1000 × ( 0.60 × (tempo_segundos / 3600)          -- tempo (orçamento 60 min)
              + 0.25 × (peak_ram_mb / 1024)               -- RAM   (orçamento 1 GB)
              + 0.15 × (storage_total_mb / 4096) )         -- storage (referência 4 GB)
```

| Componente | Peso | Referência | Por quê |
| :--- | ---: | :--- | :--- |
| `tempo_segundos` | **0.60** | 3600 s | Velocidade ainda manda, mas não é tudo |
| `peak_ram_mb` | **0.25** | 1024 MB | RAM é *o* recurso escasso (1 GB) — frugalidade vale muito |
| `storage_total_mb` | **0.15** | 4096 MB | Postgres + S3; recompensa tipos compactos e sem bloat |

> Exemplo real (validado): uma solução de **1500 s / 900 MB / 3000 MB** (score ~580) **perde** para uma de **1800 s / 400 MB / 2000 MB** (score ~471) — mesmo sendo 5 min mais rápida. É o incentivo à engenharia.

> **Storage pesa mais nesta rodada.** A carga completa (~68,6M linhas) + 6 colunas derivadas gera uma tabela **muito maior** que a da rodada anterior (~25M / 8 colunas), então o termo de storage (referência 4 GB) cresce e pode passar de `1,0`. Combata o bloat: use `BOOLEAN` para as flags (`is_mei`, `ente_federativo_presente`), `VARCHAR` curto para os rótulos derivados, evite índices desnecessários e rode `VACUUM`/`ANALYZE` ao final. A referência de 4 GB (`SCORE_REF_STORAGE_MB`) foi mantida para preservar o incentivo — não é um teto.

### Métricas coletadas

| Métrica | O que mede |
| :--- | :--- |
| `score` | Score composto acima — **chave primária do ranking** |
| `tempo_segundos` | Wall time do `docker run` |
| `peak_ram_mb` | Pico de RAM do container |
| `storage_postgres_mb` / `storage_minio_mb` / `storage_total_mb` | Storage Postgres / S3 / soma |
| `total_registros` | Linhas na tabela final (exibição) |

### Ordem de classificação

1. Menor **`score`** composto
2. Desempates (raros, se score empatar): menor `tempo_segundos` → menor `storage_total_mb` → menor `peak_ram_mb`
3. Empate total: quem foi avaliado primeiro (`criado_em`)

A função `recalcular_posicoes_ranking()` calcula o `score` de cada tentativa classificada, aplica essa ordem e preenche `posicao_ranking` na **melhor tentativa** (menor score) de cada participante.

> Pesos e referências ficam na função SQL (`ranking_ingestao.sql`) e espelhados em `SCORE_*` de `evaluator/judge/config.env`. Ajuste ambos juntos se quiser recalibrar a competição.

---

## 3. Tabela `ranking_ingestao`

Resultados gravados em `db_ingestao.public.ranking_ingestao`.

DDL, função de ranking e views: `evaluator/judge/sql/schema/ranking_ingestao.sql`

### Campos principais

| Coluna | Descrição |
| :--- | :--- |
| `github_user` | Identificador do participante |
| `repositorio` | URL do repositório da solução |
| `score` | **Score composto** (chave primária do ranking; menor vence) |
| `tempo_segundos` | Wall time do pipeline |
| `storage_postgres_mb` / `storage_minio_mb` | Storage Postgres / S3 (coluna `storage_minio_mb` mede o prefixo no MinIO da avaliação) |
| `peak_ram_mb` | Pico de RAM |
| `total_registros` | Linhas na tabela final |
| `status` | Resultado final (ver abaixo) |
| `classificado` | `true` só se passou em todos os gates |
| `posicao_ranking` | Posição no leaderboard |
| `gate_preflight` … `gate_dq` | Flags de cada gate |
| `gate_dq_detalhes` | JSON com gates DQ que falharam |
| `commit_sha` / `pr_numero` | Rastreabilidade da submissão |
| `criado_em` | Data/hora da avaliação |

### Status possíveis

| Status | Significado |
| :--- | :--- |
| `CLASSIFICADO` | Passou em todos os gates |
| `ERRO_CLONE_GIT` | Falha ao clonar repositório |
| `DOCKERFILE_AUSENTE` | Sem Dockerfile na raiz |
| `ERRO_BUILD_TIMEOUT` | Build da imagem excedeu 15 minutos |
| `ERRO_BUILD_DOCKER` | Build da imagem falhou |
| `ERRO_PREFLIGHT_*` | Falha no preflight |
| `ERRO_OOM` | Container morto por falta de memória |
| `ERRO_TIMEOUT` | Excedeu o timeout do pipeline (60 min) |
| `ERRO_EXECUCAO` | Container saiu com erro |
| `ERRO_TABELA_AUSENTE` | Tabela final não encontrada |
| `ERRO_TABELA_VAZIA` | Tabela sem registros |
| `ERRO_POUCOS_REGISTROS` | Abaixo do mínimo esperado |
| `ERRO_REGISTROS_DEMAIS` | Acima do máximo esperado |
| `ERRO_DATA_QUALITY` | Uma ou mais regras DQ falharam |

---

## 4. Views para o site de ranking

`storage_total_mb` **não é coluna da tabela** — as views calculam `storage_postgres_mb + storage_minio_mb`.

| View | Uso no site |
| :--- | :--- |
| `v_leaderboard` | Ranking oficial (página principal) |
| `v_melhor_por_participante` | Melhor tentativa classificada de cada user |
| `v_ultima_avaliacao` | Última tentativa — feedback no ranking |
| `v_historico_tentativas` | Todas as execuções — gráficos e debug |

Consultas prontas para rodar no servidor: `evaluator/judge/sql/site/consultas_ranking.sql`

---

## 5. Regras operacionais da fila

| Regra | Comportamento |
| :--- | :--- |
| Fila única | 1 container de avaliação por vez (nunca em paralelo — `concurrency: avaliador-ingestao`) |
| Intervalo entre avaliações | Cooldown de 15 min após cada run (`COOLDOWN_SEC`) antes de liberar a fila |
| Reavaliação | Novo merge do JSON ou `workflow_dispatch` manual — execuções enfileiradas, não canceladas |
| Timeout global | **60 min** no `docker run` (hard cap; ~68,6M linhas a processar em 1 GB RAM) |
| Build global | 15 minutos no `docker build` (1 CPU / 1 GB — não compete com o pipeline) |
| Feedback | Logs do workflow + e-mail do participante (campo `email` no JSON) + site de ranking (`v_ultima_avaliacao`) |
| Melhor resultado | Cada execução gera um registro; o site usa a melhor classificada |

---

## 6. O que **não** entra no ranking

* Linhas de código
* Número de dependências
* Elegância subjetiva do código
* Erros DQ parciais — ou passa com 0 em todas, ou reprova

Data Quality é **gate**, não pontuação parcial.
