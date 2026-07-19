//! Reescrita em Rust do pipeline de ingestão (ZIP -> CSV latin-1 -> Postgres COPY).
//!
//! Contrato idêntico ao main.py/pipeline.py/schema.py originais:
//! mesmas env vars, mesmo DDL, mesma DERIVATION_SQL (reimplementada linha a
//! linha em vez de SQL), mesmo dedup por bitmap de cnpj_basico (10^8 bits).
//!
//! Único estado de vida longa é o bitmap (12,5 MB) e os buffers reutilizados
//! no loop por linha — nada acumula na heap conforme o volume processado cresce.

use std::env;
use std::fs::File;
use std::io::{BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use postgres::{Client, NoTls};

const BITMAP_BYTES: usize = 100_000_000 / 8;
const ZIP_DIR: &str = "/data";

const DDL: &str = "
CREATE TABLE IF NOT EXISTS {table} (
    cnpj_basico              VARCHAR(8) NOT NULL,
    razao_social             VARCHAR(200) NOT NULL,
    natureza_juridica        VARCHAR(4) NOT NULL,
    qualificacao_responsavel VARCHAR(2) NOT NULL,
    capital_social           DOUBLE PRECISION NOT NULL,
    porte_codigo              VARCHAR(2) NOT NULL,
    porte_descricao           VARCHAR(30) NOT NULL,
    ente_federativo           VARCHAR(100),
    capital_social_faixa      VARCHAR(20) NOT NULL,
    is_mei                    BOOLEAN NOT NULL,
    natureza_juridica_grupo   VARCHAR(30) NOT NULL,
    ente_federativo_presente  BOOLEAN NOT NULL,
    data_processamento        TIMESTAMP NOT NULL
) WITH (fillfactor=100, autovacuum_enabled=false);
";

fn get_env_or_fail(key: &str) -> String {
    match env::var(key) {
        Ok(v) if !v.is_empty() => v,
        _ => {
            println!("[FATAL] Variável de ambiente {key} não definida.");
            std::process::exit(1);
        }
    }
}

fn create_table(client: &mut Client, table: &str) -> Result<(), postgres::Error> {
    client.batch_execute(&format!("DROP TABLE IF EXISTS {table}"))?;
    client.batch_execute(&DDL.replace("{table}", table))?;
    println!("[TABLE] Tabela {table} criada/recriada.");
    Ok(())
}

fn list_zips(dir: &str) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(entries) => entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension()
                        .map(|ext| ext.to_ascii_lowercase() == "zip")
                        .unwrap_or(false)
            })
            .collect(),
        Err(_) => {
            println!("[WARN] Diretório {dir} não encontrado. Usando lista vazia.");
            Vec::new()
        }
    };
    out.sort();
    out
}

/// Bitmap fixo de 10^8 bits (12,5 MB) — 1 alocação para o pipeline inteiro.
/// mark_if_new: true se o cnpj_basico ainda não tinha sido visto (marca e mantém a linha).
struct DedupBitmap(Vec<u8>);

impl DedupBitmap {
    fn new() -> Self {
        Self(vec![0u8; BITMAP_BYTES])
    }

    fn mark_if_new(&mut self, idx: u32) -> bool {
        let (byte, bit) = ((idx >> 3) as usize, 1u8 << (idx & 7));
        if self.0[byte] & bit != 0 {
            false
        } else {
            self.0[byte] |= bit;
            true
        }
    }
}

fn map_porte_descricao(codigo: &str) -> &'static str {
    match codigo {
        "00" => "NÃO INFORMADO",
        "01" => "MICRO EMPRESA",
        "03" => "EMPRESA DE PEQUENO PORTE",
        "05" => "DEMAIS",
        _ => "NÃO INFORMADO",
    }
}

fn map_natureza_grupo(first: u8) -> &'static str {
    match first {
        b'1' => "ADMINISTRAÇÃO PÚBLICA",
        b'2' => "ENTIDADES EMPRESARIAIS",
        b'3' => "ENTIDADES SEM FINS LUCRATIVOS",
        b'4' => "PESSOAS FÍSICAS",
        b'5' => "ORGANIZAÇÕES INTERNACIONAIS",
        _ => "OUTROS",
    }
}

fn faixa(capital: f64) -> &'static str {
    if capital == 0.0 {
        "SEM CAPITAL"
    } else if capital <= 1000.0 {
        "ATÉ 1K"
    } else if capital <= 10_000.0 {
        "1K A 10K"
    } else if capital <= 100_000.0 {
        "10K A 100K"
    } else if capital <= 1_000_000.0 {
        "100K A 1M"
    } else {
        "ACIMA DE 1M"
    }
}

/// LPAD(str, 8, '0') — só preenche à esquerda se mais curto; não trunca.
fn lpad8(raw: &[u8], out: &mut [u8; 16]) -> usize {
    if raw.len() >= 8 {
        let n = raw.len().min(16);
        out[..n].copy_from_slice(&raw[..n]);
        n
    } else {
        let pad = 8 - raw.len();
        for b in out.iter_mut().take(pad) {
            *b = b'0';
        }
        out[pad..8].copy_from_slice(raw);
        8
    }
}

