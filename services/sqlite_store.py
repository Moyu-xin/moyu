from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("CHATGPT2API_DATA_DIR") or (BASE_DIR / "data")).expanduser().resolve()
DATABASE_FILE = Path(os.getenv("CHATGPT2API_SQLITE_PATH") or (DATA_DIR / "moyu.sqlite3")).expanduser().resolve()
STORAGE_BACKEND = str(os.getenv("STORAGE_BACKEND") or "sqlite").strip().lower()

_lock = threading.RLock()
_schema_ready_for: set[str] = set()

_LIST_COLLECTIONS = {
    "registered_accounts.json": "registered_accounts",
    "partial_accounts.json": "partial_accounts",
    "shipping_accounts.json": "shipping_accounts",
}
_WRAPPED_COLLECTIONS = {
    "mailbox_pool.json": ("mailboxes", "items"),
}
_MAP_COLLECTIONS = {
    "outlook_token_used.json": "outlook_token_states",
}


def sqlite_enabled() -> bool:
    return STORAGE_BACKEND in {"sqlite", "sqlite3"}


def should_use_sqlite(path: Path | str) -> bool:
    if not sqlite_enabled():
        return False
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != ".json":
        return False
    try:
        target.relative_to(DATA_DIR)
    except ValueError:
        return False
    return target != DATABASE_FILE


def _document_key(path: Path | str) -> str:
    target = Path(path).expanduser().resolve()
    try:
        return target.relative_to(DATA_DIR).as_posix().lower()
    except ValueError:
        return str(target).lower()


