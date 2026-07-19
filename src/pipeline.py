"""
Pipeline de ingestão: DuckDB → PostgreSQL.

Fluxo:
  1. Cria/recria a tabela alvo (DROP + CREATE)
  2. Para cada ZIP em /data/:
     a. Extrai CSVs do ZIP para /tmp/
     b. DuckDB :memory: lê o CSV via read_csv (streaming)
     c. Aplica derivações SQL + exporta para CSV nativamente (COPY TO)
     d. psycopg2 copy_expert lê o CSV do disco direto para PostgreSQL
  3. Fecha conexões

Idempotente: TRUNCATE + recria tabela no início.
OOM-safe: memória controlada via DuckDB memory_limit + processamento por ZIP.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

import duckdb
import psycopg2

from schema import DDL_TEMPLATE, DERIVATION_SQL

# ─── Constantes ──────────────────────────────────────────────────────────────

ZIP_DIR: str = "/data"
DUCKDB_MEMORY_LIMIT: str = "700MB"
DUCKDB_TEMP_DIR: str = "/app/duckdb_temp"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _drop_file_cache(path: str, sync: bool = True) -> None:
    """Descarta o page cache de um arquivo (fsync + fadvise DONTNEED).

    Evita que a leitura/escrita de CSVs grandes infle o uso de memória
    do container via page cache do kernel.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            if sync:
                os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except OSError:
        pass  # best-effort: fadvise indisponível não pode quebrar o pipeline


class _CacheJanitor:
    """Thread que descarta page cache dos arquivos monitorados a cada 1s,
    em paralelo à escrita/leitura dos CSVs grandes.
    """

    def __init__(self, *paths: str):
        self._paths = paths
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            for p in self._paths:
                if os.path.exists(p):
                    # sync=False: fsync a cada 1s num arquivo em escrita
                    # serializaria o writeback e mataria o throughput
                    _drop_file_cache(p, sync=False)

    def __enter__(self) -> "_CacheJanitor":
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._t.join()


def _get_zip_list() -> list[str]:
    """Retorna lista ordenada de paths de ZIPs válidos em /data/.

    Suporta nomes como Empresas0.zip … Empresas9.zip ou empresas0.zip …
    """
    if not os.path.isdir(ZIP_DIR):
        print(f"[WARN] Diretório {ZIP_DIR} não encontrado. Usando lista vazia.")
        return []
    entries = os.listdir(ZIP_DIR)
    zip_files = sorted(
        os.path.join(ZIP_DIR, f)
        for f in entries
        if f.lower().endswith(".zip") and os.path.isfile(os.path.join(ZIP_DIR, f))
    )
    return zip_files


# ─── Funções do Pipeline ─────────────────────────────────────────────────────


def create_table(pg_conn, pg_table: str) -> None:
    """Cria a tabela alvo com DDL otimizado (fillfactor=100)."""
    ddl = DDL_TEMPLATE.format(table=pg_table)
    with pg_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {pg_table}")
        cur.execute(ddl)
    pg_conn.commit()
    print(f"[TABLE] Tabela {pg_table} criada/recriada.")


