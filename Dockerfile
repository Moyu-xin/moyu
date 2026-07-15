FROM node:26-bookworm-slim AS web-build
WORKDIR /src
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml web/.npmrc ./web/
RUN cd web && pnpm install --frozen-lockfile
COPY VERSION ./VERSION
COPY web ./web
RUN cd web && pnpm build

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORAGE_BACKEND=json \
    CHATGPT2API_HOST=0.0.0.0 \
    CHATGPT2API_PORT=8000 \
    CHATGPT2API_CONFIG_FILE=/app/runtime/config.json \
    CHATGPT2API_DATA_DIR=/app/runtime/data \
    CHATGPT2API_LOG_DIR=/app/runtime/logs
WORKDIR /app
COPY pyproject.toml README.md VERSION config.example.json main.py serve_local.py ./
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /src/web/out ./web_dist
RUN pip install --no-cache-dir .
EXPOSE 8000
VOLUME ["/app/runtime"]
ENTRYPOINT ["python", "scripts/docker_entrypoint.py"]
CMD ["python", "serve_local.py"]
