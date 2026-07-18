# 📄 Regras de Negócio e Contrato de Dados

Para sua submissão ser **classificada**, a tabela final no PostgreSQL deve cumprir rigorosamente o schema abaixo e **zerar todas as métricas de erro** do Juiz Automático.

As queries executadas pelo juiz estão em [`evaluator/judge/sql/gates/`](../evaluator/judge/sql/gates/) e [`evaluator/judge/sql/metrics/`](../evaluator/judge/sql/metrics/).

---

## 1. Origem dos Dados

* Diretório de dados brutos no container: `/data/`
* **10 arquivos** compactados (`.zip`) — `Empresas0.zip` … `Empresas9.zip` (~1,26 GB comprimidos, ~5,0 GB descompactados)
* Um arquivo interno `.EMPRECSV` por `.zip` (ex: `K3241.K03200Y1.D60613.EMPRECSV`)
* Codificação original: `ISO-8859-1` (Latin-1) ➔ deve ser convertido para `UTF-8`
* Separador: `;` (ponto e vírgula) com aspas duplas `"`. **Sem cabeçalho**
* **68.629.148 linhas** no total (1 arquivo de ~28,2M + 9 de ~4,5M)

> ⚠️ **Cuidado com parsing ingênuo.** O perfil medido em [`PERFIL_DATASET.md`](./PERFIL_DATASET.md) mostra **323 linhas com `;` dentro de campos entre aspas** e **6.933 linhas com bytes não-ASCII**. Um `split(";")` cru **quebra** essas linhas, e ler como UTF-8/ASCII **corrompe** acentos. Use um parser CSV que respeite aspas + decodifique `ISO-8859-1`.

---

## 2. Destino Final (Obrigatório)

| Item | Valor |
| :--- | :--- |
| Banco | `db_empresas` |
| Schema | `public` |
| Tabela | `{participante}_empresas` |
| Exemplo | participante `renan_python` → `public.renan_python_empresas` |
| Hífen no ID | Permitido (ex.: `dataforma-hub`). Ao criar a tabela no SQL/client, use identificador entre aspas: `public."dataforma-hub_empresas"` — senão o Postgres interpreta `-` como minus. |

A tabela deve existir e estar populada ao final da execução do container.

### Uso opcional de object storage S3-compatível

Você pode usar storage S3-compatível (na avaliação: MinIO dockerizado como alvo de laboratório) livremente para staging ou formatos intermediários (Parquet, Delta Lake, Iceberg), desde que:

* Use apenas o prefixo `s3://marketing-leads/{participante}/`
* A tabela final em Postgres permaneça a **fonte de verdade** para validação e BI
* Projete o código contra a **API S3 genérica** — não acople à marca MinIO; em produção, escolha o backend S3 que fizer sentido para o seu contexto (ver [licença e alternativas](./STACK_E_LIMITES.md#-object-storage-s3-compatível-opcional))

---

## 3. Schema da Tabela Final

A tabela final tem **13 colunas**: as **7 de origem** (transformadas) + **6 derivadas** (`porte_descricao` e as 5 colunas de negócio desta rodada). **Não há filtro** — todas as linhas são carregadas (ELT: carregue tudo, segmente no BI pelas colunas/flags).

Este documento é a **fonte de verdade** dos rótulos e faixas: os gates comparam **byte a byte** (UPPER com acento). Ao mudar um rótulo aqui, atualize também `evaluator/judge/sql/gates/dq-*.sql`, `run_all_dq_manual.sql`, `sql/dev/seed_participante.sql` e `evaluator/scripts/profile_empresas.py`.

| Coluna | Tipo Postgres | Regra de Transformação |
| :--- | :--- | :--- |
| `cnpj_basico` | `VARCHAR(8)` | Exatamente 8 dígitos numéricos com zeros à esquerda |
| `razao_social` | `VARCHAR` | Uppercase, sem espaços nas extremidades; **NOT NULL** (string vazia `''` é permitida) |
| `natureza_juridica` | `VARCHAR(4)` | Código numérico de 4 dígitos |
| `qualificacao_responsavel` | `VARCHAR` | Código de qualificação (NOT NULL) |
| `capital_social` | `DOUBLE PRECISION` | Vírgula BR → ponto (`5000.00`) |
| `porte_codigo` | `VARCHAR(2)` | `"00"`, `"01"`, `"03"` ou `"05"` |
| `porte_descricao` | `VARCHAR` | Mapeamento: `00`→`NÃO INFORMADO`, `01`→`MICRO EMPRESA`, `03`→`EMPRESA DE PEQUENO PORTE`, `05`→`DEMAIS` |
| `ente_federativo` | `VARCHAR` | Strings vazias `""` → `NULL` |
| `capital_social_faixa` | `VARCHAR` | Faixa derivada de `capital_social` (tabela abaixo) |
| `is_mei` | `BOOLEAN` | `true` quando `razao_social` termina em 11 dígitos (heurística de CPF de titular de MEI) |
| `natureza_juridica_grupo` | `VARCHAR` | Grupo do 1º dígito de `natureza_juridica` (tabela abaixo) |
| `ente_federativo_presente` | `BOOLEAN` | `true` quando `ente_federativo` está preenchido (não nulo e não vazio) |
| `data_processamento` | `TIMESTAMP` | Carimbo de ingestão (linhagem); **NOT NULL** |

**Faixa de `capital_social_faixa`** (fronteiras superiores inclusivas):

| Condição sobre `capital_social` | `capital_social_faixa` |
| :--- | :--- |
| `= 0` | `SEM CAPITAL` |
| `> 0` e `<= 1000` | `ATÉ 1K` |
| `> 1000` e `<= 10000` | `1K A 10K` |
| `> 10000` e `<= 100000` | `10K A 100K` |
| `> 100000` e `<= 1000000` | `100K A 1M` |
| `> 1000000` | `ACIMA DE 1M` |

**Grupo de `natureza_juridica_grupo`** (1º dígito de `natureza_juridica`, padrão CONCLA/IBGE):

| 1º dígito | `natureza_juridica_grupo` |
| :--- | :--- |
| `1` | `ADMINISTRAÇÃO PÚBLICA` |
| `2` | `ENTIDADES EMPRESARIAIS` |
| `3` | `ENTIDADES SEM FINS LUCRATIVOS` |
| `4` | `PESSOAS FÍSICAS` |
| `5` | `ORGANIZAÇÕES INTERNACIONAIS` |
| outro | `OUTROS` |

> **Achados reais que exigem tratamento** (ver [`PERFIL_DATASET.md`](./PERFIL_DATASET.md)):
> - `porte_codigo`: distribuição real é `01` (75,4%), `05` (21,6%), `03` (3,0%) e **4.063 valores vazios** (`""`). O código `00` **não aparece** no dataset, mas é válido. Normalize o vazio (ex.: `""` → `00`/`NÃO INFORMADO`) para não reprovar em **DQ-06/07**.
> - `capital_social`: **100%** usam vírgula decimal BR (ex.: `5000,00`); ~25,6% valem `0,00` → classifique como `SEM CAPITAL`.
> - `ente_federativo`: **99,9%** vazios → viram `NULL` (e `ente_federativo_presente = false`).
> - `razao_social`: casos de espaços nas extremidades, caixa baixa e **1 valor vazio** — trate encoding/caixa/espaços, mas mantenha o vazio como `''` (**não** use `NULL`).

---

## 4. Data Quality (Gates)

Todas as **13 regras** abaixo devem ter **0 erros**. Qualquer valor acima de zero reprova a submissão — não há pontuação parcial.

| Gate | Regra | Tolerância |
| :--- | :--- | :--- |
| DQ-01 | `cnpj_basico` com exatamente 8 dígitos numéricos | **0** |
| DQ-02 | `razao_social` em UPPER e sem espaços nas extremidades | **0** |
| DQ-03 | `natureza_juridica` com exatamente 4 dígitos **numéricos** (`^[0-9]{4}$`) | **0** |
| DQ-04 | `qualificacao_responsavel` preenchido (NOT NULL **e não vazio**) | **0** |
| DQ-05 | `capital_social_faixa` **exatamente igual** à faixa derivada de `capital_social` (consistência linha a linha) | **0** |
| DQ-06 | `porte_codigo` em `00`, `01`, `03` ou `05` | **0** |
| DQ-07 | `porte_descricao` **exatamente igual** ao mapeamento de `porte_codigo` (consistência linha a linha) | **0** |
| DQ-08 | `is_mei` **consistente**: `true` sse, e somente se, `razao_social` termina em 11 dígitos | **0** |
| DQ-09 | `cnpj_basico` **único** na tabela (`COUNT(*) = COUNT(DISTINCT cnpj_basico)`) | **0** |
| DQ-10 | `razao_social` **NOT NULL** e com **encoding correto** (sem `U+FFFD` nem bytes de controle); vazio `''` é permitido | **0** |
| DQ-11 | `natureza_juridica_grupo` **exatamente igual** ao grupo do 1º dígito de `natureza_juridica` (consistência linha a linha) | **0** |
| DQ-12 | `ente_federativo_presente` **consistente** com `ente_federativo` preenchido (consistência linha a linha) | **0** |
| DQ-13 | `data_processamento` preenchido (NOT NULL) | **0** |

**Novidades desta rodada (carga completa + colunas de negócio):**

- **Sem filtro:** a tabela final carrega **todas** as linhas (~68,6M). Os antigos filtros viraram flags — `DQ-05` valida a **faixa** de capital (não mais `> 1000`) e `DQ-08` valida o **flag `is_mei`** (não mais a remoção de MEIs).
- **DQ-10 relaxado:** exige encoding correto e `razao_social` NOT NULL, mas **permite** `razao_social` vazia (`''`) — há 1 registro assim na origem.
- **Novos gates de consistência das colunas derivadas:** `DQ-11` (`natureza_juridica_grupo`), `DQ-12` (`ente_federativo_presente`) e `DQ-13` (`data_processamento`).
- **Mantidos:** DQ-01/02/03/04/06/07/09 (DQ-03 numérico, DQ-07 consistência linha a linha, DQ-09 `cnpj_basico` único).

Arquivos SQL por gate: `evaluator/judge/sql/gates/dq-01_*.sql` … `dq-13_*.sql`  
Validação manual de todos: `evaluator/judge/sql/gates/run_all_dq_manual.sql`

---

## 5. Carga Completa (sem filtro)

Nesta rodada **não há filtro de negócio**: a tabela final recebe **todas as ~68,6M linhas** da origem. A lógica antiga de descarte virou **sinalização** em colunas derivadas — você carrega tudo e segmenta no BI:

- **Capital:** em vez de remover `capital_social ≤ 1000`, classifique cada linha em `capital_social_faixa` (`SEM CAPITAL` … `ACIMA DE 1M`).
- **MEI/CPF:** em vez de remover `razao_social` terminada em 11 dígitos, marque `is_mei = true`.
- **Órgão público (B2G):** marque `ente_federativo_presente = true` quando `ente_federativo` estiver preenchido.
- **Natureza jurídica:** agrupe pelo 1º dígito em `natureza_juridica_grupo`.

Isto é uma escolha **ELT** (carregue o dado bruto, derive segmentações para conveniência de BI). O custo é **denormalização**: as colunas derivadas incham a tabela e pesam mais no score de **storage** — otimize tipos (ex.: `BOOLEAN` em vez de texto, `VARCHAR` curto) e evite índices desnecessários.

**Distribuição de referência** (68,6M linhas de entrada; informativo, **nada é removido**):

| Sinal | Linhas | % do total |
| :--- | ---: | ---: |
| `capital_social ≤ 1000` (inclui 17,5M com `0,00`) | 33.716.612 | 49,1% |
| `razao_social` termina em 11 dígitos → `is_mei = true` | 18.907.005 | 27,5% |
| **B2B-qualificadas** (capital > 1000 **e** não-MEI) | **25.031.418** | **36,5%** |

---

## 6. Sanidade de Volume

A carga é **completa**: uma solução correta grava **todas** as linhas da origem (~68,6M). A faixa aceita é **estreita** em torno do total — confirme o valor exato com um run do profiler (`evaluator/scripts/profile_empresas.py`) antes de abrir a rodada.

| Situação | Faixa | Status |
| :--- | :--- | :--- |
| Zero registros | `total = 0` | `ERRO_TABELA_VAZIA` |
| Abaixo do mínimo | `total < 68.560.000` | `ERRO_POUCOS_REGISTROS` |
| Acima do máximo | `total > 68.700.000` | `ERRO_REGISTROS_DEMAIS` |
| Dentro da faixa | `68,56M ≤ total ≤ 68,70M` | aprovado |

Os limites exatos (`limite_min` = `VOLUME_MIN`, `limite_max` = `VOLUME_MAX`) ficam em `evaluator/judge/config.env` e em `evaluator/judge/sql/metrics/volume_sanity.sql`. Erros comuns que a faixa apertada pega:

| Erro de pipeline | Total resultante | Status |
| :--- | ---: | :--- |
| Aplicou algum filtro antigo (capital / MEI) | < 68,56M | `ERRO_POUCOS_REGISTROS` |
| Perdeu o arquivo grande (`Empresas0`) | ~40M | `ERRO_POUCOS_REGISTROS` |
| Parser ingênuo perde linhas com `;` em aspas | < 68,56M | `ERRO_POUCOS_REGISTROS` |
| Carga dupla / sem dedup | > 68,70M | `ERRO_REGISTROS_DEMAIS` (+ DQ-09) |
| Cabeçalho/linha em branco contada como registro | 68,6M + N | pode cair fora da faixa |