def _process_csv(
    csv_path: str,
    pg_conn,
    pg_table: str,
    label: str,
) -> int:
    """Processa um único arquivo CSV via DuckDB → PostgreSQL.

    Cria conexão DuckDB :memory:, aplica derivações, itera em chunks,
    copia via COPY. Fecha DuckDB ao final.
    """
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = '{DUCKDB_TEMP_DIR}'")
    con.execute("SET threads = 2")

    total_rows = 0
    tmp_csv = os.path.join(DUCKDB_TEMP_DIR, f"{os.path.basename(csv_path)}.out.csv")
    try:
        derivation_sql = DERIVATION_SQL.format(csv_path=csv_path)

        # Descarta o page cache dos CSVs grandes a cada 1s enquanto são
        # escritos/lidos, para manter o uso de memória do container sob controle.
        with _CacheJanitor(csv_path, tmp_csv):
            # (a) DuckDB extrai + formata CSV nativamente (sem loop Python por linha)
            t0 = time.perf_counter()
            con.execute(
                f"COPY ({derivation_sql}) TO '{tmp_csv}' "
                "(FORMAT CSV, DELIMITER ';', NULLSTR '', HEADER FALSE)"
            )
            t_extract = time.perf_counter() - t0
            _check_rss()

            rejected = con.execute("SELECT COUNT(*) FROM reject_errors").fetchone()[0]
            if rejected:
                print(f"  [WARN] {label}: {rejected:,} linha(s) rejeitada(s) pelo parser CSV (malformadas).")

            # (b) copy_expert para PostgreSQL direto do arquivo (streaming)
            t0 = time.perf_counter()
            with open(tmp_csv, "rb") as f, pg_conn.cursor() as pg_cur:
                pg_cur.copy_expert(
                    f"COPY {pg_table} FROM STDIN (FORMAT CSV, DELIMITER ';', NULL '')",
                    f,
                )
                total_rows = pg_cur.rowcount
            t_copy = time.perf_counter() - t0
        # Commit ao final de cada ZIP (não por chunk) — reduz ~1.373 commits
        # para 10 commits no total. Seguro com synchronous_commit=off.

        print(f"  [COPY] {label}: {total_rows:,} linhas")
        _check_rss()

    except Exception:
        print(f"[ERRO] Falha ao processar CSV {csv_path}")
        pg_conn.rollback()
        raise
    finally:
        con.close()
        if os.path.exists(tmp_csv):
            os.remove(tmp_csv)

    if total_rows > 0:
        t_total = t_extract + t_copy
        print(
            f"  [PROFILE] {label}: extract(duckdb)={t_extract:.2f}s "
            f"({t_extract / t_total * 100:.0f}%) copy(pg)={t_copy:.2f}s "
            f"({t_copy / t_total * 100:.0f}%) total={t_total:.2f}s"
        )

    return total_rows


def process_zip(
    zip_path: str,
    pg_conn,
    pg_table: str,
) -> int:
    """Processa um único ZIP.

    1. Extrai CSVs para /tmp/
    2. Para cada CSV: DuckDB :memory: → derivações → COPY PostgreSQL
    3. Limpa arquivos extraídos

    Args:
        zip_path: Caminho absoluto para o .zip
        pg_conn: Conexão psycopg2
        pg_table: Nome da tabela alvo

    Returns:
        Número total de linhas inseridas deste ZIP
    """
    basename = os.path.basename(zip_path)
    print(f"[ZIP] Processando {basename} …")

    # ── Extrai ZIP para diretório temporário ──────────────────────────────
    extract_dir = tempfile.mkdtemp(prefix="empresas_")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception:
        print(f"[ERRO] Falha ao extrair {basename}")
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise

    # ── Lista CSVs extraídos ──────────────────────────────────────────────
    csv_files: list[str] = []
    for root, _dirs, files in os.walk(extract_dir):
        for f in sorted(files):
            csv_files.append(os.path.join(root, f))

    if not csv_files:
        print(f"[WARN] Nenhum CSV encontrado em {basename}")
        shutil.rmtree(extract_dir, ignore_errors=True)
        return 0

    print(f"  Arquivos no ZIP: {len(csv_files)}")
    grand_total = 0

    for csv_file in csv_files:
        fname = os.path.basename(csv_file)
        print(f"  [CSV] Processando {fname} …")
        rows = _process_csv(csv_file, pg_conn, pg_table, f"{basename}/{fname}")
        grand_total += rows
        print(f"  [CSV] {fname}: {rows:,} linhas")

    # ── Limpeza ───────────────────────────────────────────────────────────
    shutil.rmtree(extract_dir, ignore_errors=True)

    # Commit único ao final do ZIP (não por chunk) — economiza ~1.373 round-trips
    pg_conn.commit()

    print(f"[ZIP] {basename} concluído — {grand_total:,} linhas.")
    return grand_total


