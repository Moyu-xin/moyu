from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
config_path = Path(os.getenv("CHATGPT2API_CONFIG_FILE") or (ROOT / "config.json")).expanduser()
data_dir = Path(os.getenv("CHATGPT2API_DATA_DIR") or (ROOT / "data")).expanduser()
log_dir = Path(os.getenv("CHATGPT2API_LOG_DIR") or (ROOT / "logs")).expanduser()

data_dir.mkdir(parents=True, exist_ok=True)
log_dir.mkdir(parents=True, exist_ok=True)
config_path.parent.mkdir(parents=True, exist_ok=True)

if not config_path.exists():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
    example["auth-key"] = str(os.getenv("CHATGPT2API_INITIAL_AUTH_KEY") or "moyu666").strip() or "moyu666"
    config_path.write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass

command = sys.argv[1:] or [sys.executable, str(ROOT / "serve_local.py")]
os.execvp(command[0], command)
