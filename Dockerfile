FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Diretório para spill do DuckDB (filesystem regular, não tmpfs)
RUN mkdir -p /app/duckdb_temp

# Instalar dependências de sistema
RUN apt-get update && apt-get install -y --no-install-recommends unzip && \
    rm -rf /var/lib/apt/lists/*

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código fonte
COPY src/ .

# Pipeline entry point
CMD ["python", "main.py"]
