from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.managed_mihomo_service import managed_mihomo_manager


def main() -> None:
    status = managed_mihomo_manager.stop(include_persisted=True)
    print(json.dumps({"ok": True, "running": status.get("running", 0)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
