#!/usr/bin/env python3
"""
Entry point do pipeline de ingestão.

Lê variáveis de ambiente, conecta ao PostgreSQL e executa o pipeline.
Os gates DQ são aplicados pelo avaliador, não pelo pipeline do participante.

Uso:
    PARTICIPANTE=roberton003 \
    PG_TABLE=public.roberton003_empresas \
    PG_HOST=postgres_db \
    PG_PORT=5432 \
    PG_USER=homelab_postgres \
    PG_PASSWORD=... \
    PG_DB=db_empresas \
    python main.py

Exit codes:
    0   — Sucesso (G2 aprovado)
    137 — OOM kill (desclassificação)
    1   — Erro genérico (falha de conexão, erro no pipeline, etc.)
"""

from __future__ import annotations

import os
import sys

import psycopg2

from pipeline import run_pipeline


def _get_env_or_fail(key: str) -> str:
    """Lê variável de ambiente ou aborta."""
    val = os.environ.get(key)
    if not val:
        print(f"[FATAL] Variável de ambiente {key} não definida.")
        sys.exit(1)
    return val


def main() -> None:
    """Pipeline principal."""

    # ── Lê ambiente ─────────────────────────────────────────────────────────
    participante = _get_env_or_fail("PARTICIPANTE")
    pg_table = _get_env_or_fail("PG_TABLE")
    pg_host = _get_env_or_fail("PG_HOST")
    pg_port = _get_env_or_fail("PG_PORT")
    pg_user = _get_env_or_fail("PG_USER")
    pg_password = _get_env_or_fail("PG_PASSWORD")
    pg_db = _get_env_or_fail("PG_DB")

    print(f"Iniciando pipeline para participante: {participante}")
    print(f"Tabela alvo: {pg_table}")
    print(f"PostgreSQL: {pg_host}:{pg_port}/{pg_db} usuário: {pg_user}")

    # ── Conexão PostgreSQL ─────────────────────────────────────────────────
    try:
        pg_conn = psycopg2.connect(
            host=pg_host,
            port=pg_port,
            user=pg_user,
            password=pg_password,
            dbname=pg_db,
        )
        pg_conn.autocommit = False  # commits manuais para controlar transações
        print("[PG] Conexão estabelecida.")
    except psycopg2.Error as e:
        print(f"[FATAL] Erro ao conectar ao PostgreSQL: {e}")
        sys.exit(1)

    # ── Executa pipeline ───────────────────────────────────────────────────
    try:
        total_rows = run_pipeline(pg_conn, pg_table)
    except MemoryError:
        print("[OOM] Memória insuficiente detectada. Abortando.")
        sys.exit(137)
    except Exception as e:
        print(f"[FATAL] Erro no pipeline: {e}")
        pg_conn.rollback()
        sys.exit(1)

    # ── Sucesso ─────────────────────────────────────────────────────────────
    print("[RESULTADO] ✅ Pipeline concluído com sucesso.")
    print(f"[RESULTADO] Total de linhas: {total_rows:,}")
    sys.exit(0)


if __name__ == "__main__":
    main()
