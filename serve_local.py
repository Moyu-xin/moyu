from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEPS = ROOT / ".deps"
LOG_DIR = Path(os.getenv("CHATGPT2API_LOG_DIR") or (ROOT / "logs")).expanduser().resolve()
HOST = str(os.getenv("CHATGPT2API_HOST") or "127.0.0.1").strip()
try:
    PORT = int(os.getenv("CHATGPT2API_PORT") or "8000")
except ValueError:
    PORT = 8000

os.environ.setdefault("STORAGE_BACKEND", "sqlite")
os.environ.setdefault("CHATGPT2API_AUTO_RESUME_PLUS_TASKS", "1")
sys.path.insert(0, str(DEPS))

LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.stdout = open(LOG_DIR / "local-server.out.log", "a", encoding="utf-8", buffering=1)
sys.stderr = open(LOG_DIR / "local-server.err.log", "a", encoding="utf-8", buffering=1)

print(f"Starting Moyu on http://{HOST}:{PORT}/register/", flush=True)

import uvicorn  # noqa: E402


uvicorn.run(
    "main:app",
    host=HOST,
    port=PORT,
    access_log=True,
)
