FROM ghcr.io/astral-sh/uv:0.11.24 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY pigeon ./pigeon
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["pigeon", "relay", "serve", "--db", "/data/relay.sqlite3", "--host", "0.0.0.0", "--port", "8080", "--behind-tls-proxy"]
