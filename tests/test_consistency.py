#!/usr/bin/env python3
"""
Teste de Consistência: 3 cenários × 3 repetições.

Valida repetibilidade e consistência da transformação DuckDB em:
  - Cenário A (Limpo):    10k linhas, dados padronizados, sem edge cases
  - Cenário B (Edge):     10k linhas, 2% edge cases concentrados
  - Cenário C (Volume):  100k linhas, misto normal + edge cases

Cada cenário roda 3 vezes. Em cada rodada:
  - Gera CSV sintético
  - Processa via DuckDB (DERIVATION_SQL)
  - Executa 13 DQ gates
  - Coleta: linhas, tempo, RAM estimada, DQ results

Ao final, compara resultados entre rodadas para comprovar consistência.

Uso:
    python tests/test_consistency.py
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import time
from io import StringIO
from typing import Any
from collections import defaultdict

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.schema import DERIVATION_SQL


# ─── Geradores de CSV ───────────────────────────────────────────────────────


def _format_capital_br(valor: int) -> str:
    """Formata valor inteiro como capital brasileiro: 1234567 → '1.234.567,00'."""
    if valor == 0:
        return "0,00"
    milhar = f"{valor:,}".replace(",", ".")
    return f"{milhar},00"


def generate_cenario_limpo(num_rows: int = 10_000) -> bytes:
    """Cenário A: dados limpos, sem edge cases."""
    buf = StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")

    porte_opcoes = ["01", "03", "05"]  # sem vazio, sem '00'

    for i in range(num_rows):
        cnpj = f"{10_000_000 + i:08d}"  # CNPJ sequencial único
        razao = f"EMPRESA {i} LTDA"
        natureza = f"{1000 + (i % 9000):04d}"
        qualif = f"{10 + (i % 89):02d}"
        capital = _format_capital_br((i % 5000) * 1000)
        porte = porte_opcoes[i % len(porte_opcoes)]
        ente = ""  # sempre vazio
        writer.writerow([cnpj, razao, natureza, qualif, capital, porte, ente])

    return buf.getvalue().encode("iso-8859-1")


def generate_cenario_edge(num_rows: int = 10_000) -> bytes:
    """Cenário B: 2% edge cases concentrados."""
    buf = StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")

    for i in range(num_rows):
        cnpj = f"{20_000_000 + i:08d}"

        # Edge: ; dentro de aspas a cada 50
        if i > 0 and i % 50 == 0:
            razao = f"EMPRESA; TESTE {i} LTDA"
        else:
            razao = f"EMPRESA {i} LTDA"

        natureza = f"{1000 + (i % 9000):04d}"

        # Edge: qualificacao curta a cada 100
        if i > 0 and i % 100 == 0:
            qualif = "5"
        else:
            qualif = f"{10 + (i % 89):02d}"

        # Edge: capital variado (milhar, zero, normal)
        if i > 0 and i % 80 == 0:
            capital = _format_capital_br(1_234_567)  # com ponto milhar
        elif i > 0 and i % 70 == 0:
            capital = "0,00"
        else:
            capital = _format_capital_br((i % 5000) * 1000)

        # Edge: porte vazio a cada 40, normal otherwise
        if i > 0 and i % 40 == 0:
            porte = ""
        else:
            porte_opcoes = ["00", "01", "03", "05"]
            porte = porte_opcoes[i % len(porte_opcoes)]

        # Edge: ente_federativo preenchido a cada 200
        if i > 0 and i % 200 == 0:
            ente = "SAO PAULO"
        else:
            ente = ""

        writer.writerow([cnpj, razao, natureza, qualif, capital, porte, ente])

    return buf.getvalue().encode("iso-8859-1")


def generate_cenario_volume(num_rows: int = 100_000) -> bytes:
    """Cenário C: misto limpo + edge cases, 100k linhas."""
    buf = StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")

    for i in range(num_rows):
        cnpj = f"{30_000_000 + i:08d}"

        # 1% edge: ; em aspas
        if i > 0 and i % 100 == 0:
            razao = f"EMPRESA; TESTE {i} LTDA"
        else:
            razao = f"EMPRESA {i} LTDA"

        natureza = f"{1000 + (i % 9000):04d}"

        qualif = f"{10 + (i % 89):02d}"

        # 1.5% edge: capital variado
        if i > 0 and i % 60 == 0:
            capital = _format_capital_br(9_876_543)  # com ponto milhar
        elif i > 0 and i % 50 == 0:
            capital = "0,00"
        else:
            capital = _format_capital_br((i % 5000) * 1000)

        # 2% edge: porte vazio
        if i > 0 and i % 50 == 0:
            porte = ""
        else:
            porte_opcoes = ["00", "01", "03", "05"]
            porte = porte_opcoes[i % len(porte_opcoes)]

        # 0.5% edge: ente preenchido
        if i > 0 and i % 200 == 0:
            ente = "SAO PAULO"
        else:
            ente = ""

        # 0.5% edge: MEI
        if i > 0 and i % 200 == 0:
            razao = f"MEI {i} {''.join(str((i + j) % 10) for j in range(11))}"

        writer.writerow([cnpj, razao, natureza, qualif, capital, porte, ente])

    return buf.getvalue().encode("iso-8859-1")


# ─── Adaptador DQ para DuckDB ───────────────────────────────────────────────


def adapt_dq_queries() -> dict[str, str]:
    """DQ queries adaptadas para DuckDB (RE2 regex em vez de ~ POSIX)."""
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
            WHERE (SUBSTRING(natureza_juridica, 1, 1) = '1' AND natureza_juridica_grupo != 'ADMINISTRAÇÃO PÚBLICA')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '2' AND natureza_juridica_grupo != 'ENTIDADES EMPRESARIAIS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '3' AND natureza_juridica_grupo != 'ENTIDADES SEM FINS LUCRATIVOS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '4' AND natureza_juridica_grupo != 'PESSOAS FÍSICAS')
               OR (SUBSTRING(natureza_juridica, 1, 1) = '5' AND natureza_juridica_grupo != 'ORGANIZAÇÕES INTERNACIONAIS')
               OR (SUBSTRING(natureza_juridica, 1, 1) NOT IN ('1','2','3','4','5') AND natureza_juridica_grupo != 'OUTROS')
        """,
        "DQ-12": """
            SELECT COUNT(*) FROM transformed
            WHERE (ente_federativo IS NOT NULL AND ente_federativo != '' AND ente_federativo_presente != TRUE)
               OR ((ente_federativo IS NULL OR ente_federativo = '') AND ente_federativo_presente != FALSE)
        """,
        "DQ-13": "SELECT COUNT(*) FROM transformed WHERE data_processamento IS NULL",
    }