/// Escreve um campo no formato CSV do Postgres COPY (quota apenas se necessário).
fn write_csv_field(buf: &mut Vec<u8>, s: &str) {
    if s.bytes().any(|b| b == b';' || b == b'"' || b == b'\n' || b == b'\r') {
        buf.push(b'"');
        for ch in s.bytes() {
            if ch == b'"' {
                buf.push(b'"');
            }
            buf.push(ch);
        }
        buf.push(b'"');
    } else {
        buf.extend_from_slice(s.as_bytes());
    }
}

/// bytes latin-1 -> ASCII já são idênticos a utf-8 pra dígitos/códigos; usado
/// só para os campos numéricos/código onde não há acentuação possível.
fn ascii_str(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).unwrap_or("")
}

fn parse_capital_social(raw: &[u8], scratch: &mut String) -> f64 {
    scratch.clear();
    if raw.is_empty() {
        scratch.push('0');
    } else {
        for &b in raw {
            match b {
                b'.' => {}
                b',' => scratch.push('.'),
                _ => scratch.push(b as char),
            }
        }
    }
    scratch.parse::<f64>().unwrap_or(0.0)
}

struct RowCounters {
    rows: u64,
    removed: u64,
    rejected: u64,
}

/// Processa um CSV (via qualquer Read, inclusive direto da entry do ZIP —
/// sem materializar em disco) e escreve o resultado deduplicado no CopyInWriter.
fn process_csv<R: std::io::Read, W: Write>(
    reader: R,
    out: &mut W,
    bitmap: &mut DedupBitmap,
    label: &str,
    data_processamento: &str,
) -> Result<RowCounters, Box<dyn std::error::Error>> {
    let mut csv_reader = csv::ReaderBuilder::new()
        .delimiter(b';')
        .quote(b'"')
        .has_headers(false)
        .flexible(true)
        .from_reader(reader);

    let mut counters = RowCounters { rows: 0, removed: 0, rejected: 0 };
    let mut row_buf: Vec<u8> = Vec::with_capacity(512);
    let mut num_scratch = String::with_capacity(32);
    let mut record = csv::ByteRecord::new();

    while csv_reader.read_byte_record(&mut record)? {
        if record.len() < 7 {
            counters.rejected += 1;
            continue;
        }

        let mut cnpj_buf = [0u8; 16];
        let cnpj_len = lpad8(record.get(0).unwrap_or(b""), &mut cnpj_buf);
        let cnpj_str = ascii_str(&cnpj_buf[..cnpj_len]);
        let idx_str = ascii_str(&cnpj_buf[..8.min(cnpj_len)]);
        let idx: u32 = match idx_str.parse() {
            Ok(v) => v,
            Err(_) => {
                counters.rejected += 1;
                continue;
            }
        };

        let razao_cow = encoding_rs::mem::decode_latin1(record.get(1).unwrap_or(b""));
        let razao_social = razao_cow.trim().to_uppercase();

        let natureza_juridica = ascii_str(record.get(2).unwrap_or(b""));
        let qualificacao = ascii_str(record.get(3).unwrap_or(b""));

        let capital_social = parse_capital_social(record.get(4).unwrap_or(b""), &mut num_scratch);

        let porte_raw = ascii_str(record.get(5).unwrap_or(b""));
        let porte_codigo = if porte_raw.is_empty() { "00" } else { porte_raw };
        let porte_descricao = map_porte_descricao(porte_codigo);

        let ente_raw = record.get(6).unwrap_or(b"");
        let ente_federativo = if ente_raw.is_empty() {
            None
        } else {
            Some(encoding_rs::mem::decode_latin1(ente_raw))
        };

        let capital_social_faixa = faixa(capital_social);

        let is_mei = {
            let bytes = razao_social.as_bytes();
            bytes.len() >= 11
                && bytes[bytes.len() - 11..].iter().all(|b| b.is_ascii_digit())
        };

        let natureza_grupo = map_natureza_grupo(natureza_juridica.as_bytes().first().copied().unwrap_or(0));
        let ente_presente = ente_federativo.is_some();

        // ── Bitmap dedup: descarta se cnpj_basico já visto (intra e inter-ZIP) ──
        if !bitmap.mark_if_new(idx) {
            counters.removed += 1;
            continue;
        }

        // ── Monta a linha CSV de saída (buffer reutilizado, sem alocar por linha) ──
        row_buf.clear();
        write_csv_field(&mut row_buf, cnpj_str);
        row_buf.push(b';');
        write_csv_field(&mut row_buf, &razao_social);
        row_buf.push(b';');
        write_csv_field(&mut row_buf, natureza_juridica);
        row_buf.push(b';');
        write_csv_field(&mut row_buf, qualificacao);
        row_buf.push(b';');
        write!(row_buf, "{capital_social}")?;
        row_buf.push(b';');
        write_csv_field(&mut row_buf, porte_codigo);
        row_buf.push(b';');
        write_csv_field(&mut row_buf, porte_descricao);
        row_buf.push(b';');
        if let Some(ref e) = ente_federativo {
            write_csv_field(&mut row_buf, e);
        }
        row_buf.push(b';');
        write_csv_field(&mut row_buf, capital_social_faixa);
        row_buf.push(b';');
        row_buf.extend_from_slice(if is_mei { b"true" } else { b"false" });
        row_buf.push(b';');
        write_csv_field(&mut row_buf, natureza_grupo);
        row_buf.push(b';');
        row_buf.extend_from_slice(if ente_presente { b"true" } else { b"false" });
        row_buf.push(b';');
        row_buf.extend_from_slice(data_processamento.as_bytes());
        row_buf.push(b'\n');

        out.write_all(&row_buf)?;
        counters.rows += 1;
    }

    if counters.rejected > 0 {
        println!("  [WARN] {label}: {} linha(s) rejeitada(s) (malformadas).", counters.rejected);
    }
    if counters.removed > 0 {
        println!("  [DEDUP] {label}: {} duplicata(s) de cnpj_basico filtrada(s).", counters.removed);
    }
    println!("  [COPY] {label}: {} linhas", counters.rows);

    Ok(counters)
}