def _connect() -> sqlite3.Connection:
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_FILE, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    key = str(DATABASE_FILE).lower()
    if key in _schema_ready_for:
        return
    with _lock:
        if key in _schema_ready_for:
            return
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_documents (
                document_key TEXT PRIMARY KEY,
                schema_version INTEGER,
                shape TEXT NOT NULL DEFAULT 'document',
                collection_name TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                updated_ns INTEGER NOT NULL,
                payload_bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_items (
                collection_name TEXT NOT NULL,
                item_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                provider_type TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                registration_batch TEXT NOT NULL DEFAULT '',
                outbound_at TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                PRIMARY KEY (collection_name, item_key)
            );
            CREATE INDEX IF NOT EXISTS idx_collection_position
                ON collection_items(collection_name, position);
            CREATE INDEX IF NOT EXISTS idx_collection_email
                ON collection_items(collection_name, email);
            CREATE INDEX IF NOT EXISTS idx_collection_batch
                ON collection_items(collection_name, registration_batch, position);
            CREATE INDEX IF NOT EXISTS idx_collection_outbound
                ON collection_items(collection_name, outbound_at, position);
            CREATE TABLE IF NOT EXISTS storage_meta (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO storage_meta(meta_key, meta_value) VALUES('schema_version', '1')"
        )
        _schema_ready_for.add(key)


def _wrapped_payload(data: Any, schema_version: int | None) -> Any:
    if schema_version is None:
        return data
    if isinstance(data, dict):
        return {**data, "schema_version": int(schema_version)}
    if isinstance(data, list):
        return {"schema_version": int(schema_version), "items": data}
    return {"schema_version": int(schema_version), "data": data}


def _batch_value(item: dict[str, Any]) -> str:
    explicit = str(
        item.get("registration_batch_id")
        or item.get("registration_batch")
        or item.get("batch_id")
        or ""
    ).strip()
    if explicit:
        return explicit
    created = str(item.get("registered_at") or item.get("created_at") or "").strip()
    return f"legacy-{created[:10]}" if len(created) >= 10 else "legacy-unknown"


def _item_key(collection: str, item: Any, position: int, map_key: str = "") -> str:
    if map_key:
        base = map_key.strip().lower()
    elif isinstance(item, dict):
        email = str(item.get("email") or item.get("address") or "").strip().lower()
        scope = str(
            item.get("chatgpt_account_id")
            or item.get("workspace_id")
            or item.get("k12_team_id")
            or item.get("organization_id")
            or ""
        ).strip().lower()
        platform = str(item.get("platform") or "").strip().lower()
        provider = str(item.get("provider_type") or "").strip().lower()
        explicit = str(item.get("inventory_id") or item.get("id") or "").strip().lower()
        base = explicit or "::".join(part for part in (platform, provider, email, scope) if part)
    else:
        base = ""
    if not base:
        base = f"row-{position}"
    return hashlib.sha256(f"{collection}:{base}".encode("utf-8", errors="ignore")).hexdigest()[:32]


def _collection_descriptor(path: Path, payload: Any) -> tuple[str, str, Any, list[tuple[str, Any]]] | None:
    name = path.name.lower()
    if name in _LIST_COLLECTIONS:
        collection = _LIST_COLLECTIONS[name]
        items = payload.get("items") if isinstance(payload, dict) else payload
        items = items if isinstance(items, list) else []
        meta = {key: value for key, value in payload.items() if key != "items"} if isinstance(payload, dict) else {}
        return "list", collection, meta, [("", item) for item in items]
    if name in _WRAPPED_COLLECTIONS and isinstance(payload, dict):
        collection, item_field = _WRAPPED_COLLECTIONS[name]
        items = payload.get(item_field) if isinstance(payload.get(item_field), list) else []
        meta = {key: value for key, value in payload.items() if key != item_field}
        meta["_sqlite_item_field"] = item_field
        return "wrapped", collection, meta, [("", item) for item in items]
    if name in _MAP_COLLECTIONS and isinstance(payload, dict):
        collection = _MAP_COLLECTIONS[name]
        schema = payload.get("schema_version")
        meta = {"schema_version": schema} if schema is not None else {}
        entries = [(str(key), value) for key, value in payload.items() if key != "schema_version"]
        return "map", collection, meta, entries
    return None


def write_document(path: Path | str, data: Any, *, schema_version: int | None = None) -> None:
    target = Path(path).expanduser().resolve()
    payload = _wrapped_payload(data, schema_version)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    descriptor = _collection_descriptor(target, payload)
    shape = descriptor[0] if descriptor else "document"
    collection = descriptor[1] if descriptor else ""
    meta = descriptor[2] if descriptor else payload
    rows = descriptor[3] if descriptor else []
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    updated_ns = time.time_ns()
    document_key = _document_key(target)
    stored_schema_version = schema_version
    if stored_schema_version is None and isinstance(payload, dict) and payload.get("schema_version") is not None:
        try:
            stored_schema_version = int(payload["schema_version"])
        except (TypeError, ValueError):
            stored_schema_version = None

    with _lock, closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if collection:
                connection.execute("DELETE FROM collection_items WHERE collection_name = ?", (collection,))
                records: list[tuple[Any, ...]] = []
                seen_keys: set[str] = set()
                for position, (map_key, item) in enumerate(rows):
                    item_key = _item_key(collection, item, position, map_key)
                    if item_key in seen_keys:
                        item_key = f"{item_key}-{position}"
                    seen_keys.add(item_key)
                    item_dict = item if isinstance(item, dict) else {}
                    records.append(
                        (
                            collection,
                            item_key,
                            position,
                            str(item_dict.get("email") or item_dict.get("address") or map_key or "").strip().lower(),
                            str(item_dict.get("provider_type") or "").strip().lower(),
                            str(item_dict.get("group") or item_dict.get("mailbox_group") or "").strip(),
                            str(item_dict.get("created_at") or item_dict.get("registered_at") or "").strip(),
                            _batch_value(item_dict) if item_dict else "",
                            str(item_dict.get("outbound_at") or "").strip(),
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                if records:
                    connection.executemany(
                        """
                        INSERT INTO collection_items(
                            collection_name, item_key, position, email, provider_type, group_name,
                            created_at, registration_batch, outbound_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        records,
                    )
            connection.execute(
                """
                INSERT INTO app_documents(
                    document_key, schema_version, shape, collection_name,
                    payload_json, updated_ns, payload_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_key) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    shape=excluded.shape,
                    collection_name=excluded.collection_name,
                    payload_json=excluded.payload_json,
                    updated_ns=excluded.updated_ns,
                    payload_bytes=excluded.payload_bytes
                """,
                (
                    document_key,
                    stored_schema_version,
                    shape,
                    collection,
                    meta_json,
                    updated_ns,
                    len(serialized.encode("utf-8")),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _read_row(connection: sqlite3.Connection, document_key: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM app_documents WHERE document_key = ?", (document_key,)
    ).fetchone()


def has_document(path: Path | str) -> bool:
    if not should_use_sqlite(path) or not DATABASE_FILE.exists():
        return False
    with _lock, closing(_connect()) as connection:
        return _read_row(connection, _document_key(path)) is not None


def read_document(path: Path | str, *, expected_schema_version: int | None = None) -> Any:
    with _lock, closing(_connect()) as connection:
        row = _read_row(connection, _document_key(path))
        if row is None:
            raise KeyError(_document_key(path))
        version = row["schema_version"]
        if expected_schema_version is not None and version is not None and int(version) > int(expected_schema_version):
            raise RuntimeError(
                f"{Path(path).name} schema_version={version} is newer than supported version {expected_schema_version}"
            )
        payload = json.loads(row["payload_json"])
        shape = str(row["shape"] or "document")
        collection = str(row["collection_name"] or "")
        if shape == "document" or not collection:
            return payload
        item_rows = connection.execute(
            "SELECT email, payload_json FROM collection_items WHERE collection_name = ? ORDER BY position",
            (collection,),
        ).fetchall()
        if shape == "map":
            result = dict(payload) if isinstance(payload, dict) else {}
            for item_row in item_rows:
                result[str(item_row["email"])] = json.loads(item_row["payload_json"])
            return result
        items = [json.loads(item_row["payload_json"]) for item_row in item_rows]
        if shape == "wrapped":
            result = dict(payload) if isinstance(payload, dict) else {}
            field = str(result.pop("_sqlite_item_field", "items") or "items")
            result[field] = items
            return result
        result = dict(payload) if isinstance(payload, dict) else {}
        result["items"] = items
        return result


def collection_page(path: Path | str, *, limit: int, offset: int = 0) -> tuple[list[Any], int] | None:
    if not has_document(path):
        return None
    with _lock, closing(_connect()) as connection:
        row = _read_row(connection, _document_key(path))
        if row is None or not row["collection_name"]:
            return None
        collection = str(row["collection_name"])
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM collection_items WHERE collection_name = ?", (collection,)
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT payload_json FROM collection_items
            WHERE collection_name = ? ORDER BY position LIMIT ? OFFSET ?
            """,
            (collection, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows], total


def document_signature(path: Path | str) -> tuple[str, int, int]:
    target = Path(path).expanduser().resolve()
    if should_use_sqlite(target) and DATABASE_FILE.exists():
        with _lock, closing(_connect()) as connection:
            row = connection.execute(
                "SELECT updated_ns, payload_bytes FROM app_documents WHERE document_key = ?",
                (_document_key(target),),
            ).fetchone()
            if row is not None:
                return str(target), int(row["updated_ns"]), int(row["payload_bytes"])
    try:
        stat = target.stat()
        return str(target), int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return str(target), 0, 0


def preserve_json_migration_source(path: Path | str) -> None:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return
    backup = target.with_suffix(target.suffix + ".pre-sqlite.bak")
    if not backup.exists():
        shutil.copy2(target, backup)


def migrate_data_directory() -> dict[str, Any]:
    result: dict[str, Any] = {"migrated": 0, "existing": 0, "failed": 0, "errors": []}
    if not sqlite_enabled() or not DATA_DIR.exists():
        return result
    for target in sorted(DATA_DIR.glob("*.json")):
        if not should_use_sqlite(target):
            continue
        if has_document(target):
            result["existing"] += 1
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            preserve_json_migration_source(target)
            write_document(target, payload)
            result["migrated"] += 1
        except Exception as error:
            result["failed"] += 1
            result["errors"].append({"file": target.name, "error": str(error)[:300]})
    return result


def storage_status() -> dict[str, Any]:
    document_count = 0
    collection_count = 0
    if DATABASE_FILE.exists():
        with _lock, closing(_connect()) as connection:
            document_count = int(connection.execute("SELECT COUNT(*) FROM app_documents").fetchone()[0])
            collection_count = int(connection.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0])
    return {
        "backend": "sqlite" if sqlite_enabled() else "json",
        "database": str(DATABASE_FILE),
        "database_exists": DATABASE_FILE.exists(),
        "database_bytes": DATABASE_FILE.stat().st_size if DATABASE_FILE.exists() else 0,
        "documents": document_count,
        "collection_items": collection_count,
    }
