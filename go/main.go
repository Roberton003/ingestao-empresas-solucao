// Reescrita em Go do pipeline de ingestão (ZIP -> CSV latin-1 -> Postgres COPY).
//
// Contrato idêntico a src/main.py / rust/src/main.rs: mesmas env vars, mesmo
// DDL, mesma derivação, mesmo dedup por bitmap de cnpj_basico (10^8 bits).
//
// Único estado de vida longa é o bitmap (12,5 MB) e os buffers reutilizados
// no loop por linha (csv.Reader.ReuseRecord + row buffer reciclado via [:0]) —
// nada acumula na heap conforme o volume processado cresce.
package main

import (
	"archive/zip"
	"bufio"
	"context"
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"golang.org/x/text/encoding/charmap"
	"golang.org/x/text/transform"
)

const bitmapBytes = 100_000_000 / 8
const zipDir = "/data"

const ddl = `
CREATE TABLE IF NOT EXISTS %s (
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
`

func getEnvOrFail(key string) string {
	v := os.Getenv(key)
	if v == "" {
		fmt.Printf("[FATAL] Variável de ambiente %s não definida.\n", key)
		os.Exit(1)
	}
	return v
}

// Bitmap fixo de 10^8 bits (12,5 MB) — 1 alocação para o pipeline inteiro.
type dedupBitmap []byte

func newDedupBitmap() dedupBitmap {
	return make(dedupBitmap, bitmapBytes)
}

func (b dedupBitmap) markIfNew(idx uint32) bool {
	byteIdx, bit := idx>>3, byte(1<<(idx&7))
	if b[byteIdx]&bit != 0 {
		return false
	}
	b[byteIdx] |= bit
	return true
}

func portDescricao(codigo string) string {
	switch codigo {
	case "00":
		return "NÃO INFORMADO"
	case "01":
		return "MICRO EMPRESA"
	case "03":
		return "EMPRESA DE PEQUENO PORTE"
	case "05":
		return "DEMAIS"
	default:
		return "NÃO INFORMADO"
	}
}

func naturezaGrupo(first byte) string {
	switch first {
	case '1':
		return "ADMINISTRAÇÃO PÚBLICA"
	case '2':
		return "ENTIDADES EMPRESARIAIS"
	case '3':
		return "ENTIDADES SEM FINS LUCRATIVOS"
	case '4':
		return "PESSOAS FÍSICAS"
	case '5':
		return "ORGANIZAÇÕES INTERNACIONAIS"
	default:
		return "OUTROS"
	}
}

func faixa(capital float64) string {
	switch {
	case capital == 0:
		return "SEM CAPITAL"
	case capital <= 1000:
		return "ATÉ 1K"
	case capital <= 10_000:
		return "1K A 10K"
	case capital <= 100_000:
		return "10K A 100K"
	case capital <= 1_000_000:
		return "100K A 1M"
	default:
		return "ACIMA DE 1M"
	}
}

// LPAD(str, 8, '0') — só preenche à esquerda se mais curto; não trunca.
func lpad8(raw string, out []byte) []byte {
	if len(raw) >= 8 {
		return append(out[:0], raw...)
	}
	out = out[:0]
	for i := 0; i < 8-len(raw); i++ {
		out = append(out, '0')
	}
	return append(out, raw...)
}

func writeCSVField(buf []byte, s string) []byte {
	needsQuote := strings.ContainsAny(s, ";\"\n\r")
	if !needsQuote {
		return append(buf, s...)
	}
	buf = append(buf, '"')
	for i := 0; i < len(s); i++ {
		if s[i] == '"' {
			buf = append(buf, '"')
		}
		buf = append(buf, s[i])
	}
	return append(buf, '"')
}

func parseCapitalSocial(raw string, scratch []byte) (float64, []byte) {
	scratch = scratch[:0]
	if raw == "" {
		scratch = append(scratch, '0')
	} else {
		for i := 0; i < len(raw); i++ {
			switch raw[i] {
			case '.':
			case ',':
				scratch = append(scratch, '.')
			default:
				scratch = append(scratch, raw[i])
			}
		}
	}
	v, err := strconv.ParseFloat(string(scratch), 64)
	if err != nil {
		v = 0.0
	}
	return v, scratch
}

func listZips(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		fmt.Printf("[WARN] Diretório %s não encontrado. Usando lista vazia.\n", dir)
		return nil
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if strings.EqualFold(filepath.Ext(e.Name()), ".zip") {
			out = append(out, filepath.Join(dir, e.Name()))
		}
	}
	sort.Strings(out)
	return out
}

type rowCounters struct {
	rows, removed, rejected int64
}