# ─── Runner ──────────────────────────────────────────────────────────────────


def run_scenario(
    name: str, csv_bytes: bytes, repetition: int, dq_queries: dict[str, str]
) -> dict[str, Any]:
    """Roda uma repetição de cenário e retorna métricas."""
    # Escreve CSV temporário
    with tempfile.NamedTemporaryFile(
        suffix=".csv", prefix=f"consistency_{name}_", delete=False
    ) as f:
        f.write(csv_bytes)
        csv_path = f.name

    # Estima RAM via /proc/self/status antes
    ram_before = _get_rss_mb()

    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit = '200MB'")
    con.execute("SET threads = 1")

    start = time.perf_counter()

    try:
        # Cria view transformada
        derivation = DERIVATION_SQL.format(csv_path=csv_path)
        con.execute(f"CREATE OR REPLACE TEMP VIEW transformed AS ({derivation})")

        # Contagem de linhas
        total_rows = con.execute("SELECT COUNT(*) FROM transformed").fetchone()[0]

        # Executa DQ gates
        dq_results = {}
        for gate_name, sql in dq_queries.items():
            result = con.execute(sql).fetchone()
            dq_results[gate_name] = result[0] if result else -1

        # Amostra de 5 linhas para verificação estrutural
        sample = con.execute("SELECT * FROM transformed USING SAMPLE 5").fetchall()

        # Perfil das colunas derivadas
        profile = _profile_columns(con)

    finally:
        elapsed = time.perf_counter() - start
        ram_after = _get_rss_mb()
        con.close()
        os.unlink(csv_path)

    return {
        "cenario": name,
        "repeticao": repetition,
        "linhas_entrada": len(csv_bytes.decode("iso-8859-1").splitlines()),
        "linhas_transformadas": total_rows,
        "tempo_seg": round(elapsed, 3),
        "ram_estimada_mb": max(0, ram_after - ram_before),
        "dq_results": dq_results,
        "amostra": sample,
        "perfil": profile,
    }


