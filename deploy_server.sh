#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine with the Compose plugin first." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "The Docker Compose plugin is required." >&2
  exit 1
fi

mkdir -p runtime/data runtime/logs
docker compose up -d --build

echo "Moyu 1.0 is running at http://127.0.0.1:${CHATGPT2API_PORT:-8000}/register/"
echo "Initial admin key: ${CHATGPT2API_INITIAL_AUTH_KEY:-moyu666}"
echo "Change the key in System Settings after the first login."
