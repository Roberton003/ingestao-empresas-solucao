FROM golang:1.22-bookworm AS builder
WORKDIR /build
COPY go.mod ./
COPY main.go ./
RUN go mod tidy
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /build/ingestao-empresas .

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=builder /build/ingestao-empresas /app/ingestao-empresas
ENV GOMEMLIMIT=24MiB
CMD ["/app/ingestao-empresas"]
