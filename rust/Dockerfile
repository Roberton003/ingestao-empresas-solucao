FROM rust:1-slim AS builder
WORKDIR /build
COPY Cargo.toml Cargo.lock* ./
COPY src/ src/
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev && \
    rm -rf /var/lib/apt/lists/*
RUN cargo build --release

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=builder /build/target/release/ingestao-empresas /app/ingestao-empresas
CMD ["/app/ingestao-empresas"]