fn process_zip(
    zip_path: &Path,
    client: &mut Client,
    table: &str,
    bitmap: &mut DedupBitmap,
) -> Result<u64, Box<dyn std::error::Error>> {
    let basename = zip_path.file_name().unwrap().to_string_lossy().to_string();
    println!("[ZIP] Processando {basename} …");

    let file = File::open(zip_path)?;
    let mut archive = zip::ZipArchive::new(BufReader::new(file))?;

    let data_processamento = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.6f").to_string();

    let mut grand_total = 0u64;
    let n_files = archive.len();
    println!("  Arquivos no ZIP: {n_files}");

    for i in 0..n_files {
        let entry = archive.by_index(i)?;
        if entry.is_dir() {
            continue;
        }
        let fname = entry.name().to_string();
        println!("  [CSV] Processando {fname} …");

        let statement = format!("COPY {table} FROM STDIN (FORMAT CSV, DELIMITER ';', NULL '')");
        let mut writer = client.copy_in(statement.as_str())?;
        let counters = process_csv(entry, &mut writer, bitmap, &format!("{basename}/{fname}"), &data_processamento)?;
        writer.finish()?;

        grand_total += counters.rows;
        println!("  [CSV] {fname}: {} linhas", counters.rows);
    }

    println!("[ZIP] {basename} concluído — {grand_total} linhas.");
    Ok(grand_total)
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let participante = get_env_or_fail("PARTICIPANTE");
    let pg_table = get_env_or_fail("PG_TABLE");
    let pg_host = get_env_or_fail("PG_HOST");
    let pg_port = get_env_or_fail("PG_PORT");
    let pg_user = get_env_or_fail("PG_USER");
    let pg_password = get_env_or_fail("PG_PASSWORD");
    let pg_db = get_env_or_fail("PG_DB");

    println!("Iniciando pipeline para participante: {participante}");
    println!("Tabela alvo: {pg_table}");
    println!("PostgreSQL: {pg_host}:{pg_port}/{pg_db} usuário: {pg_user}");

    let conn_str = format!(
        "host={pg_host} port={pg_port} user={pg_user} password={pg_password} dbname={pg_db}"
    );
    let mut client = Client::connect(&conn_str, NoTls)?;
    println!("[PG] Conexão estabelecida.");

    create_table(&mut client, &pg_table)?;
    client.batch_execute("SET synchronous_commit = off")?;

    let zip_list = list_zips(ZIP_DIR);
    if zip_list.is_empty() {
        println!("[ERRO] Nenhum arquivo ZIP encontrado em {ZIP_DIR}/");
        return Ok(());
    }
    println!("[PIPELINE] {} ZIP(s) encontrados.", zip_list.len());

    let start = std::time::Instant::now();
    let mut bitmap = DedupBitmap::new(); // 12,5 MB — única alocação de vida longa
    let mut grand_total = 0u64;

    for (i, zp) in zip_list.iter().enumerate() {
        let t0 = std::time::Instant::now();
        let rows = process_zip(zp, &mut client, &pg_table, &mut bitmap)?;
        let elapsed = t0.elapsed().as_secs_f64();
        grand_total += rows;
        let rate = if elapsed > 0.0 { rows as f64 / elapsed } else { 0.0 };
        println!(
            "  [FIM] ZIP {}/{}: {} — {rows} linhas em {elapsed:.1}s (rate: {rate:.0} linhas/s)",
            i + 1,
            zip_list.len(),
            zp.file_name().unwrap().to_string_lossy()
        );
    }

    let elapsed_total = start.elapsed().as_secs_f64();
    println!("\n[PIPELINE] Total: {grand_total} linhas em {elapsed_total:.1}s.");
    println!("[RESULTADO] ✅ Pipeline concluído com sucesso.");
    println!("[RESULTADO] Total de linhas: {grand_total}");

    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::from(0),
        Err(e) => {
            println!("[FATAL] Erro no pipeline: {e}");
            ExitCode::from(1)
        }
    }
}
