from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    root = Path(sys.argv[1]).resolve()
    if not (root / "serve_local.py").is_file():
        return 2
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return 3

    log_file = root / "logs" / "restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def write(message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message.rstrip()}\n")

    def run_script(name: str, timeout: int) -> bool:
        script = root / name
        write(f"Running {name}.")
        with log_file.open("a", encoding="utf-8") as output:
            result = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(root),
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        write(f"{name} exit={result.returncode}")
        return result.returncode == 0

    time.sleep(2)
    write("Restart requested.")
    if not run_script("stop_local.ps1", 60):
        return 4
    write("Waiting for Python process cleanup.")
    time.sleep(5)
    started = False
    for attempt in range(1, 4):
        if run_script("start_local.ps1", 300):
            started = True
            break
        if attempt < 3:
            write(f"Start attempt {attempt} failed; retrying after Python process cleanup.")
            time.sleep(5)
    if not started:
        return 5
    write("Restart completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
