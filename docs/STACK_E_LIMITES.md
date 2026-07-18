# 💻 Stack do Servidor e Acesso na Avaliação

O seu código rodará em uma máquina real com recursos contados. Na avaliação oficial (após merge na `main`), o servidor **injeta variáveis de ambiente** no seu container — você **não precisa** (e não deve) hardcodar senhas ou IPs no código.

Esta competição simula um cenário real: a infraestrutura é fornecida, mas **a arquitetura do pipeline é decisão sua**.

---

## 🔄 O que acontece após o merge na `main`

1. Você abre um PR no fork com `submissions/seu_usuario.json` e **faz merge** na `main` do repo oficial.
2. O GitHub Action dispara `evaluator/evaluator.sh` no servidor (**Hardware Celeron**).
3. O avaliador clona o **seu repositório** (campo `repositorio` do JSON).
4. Faz `docker build` da **sua imagem** e executa com limites **restritivos** de 2 CPU / **1 GB RAM**.
5. Seu container entra na **mesma rede Docker** do Postgres e do object storage S3 (MinIO no laboratório).
6. Os dados brutos ficam montados em **`/data/`** (somente leitura).
7. Ao terminar, o **juiz** valida a tabela `public.{participante}_empresas` e grava o ranking.

Você **não acessa o servidor por SSH**. A avaliação roda automaticamente após o merge. O organizador pode **reavaliar** sem novo PR em **Actions → Avaliador de Submissoes → Run workflow**.

---

## 🔑 Variáveis de ambiente (injetadas no seu container)

O avaliador define estas variáveis no `docker run`. **Seu código deve lê-las** — não hardcode hosts ou credenciais.

| Variável | Descrição | Valor na avaliação (servidor) |
| :--- | :--- | :--- |
| `PARTICIPANTE` | Seu identificador do JSON | ex.: `renan_python` |
| `PG_TABLE` | Nome da tabela final | ex.: `renan_python_empresas` |
| `PG_HOST` | Host do Postgres na rede Docker | `postgres_db` |
| `PG_PORT` | Porta Postgres | `5432` |
| `PG_USER` | Usuário Postgres | `homelab_postgres` |
| `PG_PASSWORD` | Senha Postgres | *(injetada pelo servidor)* |
| `PG_DB` | Banco de destino | `db_empresas` |
| `S3_ENDPOINT` | Endpoint S3-compatível (MinIO na avaliação) | `http://minio:9000` |
| `AWS_ACCESS_KEY_ID` | Credencial S3 | `admin` |
| `AWS_SECRET_ACCESS_KEY` | Credencial S3 | `minio_password` |
| `MINIO_BUCKET` | Bucket S3 | `marketing-leads` |

### Regra de ouro

> Dentro do container na avaliação, use **`postgres_db`** e **`minio`** como hosts — **não** use `localhost` para Postgres ou S3.  
> `localhost` dentro do container aponta para o próprio container, não para o servidor.

### Tabela e path de saída

| Item | Como obter no código |
| :--- | :--- |
| Tabela Postgres | `public.{PG_TABLE}` ou `public.{PARTICIPANTE}_empresas` |
| Prefixo S3 (opcional) | `s3://{MINIO_BUCKET}/{PARTICIPANTE}/` |

---

## 🗄️ PostgreSQL (destino obrigatório)

| Item | Na avaliação (dentro do container) | No seu PC (dev local) |
| :--- | :--- | :--- |
| Host | `postgres_db` (via `PG_HOST`) | `localhost` ou IP da LAN |
| Porta | `5432` | `5432` |
| Banco | `db_empresas` | `db_empresas` |
| Schema | `public` | `public` |
| Tabela final | `{participante}_empresas` | idem |
| Usuário | `homelab_postgres` | idem |

O banco `db_ingestao` / tabela `ranking_ingestao` é **interno da competição** — não grave seus dados de negócio lá.

---

## 📦 Object storage S3-compatível (opcional)

O desafio expõe um backend **compatível com a API S3**. Na avaliação oficial, essa implementação é o **MinIO dockerizado** — apenas como **alvo de laboratório/benchmark**, não como sugestão de stack de produção.

