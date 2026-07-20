from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}
_cache_guard = threading.Lock()
_json_cache: dict[str, tuple[tuple[int, int], Any]] = {}


class JsonStoreReadError(RuntimeError):
    pass


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


def _cache_key(path: Path) -> str:
    return str(path.resolve()).lower()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_mtime_ns), int(stat.st_size)


def _invalidate_cache(path: Path) -> None:
    with _cache_guard:
        _json_cache.pop(_cache_key(path), None)


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(
    path: Path | str,
    data: Any,
    *,
    indent: int | None = 2,
    default: Callable[[Any], Any] | None = None,
    backup: bool = True,
    schema_version: int | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data
    if schema_version is not None:
        if isinstance(data, dict):
            payload = {**data, "schema_version": int(schema_version)}
        elif isinstance(data, list):
            payload = {"schema_version": int(schema_version), "items": data}
        else:
            payload = {"schema_version": int(schema_version), "data": data}
    content = (json.dumps(payload, ensure_ascii=False, indent=indent, default=default) + "\n").encode("utf-8")
    lock = _lock_for(target)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    backup_path = target.with_suffix(target.suffix + ".bak")
    backup_temp = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")

    with lock:
        try:
            if backup and target.exists():
                _write_bytes(backup_temp, target.read_bytes())
                os.replace(backup_temp, backup_path)
            _write_bytes(temp, content)
            os.replace(temp, target)
            _invalidate_cache(target)
        finally:
            temp.unlink(missing_ok=True)
            backup_temp.unlink(missing_ok=True)


def read_json(
    path: Path | str,
    default: Any,
    *,
    expected_schema_version: int | None = None,
    restore_backup: bool = True,
) -> Any:
    target = Path(path)
    if not target.exists():
        return deepcopy(default)
    backup_path = target.with_suffix(target.suffix + ".bak")

    def parse(source: Path) -> Any:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if expected_schema_version is not None and isinstance(data, dict):
            version = data.get("schema_version")
            if version is not None and int(version) > int(expected_schema_version):
                raise JsonStoreReadError(
                    f"{source.name} schema_version={version} is newer than supported version {expected_schema_version}"
                )
        return data

    lock = _lock_for(target)
    with lock:
        try:
            signature = _signature(target)
            cache_key = _cache_key(target)
            with _cache_guard:
                cached = _json_cache.get(cache_key)
            if cached and cached[0] == signature:
                data = deepcopy(cached[1])
                if expected_schema_version is not None and isinstance(data, dict):
                    version = data.get("schema_version")
                    if version is not None and int(version) > int(expected_schema_version):
                        raise JsonStoreReadError(
                            f"{target.name} schema_version={version} is newer than supported version {expected_schema_version}"
                        )
                return data
            data = parse(target)
            if expected_schema_version is not None and (not isinstance(data, dict) or data.get("schema_version") is None):
                write_json(target, data, schema_version=expected_schema_version)
            else:
                with _cache_guard:
                    _json_cache[cache_key] = (signature, deepcopy(data))
            return data
        except JsonStoreReadError:
            # A newer schema is valid data that this version cannot interpret.
            # Falling back to an older backup would silently downgrade it.
            raise
        except Exception as primary_error:
            if backup_path.exists():
                try:
                    recovered = parse(backup_path)
                except Exception as backup_error:
                    raise JsonStoreReadError(
                        f"JSON state is unreadable: {target}; primary={primary_error}; backup={backup_error}"
                    ) from backup_error
                if restore_backup:
                    schema_version = None
                    if expected_schema_version is not None and (not isinstance(recovered, dict) or recovered.get("schema_version") is None):
                        schema_version = expected_schema_version
                    write_json(target, recovered, backup=False, schema_version=schema_version)
                return recovered
            raise JsonStoreReadError(f"JSON state is unreadable and has no valid backup: {target}; {primary_error}") from primary_error
