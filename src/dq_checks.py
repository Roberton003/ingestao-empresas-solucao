"""
DQ (Data Quality) Gates — 13 validações pós-INSERT.

Cada gate retorna um COUNT de violações. O pipeline espera 0 em todos.
"""

from typing import Any

import psycopg2

from schema import DQ_QUERIES


def run_dq_checks(pg_conn, pg_table: str) -> dict[str, int]:
    """Executa os 13 DQ gates na tabela PostgreSQL.

    Args:
        pg_conn: Conexão psycopg2 ativa para o banco alvo.
        pg_table: Nome fully-qualified da tabela (ex: public.roberton003_empresas).

    Returns:
        Dict { "DQ-01": 0, "DQ-02": 0, ..., "DQ-13": 0 } com contagem de violações.

    Raises:
        psycopg2.Error: Se qualquer query falhar.
    """
    results: dict[str, int] = {}
    with pg_conn.cursor() as cur:
        for gate_name, query_template in DQ_QUERIES.items():
            sql = query_template.format(table=pg_table)
            cur.execute(sql)
            row = cur.fetchone()
            count: int = row[0] if row is not None else 0
            results[gate_name] = count
    return results


def format_dq_report(results: dict[str, int]) -> str:
    """Formata o relatório de DQ para exibição."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("RELATÓRIO DE QUALIDADE DE DADOS (DQ GATES)")
    lines.append("=" * 60)
    all_zero = True
    for gate_name in sorted(results.keys()):
        count = results[gate_name]
        status = "✅" if count == 0 else "❌"
        lines.append(f"  {gate_name}: {count:>8} violações  {status}")
        if count != 0:
            all_zero = False
    total_violations = sum(results.values())
    lines.append("-" * 60)
    lines.append(f"  Total violações:      {total_violations:>8}")
    lines.append(
        f"  Status geral:         {'✅ APROVADO' if all_zero else '❌ REPROVADO'}"
    )
    lines.append("=" * 60)
    return "\n".join(lines)


def compute_score(dq_results: dict[str, int]) -> float:
    """Calcula score DQ — 0 se todas as violações forem 0, senão cresce.

    Score DQ é uma métrica auxiliar (não o score oficial da competição).
    """
    return float(sum(dq_results.values()))