| Item | Na avaliação (dentro do container) | No seu PC (dev local) |
| :--- | :--- | :--- |
| Endpoint | `http://minio:9000` (via `S3_ENDPOINT`) | `http://localhost:9000` |
| Bucket | `marketing-leads` | idem |
| Prefixo | `{participante}/` | idem |
| Credenciais | via `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | `admin` / `minio_password` |

Object storage S3 é **opcional**. Se usar, limite-se ao seu prefixo — conta para o ranking de storage.

### Abstraia o backend S3 no seu código

Projete o pipeline para falar via **API S3 genérica** (boto3, aws-sdk, etc.), lendo `S3_ENDPOINT` e credenciais de variáveis de ambiente. Assim, o MinIO do desafio é só uma implementação de referência; em produção você troca o endpoint sem reescrever a lógica de ingestão.

### Licença e uso permitido do MinIO

O MinIO mudou de modelo nos últimos anos. Pontos relevantes para este desafio:

| Tema | O que importa aqui |
| :--- | :--- |
| **GNU AGPLv3** | Servidor e gateway MinIO são AGPLv3 desde 2021. Usar MinIO **sem modificações** como serviço interno de CI/laboratório **não força** AGPL no seu código de ingestão — apenas programas que derivam ou redistribuem o MinIO modificado. |
| **MinIO Software License** | Binários recentes restringem uso sem contrato enterprise a **uma instância, ambiente não produtivo, avaliação interna**. O padrão deste repositório (MinIO efêmero no runner, prefixo por participante, sem oferta pública multi-tenant) enquadra-se nesse uso de laboratório. |
| **Edição Community** | A distribuição upstream prioriza código-fonte; funcionalidades avançadas de administração migraram para CLI (`mc admin`) ou edição paga (AIStor). Para o desafio isso é aceitável; para produção, avalie risco operacional. |

**Não** trate o MinIO deste desafio como plataforma de object storage oficial para terceiros. Documente no seu repositório que, em produção, cada time escolhe sua solução S3-compatível e avalia juridicamente o uso.

Links oficiais: [repositório MinIO](https://github.com/minio/minio) · [licença AGPLv3](https://github.com/minio/minio/blob/master/LICENSE) · [MinIO Software License](https://docs.min.io/license/)

### Alternativas S3-compatíveis (produção e replicação do desafio)

Se você for replicar o desafio ou levar o pipeline a produção, considere backends que falam a mesma API S3:

| Opção | Observação |
| :--- | :--- |
| **AWS S3** (ou equivalentes em nuvem) | Padrão de mercado; sem MinIO no caminho |
| **Ceph RADOS Gateway** | Object storage open source com API S3 |
| **SeaweedFS** | Leve, S3-compatível, bom para ambientes enxutos |
| **MinIO local (docker-compose)** | Válido para **dev/benchmark individual**; fixe uma **tag de release** (evite `latest`) |

### Organizadores: fixar versão do MinIO

Ao subir a infra do servidor de avaliação, **fixe uma tag de release** do MinIO no `docker-compose` ou `docker run` (ex.: `minio/minio:RELEASE.2024-...`), não `latest`. Mudanças de licença e de interface entre versões podem quebrar expectativas de uma edição para outra.

---

## 📂 Dados brutos (`/data/`)

| Item | Valor |
| :--- | :--- |
| Caminho no container | `/data/` |
| Conteúdo | Arquivos `.zip` com CSVs empresariais |
| Montagem | Feita pelo avaliador (`DATA_VOLUME` no servidor) |
| Permissão | Somente leitura (`:ro`) |

Seu pipeline deve ler os zips **diretamente** de `/data/`, sem depender de download ou extração manual no host.

---

## 🧪 Desenvolvimento local vs avaliação no servidor

| Aspecto | Local (seu PC) | Avaliação (após merge na `main`) |
| :--- | :--- | :--- |
| Quem roda o Docker | Você | `evaluator/evaluator.sh` |
| `PG_HOST` | `localhost` | `postgres_db` |
| `S3_ENDPOINT` | `http://localhost:9000` | `http://minio:9000` |
| `/data/` | Você monta com `-v` | Montado automaticamente |
| Limites CPU/RAM | Opcional | **2 CPU / 1 GB** (obrigatório) |
| Timeout | Opcional | **60 min** (hard cap para ~68,6M linhas) |

### Exemplo: ler variáveis no código (Python)

```python
import os

PARTICIPANTE = os.environ["PARTICIPANTE"]
PG_TABLE = os.environ.get("PG_TABLE", f"{PARTICIPANTE}_empresas")

pg_dsn = (
    f"postgresql://{os.environ['PG_USER']}:{os.environ['PG_PASSWORD']}"
    f"@{os.environ.get('PG_HOST', 'postgres_db')}:{os.environ.get('PG_PORT', '5432')}"
    f"/{os.environ.get('PG_DB', 'db_empresas')}"
)

s3_endpoint = os.environ.get("S3_ENDPOINT", "http://minio:9000")
s3_bucket = os.environ.get("MINIO_BUCKET", "marketing-leads")
s3_prefix = f"{PARTICIPANTE}/"
```