def _dedupe_cnpj_basico(pg_conn, pg_table: str) -> int:
    """Remove linhas com `cnpj_basico` duplicado, mantendo a primeira ocorrência.

    Passo único (GROUP BY + DELETE direcionado por ctid), não self-join
    completo — evita índice permanente (custa storage no score) mantendo
    o custo de uma limpeza pontual pós-carga.
    """
    sql = f"""
        DELETE FROM {pg_table} t
        USING (
            SELECT cnpj_basico, min(ctid) AS keep_ctid
            FROM {pg_table}
            GROUP BY cnpj_basico
            HAVING COUNT(*) > 1
        ) dups
        WHERE t.cnpj_basico = dups.cnpj_basico
          AND t.ctid <> dups.keep_ctid
    """
    with pg_conn.cursor() as cur:
        cur.execute(sql)
        removed = cur.rowcount
    pg_conn.commit()
    return removed


def _check_rss() -> None:
    """Monitora RSS via /proc/self/status e aborta se > 800 MB."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    rss_kb = int(parts[1]) if len(parts) >= 2 else 0
                    rss_mb = rss_kb / 1024
                    if rss_mb > 800:
                        print(
                            f"[OOM] RSS = {rss_mb:.0f} MB > 800 MB — "
                            f"abortando para evitar OOM kill (exit 137)."
                        )
                        sys.exit(137)
                    elif rss_mb > 600:
                        print(
                            f"[WARN] RSS = {rss_mb:.0f} MB — próximo do limite de 1 GB."
                        )
                    return
    except FileNotFoundError:
        pass  # /proc não disponível (Windows, container restrito)


def run_pipeline(pg_conn, pg_table: str) -> int:
    """Orquestra o pipeline completo.

    Args:
        pg_conn: Conexão psycopg2
        pg_table: Nome da tabela alvo

    Returns:
        Número total de linhas inseridas

    Raises:
        SystemExit(137) se OOM detectado
    """
    start = time.time()

    # ── 1. Cria tabela (DROP + CREATE) ─────────────────────────────────────
    create_table(pg_conn, pg_table)

    # ── 2. Seta synchronous_commit = off ───────────────────────────────────
    with pg_conn.cursor() as cur:
        cur.execute("SET synchronous_commit = off")
    pg_conn.commit()

    # ── 3. Lista ZIPs e processa ───────────────────────────────────────────
    zip_list = _get_zip_list()
    if not zip_list:
        print(f"[ERRO] Nenhum arquivo ZIP encontrado em {ZIP_DIR}/")
        print(
            f"  Arquivos encontrados: {os.listdir(ZIP_DIR) if os.path.isdir(ZIP_DIR) else 'DIRETÓRIO NÃO ENCONTRADO'}"
        )
        return 0

    print(f"[PIPELINE] {len(zip_list)} ZIP(s) encontrados.")
    grand_total = 0
    for i, zp in enumerate(zip_list, 1):
        t0 = time.time()
        rows = process_zip(zp, pg_conn, pg_table)
        elapsed = time.time() - t0
        grand_total += rows
        print(
            f"  [FIM] ZIP {i}/{len(zip_list)}: {os.path.basename(zp)} "
            f"— {rows:,} linhas em {elapsed:.1f}s "
            f"(rate: {rows / elapsed:,.0f} linhas/s)"
        )

    # ── 4. Remove duplicatas de cnpj_basico (mantém a primeira ocorrência) ──
    removed = _dedupe_cnpj_basico(pg_conn, pg_table)
    if removed:
        print(f"[DEDUP] {removed:,} linha(s) duplicada(s) de cnpj_basico removida(s).")
        grand_total -= removed

    elapsed_total = time.time() - start
    print(f"\n[PIPELINE] Total: {grand_total:,} linhas em {elapsed_total:.1f}s.")

    # Log de RAM pico
    rss_peak = _get_rss_mb()
    if rss_peak is not None:
        print(f"[MEM] Pico RSS: {rss_peak:.0f} MB")
    return grand_total


def _get_rss_mb() -> float | None:
    """Lê RSS atual de /proc/self/status."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) / 1024
    except (FileNotFoundError, IndexError, ValueError):
        return None
