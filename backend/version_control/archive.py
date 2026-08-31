"""Verified source-archive installation and code rollback support."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import VersionControlError


MAX_ARCHIVE_FILES = 12_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
CODE_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_path(relative: PurePosixPath) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    if not parts:
        return True
    name = parts[-1]
    if parts[0] in {".git", ".runtime", "artifacts"}:
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name.endswith((".key", ".pem", ".p12", ".pfx", ".jks")):
        return True
    if name.endswith(".json") and (
        name.startswith(("credentials", "secrets", "service-account"))
    ):
        return True
    if name.endswith((".db", ".sqlite", ".sqlite3", ".log")) or ".db-" in name:
        return True
    if parts[:2] in {
        ("frontend", "node_modules"),
        ("frontend", "dist"),
        ("backend", "waf-browser-profile"),
        ("backend", "order-waf-browser-profile"),
    }:
        return True
    if parts[:3] == ("backend", "order_query", "waf-browser-profile"):
        return True
    return False


@dataclass
class ArchiveUpdateManager:
    root: Path
    now: Callable[[], datetime]

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    @property
    def update_dir(self) -> Path:
        return self.root / ".runtime" / "version-updates"

    @property
    def installed_state_path(self) -> Path:
        return self.update_dir / "installed.json"

    def installed_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.installed_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def installed_state_snapshot(self) -> bytes | None:
        try:
            return self.installed_state_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise VersionControlError(
                "无法读取下载版安装状态",
                code="installed_state_read_failed",
                status=500,
            ) from exc

    def restore_installed_state(self, snapshot: bytes | None) -> None:
        temporary = self.installed_state_path.with_suffix(".json.tmp")
        try:
            if snapshot is None:
                self.installed_state_path.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
                return
            self.update_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(snapshot)
            os.replace(temporary, self.installed_state_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise VersionControlError(
                "下载版安装状态恢复失败",
                code="installed_state_restore_failed",
                status=500,
            ) from exc

    def write_installed_state(
        self,
        *,
        repository_url: str,
        branch: str,
        commit: str,
        version: str,
    ) -> None:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "repository_url": str(repository_url),
            "branch": str(branch),
            "commit": str(commit),
            "version": str(version),
            "installed_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        temporary = self.installed_state_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.installed_state_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise VersionControlError(
                "下载版安装状态写入失败",
                code="installed_state_write_failed",
                status=500,
            ) from exc

    def _validated_entries(self, archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if not infos or len(infos) > MAX_ARCHIVE_FILES:
            raise VersionControlError("GitHub 源码包文件数量异常", code="archive_file_count_invalid", status=502)
        if sum(max(0, item.file_size) for item in infos) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise VersionControlError("GitHub 源码包解压后过大", code="archive_too_large", status=502)

        raw_paths: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        roots: set[str] = set()
        for item in infos:
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if (
                path.is_absolute()
                or len(path.parts) < 2
                or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
            ):
                raise VersionControlError("GitHub 源码包包含无效路径", code="archive_path_invalid", status=502)
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or item.flag_bits & 0x1:
                raise VersionControlError("GitHub 源码包包含不支持的链接或加密文件", code="archive_entry_invalid", status=502)
            roots.add(path.parts[0])
            raw_paths.append((item, path))
        if len(roots) != 1:
            raise VersionControlError("GitHub 源码包目录结构无效", code="archive_layout_invalid", status=502)

        entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen: set[str] = set()
        for item, path in raw_paths:
            relative = PurePosixPath(*path.parts[1:])
            normalized = relative.as_posix().casefold()
            if not normalized or normalized in seen:
                raise VersionControlError("GitHub 源码包包含重复路径", code="archive_path_duplicate", status=502)
            seen.add(normalized)
            if not _protected_path(relative):
                entries.append((item, relative))

        required = {"VERSION", "backend/main.py", "frontend/package.json", "start.ps1"}
        available = {relative.as_posix() for _, relative in entries}
        if not required.issubset(available):
            raise VersionControlError("GitHub 源码包缺少必要项目文件", code="archive_project_invalid", status=502)
        return entries

    def inspect(self, payload: bytes) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                entries = self._validated_entries(archive)
                version_entry = next(item for item, path in entries if path.as_posix() == "VERSION")
                version = archive.read(version_entry).decode("utf-8").strip()[:80]
        except VersionControlError:
            raise
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise VersionControlError("GitHub 返回的源码包无效", code="archive_invalid", status=502) from exc
        if not version:
            raise VersionControlError("GitHub 源码包版本号为空", code="archive_version_invalid", status=502)
        return {"version": version, "file_count": len(entries)}

    def _code_backup_paths(self, backup_id: str) -> tuple[Path, Path]:
        return self.update_dir / f"{backup_id}.zip", self.update_dir / f"{backup_id}.json"

    def _create_code_backup(self, paths: list[PurePosixPath], *, reason: str) -> dict[str, Any]:
        self.update_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"code-{stamp}-{uuid.uuid4().hex[:8]}"
        archive_path, metadata_path = self._code_backup_paths(backup_id)
        fd, temporary_name = tempfile.mkstemp(prefix=f"{backup_id}-", suffix=".tmp", dir=self.update_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        items: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for relative in paths:
                    target = self.root.joinpath(*relative.parts)
                    existed = target.is_file()
                    items.append({"path": relative.as_posix(), "existed": existed})
                    if existed:
                        archive.write(target, f"files/{relative.as_posix()}")
            os.replace(temporary, archive_path)
        except (OSError, zipfile.BadZipFile) as exc:
            temporary.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
            raise VersionControlError("升级前代码备份失败", code="code_backup_failed", status=500) from exc
        metadata = {
            "id": backup_id,
            "created_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "archive_file": archive_path.name,
            "sha256": _sha256(archive_path),
            "items": items,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def _load_code_backup(self, backup_id: Any) -> tuple[dict[str, Any], Path]:
        normalized = str(backup_id or "").strip()
        if not CODE_BACKUP_ID_PATTERN.fullmatch(normalized):
            raise VersionControlError("代码备份标识无效", code="code_backup_id_invalid", status=400)
        archive_path, metadata_path = self._code_backup_paths(normalized)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionControlError("代码备份不存在", code="code_backup_not_found", status=404) from exc
        if not isinstance(metadata, dict) or metadata.get("id") != normalized or not archive_path.is_file():
            raise VersionControlError("代码备份记录无效", code="code_backup_invalid", status=409)
        if _sha256(archive_path) != metadata.get("sha256"):
            raise VersionControlError("代码备份校验失败", code="code_backup_checksum_mismatch", status=409)
        return metadata, archive_path

    def restore_code_backup(self, backup_id: Any) -> dict[str, Any]:
        metadata, archive_path = self._load_code_backup(backup_id)
        items = metadata.get("items")
        if not isinstance(items, list):
            raise VersionControlError("代码备份清单无效", code="code_backup_invalid", status=409)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for item in items:
                    relative = PurePosixPath(str(item.get("path") or ""))
                    if relative.is_absolute() or ".." in relative.parts or _protected_path(relative):
                        raise VersionControlError("代码备份包含无效路径", code="code_backup_invalid", status=409)
                    target = self.root.joinpath(*relative.parts)
                    if bool(item.get("existed")):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        data = archive.read(f"files/{relative.as_posix()}")
                        fd, temporary_name = tempfile.mkstemp(prefix="rollback-", suffix=".tmp", dir=target.parent)
                        os.close(fd)
                        temporary = Path(temporary_name)
                        temporary.write_bytes(data)
                        os.replace(temporary, target)
                    elif target.is_file() or target.is_symlink():
                        target.unlink()
        except VersionControlError:
            raise
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            raise VersionControlError("代码备份恢复失败，可能有文件正在使用", code="code_restore_failed", status=409, retryable=True) from exc
        return metadata

    def install(self, payload: bytes) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
            entries = self._validated_entries(archive)
        except VersionControlError:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise VersionControlError("GitHub 返回的源码包无效", code="archive_invalid", status=502) from exc
        paths = [relative for _, relative in entries]
        backup = self._create_code_backup(paths, reason="before-archive-update")
        try:
            for item, relative in entries:
                target = self.root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item)
                fd, temporary_name = tempfile.mkstemp(prefix="update-", suffix=".tmp", dir=target.parent)
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            self.restore_code_backup(backup["id"])
            raise VersionControlError("源码更新失败，代码已自动恢复", code="archive_install_failed", status=409, retryable=True) from exc
        finally:
            archive.close()
        return {"code_backup": backup, "file_count": len(entries)}