def _get_rss_mb() -> int:
    """Retorna RSS em MB do processo atual."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except FileNotFoundError:
        pass
    return 0


def _profile_columns(con) -> dict[str, Any]:
    """Extrai perfil básico das colunas derivadas."""
    profile = {}

    # Distribuição de porte_codigo
    porte_dist = con.execute(
        "SELECT porte_codigo, COUNT(*) FROM transformed GROUP BY porte_codigo ORDER BY porte_codigo"
    ).fetchall()
    profile["porte_dist"] = dict(porte_dist)

    # Distribuição de capital_social_faixa
    faixa_dist = con.execute(
        "SELECT capital_social_faixa, COUNT(*) FROM transformed GROUP BY capital_social_faixa ORDER BY capital_social_faixa"
    ).fetchall()
    profile["faixa_dist"] = dict(faixa_dist)

    # Contagem de NULLs em ente_federativo
    ente_null = con.execute(
        "SELECT COUNT(*) FROM transformed WHERE ente_federativo IS NULL"
    ).fetchone()[0]
    ente_total = con.execute("SELECT COUNT(*) FROM transformed").fetchone()[0]
    profile["ente_null_pct"] = (
        round(ente_null / ente_total * 100, 2) if ente_total else 0
    )

    # Contagem de is_mei
    mei_count = con.execute(
        "SELECT COUNT(*) FROM transformed WHERE is_mei = TRUE"
    ).fetchone()[0]
    profile["is_mei_count"] = mei_count

    # Contagem ente_federativo_presente
    ente_presente = con.execute(
        "SELECT COUNT(*) FROM transformed WHERE ente_federativo_presente = TRUE"
    ).fetchone()[0]
    profile["ente_presente_count"] = ente_presente

    return profile


# ─── Comparador de Consistência ─────────────────────────────────────────────


def check_consistency(results: list[dict[str, Any]]) -> list[str]:
    """Compara resultados entre repetições do mesmo cenário."""
    inconsistencias = []

    # Agrupa por cenário
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_scenario.setdefault(r["cenario"], []).append(r)

    for cenario, rodadas in by_scenario.items():
        if len(rodadas) < 2:
            continue

        # 1. Mesmo número de linhas em todas as rodadas
        linhas = [r["linhas_transformadas"] for r in rodadas]
        if len(set(linhas)) != 1:
            inconsistencias.append(
                f"[{cenario}] Linhas divergem entre rodadas: {linhas}"
            )

        # 2. DQ gates = 0 em todas as rodadas
        for r in rodadas:
            for gate, count in r["dq_results"].items():
                if count != 0:
                    inconsistencias.append(
                        f"[{cenario}] Rodada {r['repeticao']}: {gate}={count}"
                    )

        # 3. Mesma distribuição de porte_codigo entre rodadas
        portes = [tuple(sorted(r["perfil"]["porte_dist"].items())) for r in rodadas]
        if len(set(portes)) != 1:
            inconsistencias.append(
                f"[{cenario}] Distribuição de porte_codigo difere entre rodadas"
            )

        # 4. Mesma distribuição de capital_social_faixa
        faixas = [tuple(sorted(r["perfil"]["faixa_dist"].items())) for r in rodadas]
        if len(set(faixas)) != 1:
            inconsistencias.append(
                f"[{cenario}] Distribuição de capital_social_faixa difere entre rodadas"
            )

        # 5. Tempo dentro de 30% da média
        tempos = [r["tempo_seg"] for r in rodadas]
        media = sum(tempos) / len(tempos)
        for t in tempos:
            if media > 0 and abs(t - media) / media > 0.30:
                inconsistencias.append(
                    f"[{cenario}] Tempo {t:.2f}s diverge >30% da média {media:.2f}s"
                )

    return inconsistencias


# ─── Relatório ───────────────────────────────────────────────────────────────


GERADORES = {
    "A (Limpo)": generate_cenario_limpo,
    "B (Edge)": generate_cenario_edge,
    "C (Volume)": generate_cenario_volume,
}

REPETICOES = 3


def main():
    print("=" * 72)
    print("TESTE DE CONSISTÊNCIA — Pipeline de Ingestão de Empresas")
    print("3 cenários × 3 repetições = 9 execuções")
    print("=" * 72)

    dq_queries = adapt_dq_queries()
    all_results: list[dict[str, Any]] = []

    for cenario, generator in GERADORES.items():
        print(f"\n{'─' * 72}")
        print(f"CENÁRIO {cenario}")
        print(f"{'─' * 72}")

        for rep in range(1, REPETICOES + 1):
            print(f"\n  Rodada {rep}/{REPETICOES} …")
            csv_bytes = generator()
            print(f"    CSV gerado: {len(csv_bytes):,} bytes")

            result = run_scenario(cenario, csv_bytes, rep, dq_queries)
            all_results.append(result)

            # Sumário da rodada
            dq_ok = all(v == 0 for v in result["dq_results"].values())
            print(f"    Linhas: {result['linhas_transformadas']:,}")
            print(f"    Tempo:  {result['tempo_seg']:.3f}s")
            print(f"    RAM:    ~{result['ram_estimada_mb']} MB")
            print(f"    DQ:     {'✅ Todos OK' if dq_ok else '❌ Falhas'}")

            if not dq_ok:
                for gate, count in result["dq_results"].items():
                    if count != 0:
                        print(f"      {gate}: {count}")

    # ── Verificação de Consistência ─────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("VERIFICAÇÃO DE CONSISTÊNCIA")
    print(f"{'=' * 72}")

    inconsistencias = check_consistency(all_results)

    if not inconsistencias:
        print("\n  ✅ NENHUMA INCONSISTÊNCIA ENCONTRADA")
        print("     Todas as 9 execuções são consistentes entre si.")
    else:
        print(f"\n  ❌ {len(inconsistencias)} INCONSISTÊNCIA(S) ENCONTRADA(S):")
        for inc in inconsistencias:
            print(f"    - {inc}")

    # ── Tabela Comparativa ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("TABELA COMPARATIVA")
    print(f"{'=' * 72}")
    print(
        f"{'Cenário':<16} {'Rod.':<5} {'Linhas':<12} {'Tempo(s)':<12} {'RAM(MB)':<10} {'DQ OK':<8}"
    )
    print(f"{'─' * 16} {'─' * 5} {'─' * 12} {'─' * 12} {'─' * 10} {'─' * 8}")

    for r in all_results:
        dq_ok = "✅" if all(v == 0 for v in r["dq_results"].values()) else "❌"
        print(
            f"{r['cenario']:<16} "
            f"{r['repeticao']:<5} "
            f"{r['linhas_transformadas']:<12,} "
            f"{r['tempo_seg']:<12.3f} "
            f"{r['ram_estimada_mb']:<10} "
            f"{dq_ok:<8}"
        )

    # ── Score estimado (projeção simplificada) ─────────────────────────
    print(f"\n{'=' * 72}")
    print("PROJEÇÃO DE SCORE (para 68,6M linhas)")
    print(f"{'=' * 72}")

    # Tira média de tempo e RAM do cenário C (volume)
    vol_results = [r for r in all_results if r["cenario"] == "C (Volume)"]
    if vol_results:
        tempo_medio_100k = sum(r["tempo_seg"] for r in vol_results) / len(vol_results)
        ram_media = sum(r["ram_estimada_mb"] for r in vol_results) / len(vol_results)

        # Extrapola para 68.6M linhas
        fator_escala = 68_629_148 / 100_000
        tempo_projetado = tempo_medio_100k * fator_escala * 0.6  # overhead I/O reduz
        ram_projetada = max(ram_media, 50)  # RAM escala parcialmente
        storage_projetado = 8783  # medido em benchmark multi-ZIP anterior

        score = 1000 * (
            0.60 * tempo_projetado / 3600
            + 0.25 * ram_projetada / 1024
            + 0.15 * storage_projetado / 4096
        )

        print(f"  Tempo médio 100k linhas:  {tempo_medio_100k:.3f}s")
        print(f"  RAM média:                ~{ram_media:.0f} MB")
        print(
            f"  Tempo projetado 68,6M:    ~{tempo_projetado:.0f}s ({tempo_projetado / 60:.1f}min)"
        )
        print(f"  RAM projetada:            ~{ram_projetada:.0f} MB")
        print(f"  Storage projetado:        ~{storage_projetado} MB")
        print(f"  SCORE PROJETADO:          ~{score:.0f}")

    # ── Resultado Final ────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    if not inconsistencias:
        print("RESULTADO FINAL: ✅ CONSISTÊNCIA COMPROVADA")
        print("3 cenários × 3 repetições = 9 execuções consistentes.")
        sys.exit(0)
    else:
        print("RESULTADO FINAL: ❌ INCONSISTÊNCIAS ENCONTRADAS")
        print("Revisar pipeline antes de submeter.")
        sys.exit(1)


if __name__ == "__main__":
    main()
