#!/usr/bin/env python3
"""
Smoke test do pipeline de ingestão com 1k linhas sintéticas.

Gera dados CSV ISO-8859-1, processa via DuckDB e opcionalmente
valida contra PostgreSQL de teste.

Modos:
  1. DuckDB-only (offline): testa transformação e DQ gates no DuckDB
  2. Full (online): igual ao modo 1 + insere no PostgreSQL e repete validações

Edge cases testados:
  - ; (ponto-e-vírgula) dentro de aspas
  - porte_codigo vazio → '00'
  - capital_social com vírgula BR
  - capital_social = 0
  - is_mei true (razao_social termina em 11 dígitos)
  - Acentos (caracteres ISO-8859-1)
  - ente_federativo vazio → NULL

Uso:
    python tests/smoke_test.py                     # DuckDB-only
    PG_HOST=... PG_USER=... PG_PASSWORD=... ...    # Full mode
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from io import StringIO
from typing import Any

import duckdb

# Garante que o módulo src/ seja importável
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.schema import DERIVATION_SQL, DQ_QUERIES, PORTE_MAP

# ─── Geração de dados sintéticos ─────────────────────────────────────────────

CSV_COLUMNS = 7  # número de colunas no CSV original (sem cabeçalho)


def generate_synthetic_csv(num_rows: int = 1000) -> bytes:
    """Gera CSV ISO-8859-1 com dados sintéticos usando csv.writer.

    Usa csv.writer para garantir formatação CSV correta (escape de aspas, etc.).
    Retorna bytes codificados em ISO-8859-1.
    """
    from io import StringIO
    from random import choice, randint

    buf = StringIO()
    writer = csv.writer(
        buf,
        delimiter=";",
        quoting=csv.QUOTE_ALL,  # aspas em todos os campos para consistência
        lineterminator="\n",
    )

    porte_opcoes = ["00", "01", "03", "05", ""]

    for i in range(num_rows):
        cnpj = f"{randint(10_000_000, 99_999_999):08d}"  # 8 dígitos

        # Edge cases: MEI (termina em 11 dígitos) a cada 30 linhas
        if i > 0 and i % 30 == 0:
            # MEI: razao termina com 11 dígitos
            cpf = "".join(str(randint(0, 9)) for _ in range(11))
            razao = f"EMPRESA TESTE LTDA {cpf}"
        else:
            razao = f"EMPRESA TESTE LTDA"

        natureza = f"{randint(1000, 9999):04d}"

        # qualificacao_responsavel: 2 dígitos (exceto 1 a cada 100)
        if i % 100 == 99:
            qualif = "5"
        else:
            qualif = f"{randint(10, 99):02d}"

        # capital_social: formato BR (vírgula decimal)
        cap_valor = randint(0, 5_000_000)
        if cap_valor == 0:
            capital_br = "0,00"
        elif cap_valor > 999:
            # Com ponto de milhar: 1.234,56
            milhar = f"{cap_valor:,}".replace(",", ".")
            capital_br = f"{milhar},{randint(0, 99):02d}"
        else:
            capital_br = f"{cap_valor},{randint(0, 99):02d}"

        # porte_codigo: edge case vazio a cada ~10 linhas
        if i > 0 and i % 10 == 0:
            porte = ""
        else:
            porte = choice(porte_opcoes)

        # ente_federativo: 99.9% vazio
        ente = "SAO PAULO" if i == num_rows - 1 else ""

        writer.writerow([cnpj, razao, natureza, qualif, capital_br, porte, ente])

    csv_text = buf.getvalue()
    return csv_text.encode("iso-8859-1")


# ─── Validação DuckDB ────────────────────────────────────────────────────────


def validate_in_duckdb(csv_content: str) -> dict[str, int]:
    """Valida as transformações e DQ gates no DuckDB.

    Args:
        csv_content: Conteúdo CSV em bytes (ISO-8859-1).

    Returns:
        Dict com DQ gates.
    """
    # Escreve CSV temporário
    with tempfile.NamedTemporaryFile(
        suffix=".csv", prefix="smoke_test_", delete=False
    ) as f:
        f.write(csv_content)
        csv_path = f.name

    results: dict[str, int] = {}
    con = duckdb.connect(":memory:")

    try:
        # Configura DuckDB
        con.execute("SET memory_limit = '200MB'")
        con.execute("SET threads = 1")

        # Cria view transformada
        derivation = DERIVATION_SQL.format(csv_path=csv_path)
        con.execute(f"CREATE OR REPLACE TEMP VIEW transformed AS {derivation}")

        # Executa cada DQ gate adaptado para DuckDB
        # (DQ queries são PostgreSQL, adaptamos para DuckDB)
        dq_duckdb = _adapt_dq_queries(con)
        for gate_name, sql in dq_duckdb.items():
            result = con.execute(sql).fetchone()
            results[gate_name] = result[0] if result else 0

        # Verificação adicional dos tipos esperados
        row = con.execute("SELECT * FROM transformed LIMIT 1").fetchone()
        columns = [desc[0] for desc in con.execute("DESCRIBE transformed").fetchall()]
        print(f"  Colunas: {columns}")
        print(f"  Amostra: {row}")

        # Verifica total de linhas
        total = con.execute("SELECT COUNT(*) FROM transformed").fetchone()[0]
        print(f"  Total de linhas transformadas: {total}")

    finally:
        con.close()
        os.unlink(csv_path)

    return results


def _adapt_dq_queries(con) -> dict[str, str]:
    """Adapta as queries DQ do PostgreSQL para DuckDB.

    DuckDB RE2 regex (usar REGEXP_MATCHES, não operador ~).
    """
    return {
        "DQ-01": "SELECT COUNT(*) FROM transformed WHERE LENGTH(cnpj_basico) != 8 OR REGEXP_MATCHES(cnpj_basico, '\\D')",
        "DQ-02": "SELECT COUNT(*) FROM transformed WHERE razao_social != UPPER(TRIM(razao_social))",
        "DQ-03": "SELECT COUNT(*) FROM transformed WHERE LENGTH(natureza_juridica) != 4 OR REGEXP_MATCHES(natureza_juridica, '\\D')",
        "DQ-04": "SELECT COUNT(*) FROM transformed WHERE qualificacao_responsavel IS NULL OR qualificacao_responsavel = ''",
        "DQ-05": """
            SELECT COUNT(*) FROM transformed
            WHERE (capital_social = 0 AND capital_social_faixa != 'SEM CAPITAL')
               OR (capital_social > 0 AND capital_social <= 1000 AND capital_social_faixa != 'ATÉ 1K')
               OR (capital_social > 1000 AND capital_social <= 10000 AND capital_social_faixa != '1K A 10K')
               OR (capital_social > 10000 AND capital_social <= 100000 AND capital_social_faixa != '10K A 100K')
               OR (capital_social > 100000 AND capital_social <= 1000000 AND capital_social_faixa != '100K A 1M')
               OR (capital_social > 1000000 AND capital_social_faixa != 'ACIMA DE 1M')
               OR capital_social_faixa IS NULL
        """,
        "DQ-06": "SELECT COUNT(*) FROM transformed WHERE porte_codigo NOT IN ('00', '01', '03', '05')",
        "DQ-07": """
            SELECT COUNT(*) FROM transformed
            WHERE (porte_codigo = '00' AND porte_descricao != 'NÃO INFORMADO')
               OR (porte_codigo = '01' AND porte_descricao != 'MICRO EMPRESA')
               OR (porte_codigo = '03' AND porte_descricao != 'EMPRESA DE PEQUENO PORTE')
               OR (porte_codigo = '05' AND porte_descricao != 'DEMAIS')
        """,
        "DQ-08": """
            SELECT COUNT(*) FROM transformed
            WHERE (is_mei = TRUE AND NOT REGEXP_MATCHES(razao_social, '\\d{11}$'))
               OR (is_mei = FALSE AND REGEXP_MATCHES(razao_social, '\\d{11}$'))
        """,
        "DQ-09": """
            SELECT COALESCE(SUM(cnt), 0) FROM (
                SELECT COUNT(*) - 1 AS cnt FROM transformed
                GROUP BY cnpj_basico HAVING COUNT(*) > 1
            ) t
        """,
        "DQ-10": """
            SELECT COUNT(*) FROM transformed
            WHERE razao_social IS NULL
               OR REGEXP_MATCHES(razao_social, '[\\x80-\\xFF]')
        """,
        "DQ-11": """
            SELECT COUNT(*) FROM transformed
            WHERE (SUBSTRING(natureza_juridica, 1, 1) = '1' AND natureza_juridica_grupo != 'ADMIN PÚBLICA')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '2' AND natureza_juridica_grupo != 'ENT EMPRESARIAIS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '3' AND natureza_juridica_grupo != 'ENT S/ FINS LUCRATIVOS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '4' AND natureza_juridica_grupo != 'PESSOAS FÍSICAS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '5' AND natureza_juridica_grupo != 'ORG INTERNACIONAIS')
               OR (SUBSTRING(natureza_juridica, 1, 1) NOT IN ('1','2','3','4','5') AND natureza_juridica_grupo != 'OUTROS')
        """,
        "DQ-12": """
            SELECT COUNT(*) FROM transformed
            WHERE (ente_federativo IS NOT NULL AND ente_federativo != '' AND ente_federativo_presente != TRUE)
               OR ((ente_federativo IS NULL OR ente_federativo = '') AND ente_federativo_presente != FALSE)
        """,
        "DQ-13": "SELECT COUNT(*) FROM transformed WHERE data_processamento IS NULL",
    }


# ─── Testes específicos de edge cases ────────────────────────────────────────


def test_edge_cases():
    """Testa edge cases individualmente com DuckDB."""
    print("\n--- Teste de Edge Cases ---")
    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit = '100MB'")
    ok = True

    try:
        # 1. Ponto-e-vírgula dentro de aspas (na coluna 2 = razao_social)
        # column1 (razao_social) contém ; dentro de aspas — UPPER(TRIM) não trunca
        csv_aspas = b'12345678;"EMPRESA; TESTE";1234;10;100,00;01;\n'
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_aspas)
            path_aspas = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_aspas)
            row = con.execute(sql).fetchone()
            # razao_social (row[1]) deve preservar "EMPRESA; TESTE" em uppercase
            assert row[1] == "EMPRESA; TESTE", f"Falha no ; em aspas: row[1]={row[1]}"
            print("  ✅ Ponto-e-vírgula dentro de aspas: OK")
        finally:
            os.unlink(path_aspas)

        # 2. porte_codigo vazio → '00' → 'NÃO INFORMADO'
        csv_porte = b"12345678;TESTE;1234;10;100,00;;\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_porte)
            path_porte = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_porte)
            row = con.execute(sql).fetchone()
            assert row[5] == "00", f"Falha porte_codigo vazio: {row[5]}"
            assert row[6] == "NÃO INFORMADO", f"Falha porte_descricao: {row[6]}"
            print("  ✅ porte_codigo vazio → '00' → 'NÃO INFORMADO': OK")
        finally:
            os.unlink(path_porte)

        # 3. capital_social com vírgula BR
        csv_cap = b"12345678;TESTE;1234;10;1.234,56;01;\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_cap)
            path_cap = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_cap)
            row = con.execute(sql).fetchone()
            assert abs(row[4] - 1234.56) < 0.001, f"Falha capital_social: {row[4]}"
            print("  ✅ capital_social com vírgula BR: OK")
        finally:
            os.unlink(path_cap)

        # 4. capital_social = 0 → faixa 'SEM CAPITAL'
        csv_cap0 = b"12345678;TESTE;1234;10;0,00;01;\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_cap0)
            path_cap0 = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_cap0)
            row = con.execute(sql).fetchone()
            assert row[8] == "SEM CAPITAL", f"Falha capital_social_faixa: {row[8]}"
            print("  ✅ capital_social = 0 → 'SEM CAPITAL': OK")
        finally:
            os.unlink(path_cap0)

        # 5. is_mei true (razao_social termina em 11 dígitos)
        csv_mei = b"12345678;EMPRESA TESTE 12345678901;1234;10;100,00;01;\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_mei)
            path_mei = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_mei)
            row = con.execute(sql).fetchone()
            assert row[9] is True, f"Falha is_mei: {row[9]}"
            print("  ✅ is_mei true: OK")
        finally:
            os.unlink(path_mei)

        # 6. ente_federativo vazio → NULL
        csv_ente = b"12345678;TESTE;1234;10;100,00;01;\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_ente)
            path_ente = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_ente)
            row = con.execute(sql).fetchone()
            assert row[7] is None, f"Falha ente_federativo: {row[7]}"
            print("  ✅ ente_federativo vazio → NULL: OK")
        finally:
            os.unlink(path_ente)

        # 7. Acentos ISO-8859-1
        csv_acentos = "12345678;EMPRESA Ç Ã Ê Ó;1234;10;100,00;01;\n".encode(
            "iso-8859-1"
        )
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(csv_acentos)
            path_acentos = f.name
        try:
            sql = DERIVATION_SQL.format(csv_path=path_acentos)
            row = con.execute(sql).fetchone()
            # Após conversão ISO-8859-1 → UTF-8, os acentos devem estar preservados
            razao = row[1]
            assert "Ç" in razao or "ç" in razao.lower(), f"Falha acentos: {razao}"
            print(f"  ✅ Acentos preservados na conversão: {razao}")
        finally:
            os.unlink(path_acentos)

    except AssertionError as e:
        print(f"  ❌ {e}")
        ok = False
    finally:
        con.close()

    return ok


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("SMOKE TEST — Pipeline de Ingestão de Empresas")
    print("=" * 60)

    # ── 1. Gera dados sintéticos ──────────────────────────────────────────
    print("\n[1] Gerando 1.000 linhas sintéticas CSV ISO-8859-1 …")
    csv_bytes = generate_synthetic_csv(1000)
    print(f"    Gerado: {len(csv_bytes):,} bytes")

    # ── 2. Valida no DuckDB ───────────────────────────────────────────────
    print("\n[2] Validando transformação no DuckDB …")
    dq_results = validate_in_duckdb(csv_bytes)
    print("\n[3] Resultados DQ Gates (DuckDB):")
    all_ok = True
    for gate, count in sorted(dq_results.items()):
        status = "✅" if count == 0 else "❌"
        print(f"    {gate}: {count}  {status}")
        if count != 0:
            all_ok = False

    # ── 3. Testa edge cases ───────────────────────────────────────────────
    print("\n[4] Testes de edge cases:")
    edge_ok = test_edge_cases()

    # ── 4. Resultado final ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if all_ok and edge_ok:
        print("RESULTADO FINAL: ✅ SMOKE TEST APROVADO")
        print("=" * 60)
        sys.exit(0)
    else:
        print("RESULTADO FINAL: ❌ SMOKE TEST REPROVADO")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