### Exemplo: testar localmente com os mesmos limites do servidor

```bash
docker run --rm \
  --cpus="2.0" --memory="1g" --memory-swap="1g" \
  --network homelab_net \
  -v /caminho/para/zips:/data:ro \
  -e PARTICIPANTE=renan_python \
  -e PG_TABLE=renan_python_empresas \
  -e PG_HOST=postgres_db \
  -e PG_PORT=5432 \
  -e PG_USER=homelab_postgres \
  -e PG_PASSWORD='sua_senha' \
  -e PG_DB=db_empresas \
  -e S3_ENDPOINT=http://minio:9000 \
  -e AWS_ACCESS_KEY_ID=admin \
  -e AWS_SECRET_ACCESS_KEY=minio_password \
  -e MINIO_BUCKET=marketing-leads \
  sua-imagem
```

Substitua `homelab_net` pela rede Docker onde `postgres_db` e `minio` estão rodando.

---

## ⚙️ Limites de hardware (restritivos por design)

Esta é uma competição **"no limite"**: os recursos são **propositalmente apertados** para que a arquitetura do pipeline importe mais que a força bruta. Não há folga sobrando — cabe a você fazer o **cálculo de engenharia** (ver abaixo).

| Recurso | Limite | Antes |
| :--- | :--- | :--- |
| Memória RAM | **1 GB** (sem swap — `--memory-swap=1g`) | ~~2 GB~~ |
| vCPUs | **2** | 2 |
| Timeout | **60 min** (3600 s — hard cap, ver estimativa abaixo) | ~~~3h20m~~ |
| Build da imagem | **15 min** máx., 1 CPU / 1 GB RAM (não conta no tempo do pipeline) | igual |

> **1 GB de RAM para ~5 GB de dados descompactados.** É fisicamente impossível carregar o dataset em memória — o pipeline **obriga** streaming/batch. Se estourar RAM, o container morre com **exit code 137** (OOM) e a submissão é desclassificada.

O orquestrador (`evaluator/evaluator.sh` + juiz) é leve: o build usa no máximo 1 CPU / 1 GB; o pipeline do participante recebe 2 CPU / **1 GB** completos.

Os limites são configuráveis no servidor via `PIPELINE_CPU_LIMIT`, `PIPELINE_MEM_LIMIT` e `PIPELINE_TIMEOUT_SEC` em `evaluator/judge/config.env`.

---

## 📦 Perfil do dataset oficial (medido)

Números **reais**, medidos por `evaluator/scripts/profile_empresas.py`. Perfil completo (limpeza, distribuições, por arquivo) em [`PERFIL_DATASET.md`](./PERFIL_DATASET.md).

| Item | Valor |
| :--- | :--- |
| Arquivos `.zip` em `/data/` | **10** |
| Tamanho comprimido (total) | **~1,26 GB** |
| Total descompactado (CSV) | **~5,0 GB** (razão de compressão **4,0x**) |
| **Arquivo 1** (`Empresas0.zip`) — linhas | **28.175.408** (~2,1 GB descompactado) |
| **Arquivos 2–10** — linhas cada | **4.494.860** cada (~40,5M linhas no total) |
| **Total de linhas a processar** | **68.629.148** |
| Colunas no CSV de origem | **7** |
| Colunas derivadas (regras de negócio) | **+6** (`porte_descricao`, `capital_social_faixa`, `is_mei`, `natureza_juridica_grupo`, `ente_federativo_presente`, `data_processamento`) |
| Registros finais esperados | **~68.629.148** (carga completa, sem filtro — faixa apertada `VOLUME_MIN`/`VOLUME_MAX` = 68,56M / 68,70M) |

> Carga completa: você **lê e grava** todas as ~68,6M linhas. O orçamento de tempo incide sobre ler, transformar (derivar 6 colunas) e gravar o volume inteiro — sem o alívio de descartar linhas no filtro.

---

## ⏱️ Orçamento de tempo e o cálculo de engenharia

O timeout **não é dimensionado com folga**. É um **orçamento fixo de 60 min** (hard cap). O gargalo real não é o tamanho em GB — é **quantas linhas o pipeline precisa ler, transformar (7→13 colunas) e gravar** dentro de 1 GB de RAM. Sem filtro, você grava **todas** as linhas: não há alívio de descarte na carga.

### O cálculo que você precisa fazer antes de submeter