// Processa um CSV via qualquer io.Reader (direto da entry do ZIP, sem
// materializar em disco) e escreve o CSV deduplicado em w.
func processCSV(r io.Reader, w io.Writer, bitmap dedupBitmap, label, dataProcessamento string) (rowCounters, error) {
	latin1Reader := transform.NewReader(r, charmap.ISO8859_1.NewDecoder())
	reader := csv.NewReader(bufio.NewReaderSize(latin1Reader, 1<<16))
	reader.Comma = ';'
	reader.FieldsPerRecord = -1
	reader.LazyQuotes = false
	reader.ReuseRecord = true

	var counters rowCounters
	rowBuf := make([]byte, 0, 512)
	scratch := make([]byte, 0, 32)
	cnpjBuf := make([]byte, 0, 16)
	bw := bufio.NewWriterSize(w, 1<<16)

	for {
		record, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			counters.rejected++
			continue
		}
		if len(record) < 7 {
			counters.rejected++
			continue
		}

		cnpjField := lpad8(record[0], cnpjBuf)
		cnpjBuf = cnpjField
		cnpjStr := string(cnpjField)
		idxLen := len(cnpjField)
		if idxLen > 8 {
			idxLen = 8
		}
		idx64, err := strconv.ParseUint(string(cnpjField[:idxLen]), 10, 32)
		if err != nil {
			counters.rejected++
			continue
		}
		idx := uint32(idx64)

		razaoSocial := strings.ToUpper(strings.TrimSpace(record[1]))
		naturezaJuridica := record[2]
		qualificacao := record[3]

		capitalSocial, newScratch := parseCapitalSocial(record[4], scratch)
		scratch = newScratch

		porteCodigo := record[5]
		if porteCodigo == "" {
			porteCodigo = "00"
		}
		porteDesc := portDescricao(porteCodigo)

		enteFederativo := record[6]
		entePresente := enteFederativo != ""

		capitalFaixa := faixa(capitalSocial)

		isMei := false
		if len(razaoSocial) >= 11 {
			tail := razaoSocial[len(razaoSocial)-11:]
			isMei = true
			for i := 0; i < len(tail); i++ {
				if tail[i] < '0' || tail[i] > '9' {
					isMei = false
					break
				}
			}
		}

		var naturezaFirst byte
		if len(naturezaJuridica) > 0 {
			naturezaFirst = naturezaJuridica[0]
		}
		naturezaGrupoVal := naturezaGrupo(naturezaFirst)

		if !bitmap.markIfNew(idx) {
			counters.removed++
			continue
		}

		rowBuf = rowBuf[:0]
		rowBuf = writeCSVField(rowBuf, cnpjStr)
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, razaoSocial)
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, naturezaJuridica)
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, qualificacao)
		rowBuf = append(rowBuf, ';')
		rowBuf = strconv.AppendFloat(rowBuf, capitalSocial, 'f', -1, 64)
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, porteCodigo)
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, porteDesc)
		rowBuf = append(rowBuf, ';')
		if entePresente {
			rowBuf = writeCSVField(rowBuf, enteFederativo)
		}
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, capitalFaixa)
		rowBuf = append(rowBuf, ';')
		if isMei {
			rowBuf = append(rowBuf, "true"...)
		} else {
			rowBuf = append(rowBuf, "false"...)
		}
		rowBuf = append(rowBuf, ';')
		rowBuf = writeCSVField(rowBuf, naturezaGrupoVal)
		rowBuf = append(rowBuf, ';')
		if entePresente {
			rowBuf = append(rowBuf, "true"...)
		} else {
			rowBuf = append(rowBuf, "false"...)
		}
		rowBuf = append(rowBuf, ';')
		rowBuf = append(rowBuf, dataProcessamento...)
		rowBuf = append(rowBuf, '\n')

		if _, err := bw.Write(rowBuf); err != nil {
			return counters, err
		}
		counters.rows++
	}

	if err := bw.Flush(); err != nil {
		return counters, err
	}

	if counters.rejected > 0 {
		fmt.Printf("  [WARN] %s: %d linha(s) rejeitada(s) (malformadas).\n", label, counters.rejected)
	}
	if counters.removed > 0 {
		fmt.Printf("  [DEDUP] %s: %d duplicata(s) de cnpj_basico filtrada(s).\n", label, counters.removed)
	}
	fmt.Printf("  [COPY] %s: %d linhas\n", label, counters.rows)

	return counters, nil
}

