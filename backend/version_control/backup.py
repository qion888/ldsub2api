"""SQLite backup, integrity, and restore helpers for version operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import VersionControlError


BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BackupManager:
    root: Path
    database_path: Path
    now: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.database_path = Path(self.database_path).resolve()

    @property
    def backup_dir(self) -> Path:
        return self.root / ".runtime" / "version-backups"

    def _validate_id(self, backup_id: Any) -> str:
        value = str(backup_id or "").strip()
        if not BACKUP_ID_PATTERN.fullmatch(value):
            raise VersionControlError(
                "备份标识无效",
                code="invalid_backup_id",
                status=400,
            )
        return value

    def _database_file(self, backup_id: str) -> Path:
        return self.backup_dir / f"{backup_id}.sqlite3"

    def _metadata_file(self, backup_id: str) -> Path:
        return self.backup_dir / f"{backup_id}.json"

    def _write_metadata(self, backup_id: str, metadata: dict[str, Any]) -> None:
        target = self._metadata_file(backup_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def _integrity(self, path: Path) -> dict[str, Any]:
        if not path.exists() or not path.is_file():
            return {
                "exists": False,
                "ok": False,
                "message": "数据库文件不存在",
                "schema_version": None,
            }
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(path), timeout=15)
            row = connection.execute("PRAGMA integrity_check").fetchone()
            result = str(row[0] if row else "").strip().lower()
            schema_row = connection.execute("PRAGMA user_version").fetchone()
            schema_version = int(schema_row[0] if schema_row else 0)
            return {
                "exists": True,
                "ok": result == "ok",
                "message": "SQLite 完整性检查通过" if result == "ok" else result[:160],
                "schema_version": schema_version,
            }
        except (OSError, sqlite3.Error) as exc:
            return {
                "exists": True,
                "ok": False,
                "message": str(exc)[:160],
                "schema_version": None,
            }
        finally:
            if connection is not None:
                connection.close()

    def _copy_database(self, source_path: Path, target_path: Path) -> None:
        source: sqlite3.Connection | None = None
        target: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(str(source_path), timeout=20)
            source.execute("PRAGMA busy_timeout = 20000")
            target = sqlite3.connect(str(target_path), timeout=20)
            source.backup(target, pages=256, sleep=0.05)
            target.commit()
            integrity = self._integrity(target_path)
            if not integrity["ok"]:
                raise VersionControlError(
                    "备份完成后完整性检查未通过",
                    code="backup_integrity_failed",
                    status=500,
                )
        except sqlite3.Error as exc:
            raise VersionControlError(
                "SQLite 数据库正忙，备份未完成",
                code="database_busy",
                status=409,
                retryable=True,
            ) from exc
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()

    def create_backup(
        self,
        *,
        reason: str,
        code_commit: str = "",
        code_version: str = "",
    ) -> dict[str, Any]:
        integrity = self._integrity(self.database_path)
        if not integrity["exists"]:
            raise VersionControlError(
                "数据库文件不存在，暂时没有可备份的数据",
                code="database_not_found",
                status=409,
            )
        if not integrity["ok"]:
            raise VersionControlError(
                "当前数据库完整性检查未通过，请先修复数据库",
                code="database_integrity_failed",
                status=409,
            )
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        database_file = self._database_file(backup_id)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f"{backup_id}-", suffix=".tmp", dir=self.backup_dir
        )
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink(missing_ok=True)
            self._copy_database(self.database_path, temporary)
            os.replace(temporary, database_file)
        except Exception:
            temporary.unlink(missing_ok=True)
            database_file.unlink(missing_ok=True)
            raise
        metadata = {
            "id": backup_id,
            "created_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "reason": str(reason or "manual")[:80],
            "database_path": str(self.database_path),
            "file_name": database_file.name,
            "size_bytes": database_file.stat().st_size,
            "sha256": _sha256(database_file),
            "schema_version": integrity["schema_version"],
            "code_commit": str(code_commit or "")[:80],
            "code_version": str(code_version or "")[:80],
        }
        self._write_metadata(backup_id, metadata)
        return metadata

    def get_backup(self, backup_id: Any) -> dict[str, Any]:
        normalized = self._validate_id(backup_id)
        metadata_file = self._metadata_file(normalized)
        database_file = self._database_file(normalized)
        if not metadata_file.exists() or not database_file.exists():
            raise VersionControlError(
                "备份不存在或已损坏",
                code="backup_not_found",
                status=404,
            )
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionControlError(
                "备份记录无效",
                code="backup_metadata_invalid",
                status=500,
            ) from exc
        if not isinstance(metadata, dict) or metadata.get("id") != normalized:
            raise VersionControlError(
                "备份记录无效",
                code="backup_metadata_invalid",
                status=500,
            )
        actual_sha = _sha256(database_file)
        if actual_sha != metadata.get("sha256"):
            raise VersionControlError(
                "备份校验失败，文件可能已被修改",
                code="backup_checksum_mismatch",
                status=409,
            )
        return {**metadata, "path": str(database_file)}

    def list_backups(self) -> dict[str, Any]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for metadata_file in self.backup_dir.glob("*.json"):
            backup_id = metadata_file.stem
            if not BACKUP_ID_PATTERN.fullmatch(backup_id):
                continue
            try:
                records.append(self.get_backup(backup_id))
            except VersionControlError:
                continue
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "ok": True,
            "items": records,
            "total": len(records),
            "latest": records[0] if records else None,
        }

    def restore_backup(
        self,
        backup_id: Any,
        *,
        create_safety_backup: bool = True,
        code_commit: str = "",
        code_version: str = "",
    ) -> dict[str, Any]:
        backup = self.get_backup(backup_id)
        safety_backup = None
        if create_safety_backup and self.database_path.exists():
            safety_backup = self.create_backup(
                reason="before-restore",
                code_commit=code_commit,
                code_version=code_version,
            )
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix="restore-", suffix=".sqlite3.tmp", dir=self.backup_dir
        )
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink(missing_ok=True)
            self._copy_database(Path(backup["path"]), temporary)
            integrity = self._integrity(temporary)
            if not integrity["ok"]:
                raise VersionControlError(
                    "待恢复备份完整性检查未通过",
                    code="restore_integrity_failed",
                    status=409,
                )
            os.replace(temporary, self.database_path)
            for sidecar in (
                Path(f"{self.database_path}-wal"),
                Path(f"{self.database_path}-shm"),
            ):
                sidecar.unlink(missing_ok=True)
        except PermissionError as exc:
            temporary.unlink(missing_ok=True)
            raise VersionControlError(
                "数据库正在使用，请停止写入后再恢复",
                code="database_in_use",
                status=409,
                retryable=True,
            ) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "ok": True,
            "status": "restored",
            "restored_backup_id": backup["id"],
            "safety_backup_id": safety_backup["id"] if safety_backup else None,
            "database": self.database_status(),
            "needs_restart": True,
            "message": "数据已恢复，请重启服务以确保所有连接重新加载数据库",
        }

    def database_status(self) -> dict[str, Any]:
        integrity = self._integrity(self.database_path)
        latest = self.list_backups()["latest"]
        size = self.database_path.stat().st_size if self.database_path.exists() else 0
        return {
            "path": str(self.database_path),
            "file_name": self.database_path.name,
            "size_bytes": size,
            "integrity": integrity["ok"],
            "integrity_message": integrity["message"],
            "schema_version": integrity["schema_version"],
            "compatibility": "sqlite-preserved",
            "backup_count": self.list_backups()["total"],
            "latest_backup": latest,
        }