```
throughput_exigido = total_linhas / orçamento
                   = 68.629.148 / 3600 s
                   ≈ 19.000 linhas/s sustentadas (ler + transformar + gravar TODAS)
```

Se o seu design não sustenta **~19.000 linhas/s de ponta a ponta** em 2 CPU / 1 GB RAM, ele **não termina no prazo** — e é reprovado por `ERRO_TIMEOUT`. Esse é o incentivo: medir antes, não depois.

Valores reais no servidor (`evaluator/judge/config.env`):

| Variável | Valor | Significado |
| :--- | :--- | :--- |
| `DATA_LINES_FILE1` | `28175408` | Linhas do primeiro `.zip` (`Empresas0.zip`) |
| `DATA_LINES_OTHERS_EACH` | `4494860` | Linhas de cada um dos outros 9 arquivos |
| `DATA_FILES_OTHERS` | `9` | Quantidade de arquivos menores |
| `PIPELINE_MEM_LIMIT` | `1g` | RAM do container (sem swap) |
| `PIPELINE_CPU_LIMIT` | `2.0` | vCPUs do container |
| `PIPELINE_TIMEOUT_SEC` | `3600` | Orçamento fixo: **60 min** |

### Referência rápida — throughput exigido por orçamento

Quanto mais apertado o orçamento, maior o throughput mínimo. É o número que separa um pipeline em streaming bem feito de um script ingênuo.

| Orçamento | Throughput exigido (68,6M linhas) | Leitura |
| :--- | :--- | :--- |
| 30 min | **~38.100 linhas/s** | só engines vetorizadas/nativas |
| 45 min | **~25.400 linhas/s** | otimização séria |
| **60 min** | **~19.100 linhas/s** | **limite oficial** — streaming eficiente obrigatório |
| 90 min | ~12.700 linhas/s | (folgado — não é o cap) |
| 120 min | ~9.500 linhas/s | (folgado — não é o cap) |

### O que consome tempo no pipeline

| Etapa | Impacto |
| :--- | :--- |
| Leitura dos 10 `.zip` em streaming | I/O + descompressão (4,0x) |
| Parse CSV (`;`, ISO-8859-1, aspas com `;` embutido) | CPU por linha |
| Derivação das 6 colunas de negócio (faixa, flags, grupo, carimbo) | CPU por linha |
| Classificação/sinalização (faixa de capital, `is_mei`, grupo, ente) | CPU por linha — sem descarte |
| Carga no Postgres (`COPY`/batch) | I/O de rede Docker + disco (**todas** as ~68,6M linhas) |

Para recalcular: `source evaluator/scripts/lib/estimate-timeout.sh && print_timeout_estimate`

### Servidor de avaliação (organizadores)

| Recurso | Recomendação |
| :--- | :--- |
| RAM do host | **≥ 5 GB** (1 GB participante + Postgres + S3/MinIO + SO) |
| Avaliações simultâneas | **1** (fila única) |
| Build vs pipeline | Tempos separados — build limitado a 15 min / 1 CPU |

💡 *Dica:* com 1 GB de RAM e 60 min, força bruta não passa. Use engines de streaming/vetorizadas (Polars, DuckDB, PyArrow, Rust ou Go) e `COPY` em lotes.

---

## 🧠 Estratégias arquiteturais

| Abordagem | Quando faz sentido |
| :--- | :--- |
| ZIP → Postgres direto | Simplicidade, BI plug-and-play |
| ZIP → Parquet (S3) → Postgres | Grandes volumes, batches controlados |
| ZIP → DuckDB/Polars → ambos | Uma engine, múltiplos sinks |
| Apenas object storage S3 | **Não classifica** — Postgres é obrigatório |

---

## ⏳ Fila e avaliação

* **1 avaliação por vez** no servidor
* Submissões entram na fila após merge (`push` na `main`) ou disparo manual (`workflow_dispatch`)
* Resultado gravado em `ranking_ingestao` e visível no site de ranking

---

## 🛠️ Referência interna (organizadores)

Configuração do servidor em `evaluator/judge/config.env`:

| Variável | Função |
| :--- | :--- |
| `DOCKER_NETWORK` | Rede compartilhada entre container do participante, Postgres e S3 (MinIO) |
| `DATA_VOLUME` | Volume montado em `/data/` (ex.: `/path/zips:/data:ro`) |
| `PG_CONTAINER` | Nome do container Postgres (`postgres_db`) |

Participantes **não** editam `evaluator/judge/config.env` — isso é só no servidor de avaliação.