func processZip(ctx context.Context, zipPath string, conn *pgx.Conn, table string, bitmap dedupBitmap) (int64, error) {
	basename := filepath.Base(zipPath)
	fmt.Printf("[ZIP] Processando %s …\n", basename)

	archive, err := zip.OpenReader(zipPath)
	if err != nil {
		return 0, err
	}
	defer archive.Close()

	dataProcessamento := time.Now().Format("2006-01-02 15:04:05.000000")

	var grandTotal int64
	fmt.Printf("  Arquivos no ZIP: %d\n", len(archive.File))

	for _, f := range archive.File {
		if f.FileInfo().IsDir() {
			continue
		}
		fmt.Printf("  [CSV] Processando %s …\n", f.Name)

		entry, err := f.Open()
		if err != nil {
			return grandTotal, err
		}

		pr, pw := io.Pipe()
		label := basename + "/" + f.Name
		var counters rowCounters
		var copyErr error
		done := make(chan struct{})
		go func() {
			defer close(done)
			c, err := processCSV(entry, pw, bitmap, label, dataProcessamento)
			counters = c
			if err != nil {
				pw.CloseWithError(err)
				return
			}
			pw.Close()
		}()

		stmt := fmt.Sprintf("COPY %s FROM STDIN (FORMAT CSV, DELIMITER ';', NULL '')", table)
		_, copyErr = conn.PgConn().CopyFrom(ctx, pr, stmt)
		<-done
		entry.Close()
		if copyErr != nil {
			return grandTotal, copyErr
		}

		grandTotal += counters.rows
		fmt.Printf("  [CSV] %s: %d linhas\n", f.Name, counters.rows)
	}

	fmt.Printf("[ZIP] %s concluído — %d linhas.\n", basename, grandTotal)
	return grandTotal, nil
}

func run() error {
	participante := getEnvOrFail("PARTICIPANTE")
	pgTable := getEnvOrFail("PG_TABLE")
	pgHost := getEnvOrFail("PG_HOST")
	pgPort := getEnvOrFail("PG_PORT")
	pgUser := getEnvOrFail("PG_USER")
	pgPassword := getEnvOrFail("PG_PASSWORD")
	pgDB := getEnvOrFail("PG_DB")

	fmt.Printf("Iniciando pipeline para participante: %s\n", participante)
	fmt.Printf("Tabela alvo: %s\n", pgTable)
	fmt.Printf("PostgreSQL: %s:%s/%s usuário: %s\n", pgHost, pgPort, pgDB, pgUser)

	ctx := context.Background()
	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s", pgHost, pgPort, pgUser, pgPassword, pgDB)
	conn, err := pgx.Connect(ctx, connStr)
	if err != nil {
		return fmt.Errorf("erro ao conectar ao PostgreSQL: %w", err)
	}
	defer conn.Close(ctx)
	fmt.Println("[PG] Conexão estabelecida.")

	if _, err := conn.Exec(ctx, fmt.Sprintf("DROP TABLE IF EXISTS %s", pgTable)); err != nil {
		return err
	}
	if _, err := conn.Exec(ctx, fmt.Sprintf(ddl, pgTable)); err != nil {
		return err
	}
	fmt.Printf("[TABLE] Tabela %s criada/recriada.\n", pgTable)

	if _, err := conn.Exec(ctx, "SET synchronous_commit = off"); err != nil {
		return err
	}

	zipList := listZips(zipDir)
	if len(zipList) == 0 {
		fmt.Printf("[ERRO] Nenhum arquivo ZIP encontrado em %s/\n", zipDir)
		return nil
	}
	fmt.Printf("[PIPELINE] %d ZIP(s) encontrados.\n", len(zipList))

	start := time.Now()
	bitmap := newDedupBitmap() // 12,5 MB — única alocação de vida longa
	var grandTotal int64

	for i, zp := range zipList {
		t0 := time.Now()
		rows, err := processZip(ctx, zp, conn, pgTable, bitmap)
		if err != nil {
			return fmt.Errorf("falha ao processar %s: %w", zp, err)
		}
		elapsed := time.Since(t0).Seconds()
		grandTotal += rows
		rate := 0.0
		if elapsed > 0 {
			rate = float64(rows) / elapsed
		}
		fmt.Printf("  [FIM] ZIP %d/%d: %s — %d linhas em %.1fs (rate: %.0f linhas/s)\n",
			i+1, len(zipList), filepath.Base(zp), rows, elapsed, rate)
	}

	elapsedTotal := time.Since(start).Seconds()
	fmt.Printf("\n[PIPELINE] Total: %d linhas em %.1fs.\n", grandTotal, elapsedTotal)
	fmt.Println("[RESULTADO] ✅ Pipeline concluído com sucesso.")
	fmt.Printf("[RESULTADO] Total de linhas: %d\n", grandTotal)

	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Printf("[FATAL] Erro no pipeline: %v\n", err)
		os.Exit(1)
	}
}
