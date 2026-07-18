# 📑 Checklist para Envio de Pull Request

Antes de abrir o Pull Request e fazer **merge** na `main` (o que dispara a avaliação), verifique todos os pontos abaixo:

## Estrutura e execução

- [ ] O código lê os arquivos `.zip` em `/data/` sem extração manual prévia no SO.
- [ ] O `Dockerfile` está na raiz do repositório e instala todas as dependências.
- [ ] O container inicia e executa o pipeline automaticamente (`CMD`/`ENTRYPOINT`).
- [ ] O container finaliza dentro do limite de **1 GB de RAM** (sem OOM — dataset ~5 GB não cabe em memória, exige streaming).
- [ ] O pipeline completa em menos de **60 min** (dataset oficial: 10 zips, ~68,6M linhas → exige ~19k linhas/s).

## Contrato de dados

- [ ] A tabela final existe em `db_empresas.public.{participante}_empresas`.
- [ ] O nome da tabela usa exatamente o campo `participante` do JSON + sufixo `_empresas`.
- [ ] **Carga completa:** todas as ~68,6M linhas foram carregadas — **nenhum filtro** de capital ou MEI.
- [ ] Encoding convertido de `ISO-8859-1` para `UTF-8` (sem `�`/U+FFFD nem lixo — **DQ-10**).
- [ ] `capital_social` com vírgula BR convertida para ponto decimal.
- [ ] `capital_social_faixa` derivada e **exatamente** consistente com `capital_social` (**DQ-05**).
- [ ] `is_mei = true` quando `razao_social` termina em 11 dígitos (flag, **não** remoção — **DQ-08**).
- [ ] `natureza_juridica_grupo` derivada do 1º dígito de `natureza_juridica` (**DQ-11**).
- [ ] `ente_federativo_presente` reflete `ente_federativo` preenchido (**DQ-12**).
- [ ] `data_processamento` preenchido em toda linha (**DQ-13**).
- [ ] `cnpj_basico` **único** na tabela final (sem carga dupla — **DQ-09**).
- [ ] `porte_descricao` **exatamente** consistente com `porte_codigo` linha a linha (**DQ-07**).
- [ ] `razao_social` **NOT NULL** (string vazia `''` é permitida — **não** use `NULL`); `natureza_juridica` com 4 dígitos numéricos (**DQ-03/DQ-10**).
- [ ] Total final na faixa apertada **68,56M – 68,70M** (esperado: todas as linhas, ~68.629.148).
- [ ] Schema e tipos conforme [REGRAS_E_CONTRATO.md](./REGRAS_E_CONTRATO.md) — **13 gates DQ** devem retornar 0.

## Infraestrutura e variáveis de ambiente

- [ ] O código lê **variáveis de ambiente** (`PG_HOST`, `PG_USER`, `PG_PASSWORD`, `PG_DB`, `PARTICIPANTE`, `PG_TABLE`) — sem hardcode de host/senha.
- [ ] Na avaliação, Postgres é acessado via `PG_HOST=postgres_db` (não `localhost`).
- [ ] Dados lidos de `/data/` (zips montados pelo avaliador).
- [ ] Tabela gravada em `public.{PG_TABLE}` no banco `db_empresas`.

## Object storage S3 (opcional)

- [ ] O código fala **API S3 genérica** via `S3_ENDPOINT` — MinIO na avaliação é só alvo de laboratório.
- [ ] Endpoint lido de `S3_ENDPOINT` (`http://minio:9000` na avaliação).
- [ ] Credenciais lidas de `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
- [ ] Arquivos apenas no prefixo `s3://marketing-leads/{PARTICIPANTE}/`.
- [ ] Para produção ou fork do desafio: escolha backend S3-compatível próprio (AWS S3, Ceph, SeaweedFS, etc.) — ver [STACK_E_LIMITES.md](./STACK_E_LIMITES.md#-object-storage-s3-compatível-opcional).

## Submissão

- [ ] Arquivo `submissions/seu_usuario.json` criado no fork.
- [ ] JSON contém `participante`, `repositorio` (URL pública do seu código) e `email` (para receber o relatório da avaliação).
- [ ] Repositório da solução é público e contém o `Dockerfile` na raiz.

## Antes de abrir o PR

- [ ] Testou localmente com os mesmos limites de RAM/CPU quando possível.
- [ ] Revisou os [Gates de validação](./GATES_E_RANKING.md) — todas as queries DQ devem retornar 0.

## Após o merge

- [ ] A avaliação dispara automaticamente quando o JSON entra na `main` (workflow `push`).
- [ ] Acompanhe em **Actions → Avaliador de Submissoes** no repo oficial.
- [ ] Para reavaliar sem novo PR (ex.: após corrigir infra), o organizador usa **Run workflow** com `submissions/seu_usuario.json`.
