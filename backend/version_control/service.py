"""Read local version metadata and install fast-forward updates from GitHub."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .archive import ArchiveUpdateManager
from .backup import BackupManager
from .errors import VersionControlError


DEFAULT_REPOSITORY_URL = "https://github.com/qion888/ldsub2api.git"
DEFAULT_BRANCH = "main"
MAX_GITHUB_RESPONSE_BYTES = 1024 * 1024
MAX_GITHUB_ARCHIVE_BYTES = 100 * 1024 * 1024
_GITHUB_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _github_slug(repository_url: str) -> str:
    value = str(repository_url or "").strip()
    if value.startswith("git@github.com:"):
        slug = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise VersionControlError(
                "更新仓库必须是 HTTPS GitHub 仓库",
                code="invalid_update_repository",
                status=500,
            )
        slug = parsed.path.strip("/")
    slug = slug.removesuffix(".git")
    if not _GITHUB_SLUG.fullmatch(slug):
        raise VersionControlError(
            "GitHub 更新仓库配置无效",
            code="invalid_update_repository",
            status=500,
        )
    return slug


def _repository_key(repository_url: str) -> str:
    try:
        return f"github:{_github_slug(repository_url).lower()}"
    except VersionControlError:
        return str(repository_url or "").strip().rstrip("/\\").lower()


def _public_repository_url(repository_url: str) -> str:
    slug = _github_slug(repository_url)
    return f"https://github.com/{slug}"


@dataclass(frozen=True)
class RepositoryConfig:
    root: Path
    repository_url: str = DEFAULT_REPOSITORY_URL
    branch: str = DEFAULT_BRANCH
    remote: str = "origin"

    @classmethod
    def from_environment(cls, root: Path) -> "RepositoryConfig":
        branch = str(os.environ.get("LDXP_UPDATE_BRANCH", DEFAULT_BRANCH)).strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("-", "/")):
            branch = DEFAULT_BRANCH
        remote = str(os.environ.get("LDXP_UPDATE_REMOTE", "origin")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", remote) or remote.startswith("-"):
            remote = "origin"
        return cls(
            root=Path(root).resolve(),
            repository_url=str(os.environ.get("LDXP_UPDATE_REPOSITORY", DEFAULT_REPOSITORY_URL)).strip(),
            branch=branch,
            remote=remote,
        )

    @property
    def version_file(self) -> Path:
        return self.root / "VERSION"

    @property
    def github_slug(self) -> str:
        return _github_slug(self.repository_url)

    @property
    def github_url(self) -> str:
        return _public_repository_url(self.repository_url)


class VersionControlService:
    def __init__(
        self,
        config: RepositoryConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] | None = None,
        database_path: Path | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._opener = opener
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._update_lock = threading.Lock()
        self._backups = BackupManager(
            config.root,
            database_path or (config.root / "backend" / "monitor.db"),
            now=self._now,
        )
        self._archives = ArchiveUpdateManager(config.root, now=self._now)

    def _run_git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(self.config.root), *arguments]
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise VersionControlError(
                "未找到 Git，无法读取或更新版本",
                code="git_unavailable",
                status=503,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VersionControlError(
                "Git 操作超时，请稍后重试",
                code="git_timeout",
                status=504,
                retryable=True,
            ) from exc
        if check and result.returncode != 0:
            detail = str(result.stderr or result.stdout or "Git 操作失败").strip()[:300]
            raise VersionControlError(
                detail,
                code="git_command_failed",
                status=409,
                retryable=True,
            )
        return result

    def _git_text(self, *arguments: str) -> str:
        return str(self._run_git(*arguments).stdout or "").strip()

    @property
    def _remote_ref(self) -> str:
        return f"refs/remotes/{self.config.remote}/{self.config.branch}"

    def _fetch_remote(self) -> None:
        fetch_spec = f"+refs/heads/{self.config.branch}:{self._remote_ref}"
        self._run_git("fetch", "--prune", self.config.remote, fetch_spec)

    def _remote_commit(self) -> str:
        return self._git_text("rev-parse", self._remote_ref)

    def _commit_distance(self, current_commit: str, remote_commit: str) -> tuple[int, int]:
        result = self._run_git(
            "rev-list",
            "--left-right",
            "--count",
            f"{current_commit}...{remote_commit}",
        )
        values = str(result.stdout or "").strip().split()
        if len(values) != 2:
            raise VersionControlError(
                "Git 未返回有效的版本关系",
                code="git_comparison_failed",
                status=409,
                retryable=True,
            )
        try:
            local_ahead, remote_ahead = (max(0, int(value)) for value in values)
        except ValueError as exc:
            raise VersionControlError(
                "Git 未返回有效的版本关系",
                code="git_comparison_failed",
                status=409,
                retryable=True,
            ) from exc
        return local_ahead, remote_ahead

    def _remote_version_file(self) -> str:
        result = self._run_git("show", f"{self._remote_ref}:VERSION", check=False)
        if result.returncode != 0:
            return ""
        return str(result.stdout or "").strip()[:80]

    def _read_current_version(self) -> str:
        try:
            value = self.config.version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "dev"
        return value[:80] or "dev"

    def _is_git_installation(self) -> bool:
        # GitHub's "Download ZIP" packages intentionally omit .git. Check the
        # marker before invoking Git so archive installations never surface a
        # raw "not a git repository" error.
        return (self.config.root / ".git").exists()

    def _repository_state(self) -> dict[str, Any]:
        if not self._is_git_installation():
            installed = self._archives.installed_state()
            installed_matches = (
                _repository_key(str(installed.get("repository_url") or ""))
                == _repository_key(self.config.repository_url)
                and str(installed.get("branch") or "") == self.config.branch
            )
            current_commit = str(installed.get("commit") or "").strip().lower() if installed_matches else ""
            return {
                "current_version": self._read_current_version(),
                "current_commit": current_commit,
                "current_short_commit": current_commit[:8],
                "branch": self.config.branch,
                "target_branch": self.config.branch,
                "remote": "github-archive",
                "repository_url": self.config.github_url,
                "worktree_clean": True,
                "dirty_file_count": 0,
                "repository_matches": True,
                "can_update": True,
                "installation_mode": "archive",
            }
        inside = self._git_text("rev-parse", "--is-inside-work-tree").lower()
        if inside != "true":
            raise VersionControlError(
                "当前目录不是 Git 工作树",
                code="not_git_worktree",
                status=409,
            )
        current_commit = self._git_text("rev-parse", "HEAD")
        branch = self._git_text("branch", "--show-current")
        remote_url = self._git_text("remote", "get-url", self.config.remote)
        dirty_lines = [
            line for line in self._git_text("status", "--porcelain=v1", "--untracked-files=all").splitlines()
            if line.strip()
        ]
        repository_matches = _repository_key(remote_url) == _repository_key(self.config.repository_url)
        worktree_clean = not dirty_lines
        return {
            "current_version": self._read_current_version(),
            "current_commit": current_commit,
            "current_short_commit": current_commit[:8],
            "branch": branch,
            "target_branch": self.config.branch,
            "remote": self.config.remote,
            "repository_url": self.config.github_url,
            "worktree_clean": worktree_clean,
            "dirty_file_count": len(dirty_lines),
            "repository_matches": repository_matches,
            "can_update": bool(
                worktree_clean
                and branch == self.config.branch
                and repository_matches
            ),
            "installation_mode": "git",
        }

    def version_info(self) -> dict[str, Any]:
        state = self._repository_state()
        backups = self._backups.list_backups()
        return {
            "ok": True,
            **state,
            "status": "ready" if state["can_update"] else "blocked",
            "database": self._backups.database_status(),
            "backups": backups,
            "last_update": self._read_last_update(),
        }

    @property
    def _last_update_path(self) -> Path:
        return self._backups.backup_dir / "last-update.json"

    def _read_last_update(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._last_update_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_last_update(self, payload: dict[str, Any]) -> None:
        self._backups.backup_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._last_update_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self._last_update_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise VersionControlError(
                "版本更新记录写入失败",
                code="last_update_write_failed",
                status=500,
            ) from exc

    @staticmethod
    def _file_snapshot(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise VersionControlError(
                "无法读取版本更新记录",
                code="last_update_read_failed",
                status=500,
            ) from exc

    @staticmethod
    def _restore_file_snapshot(path: Path, snapshot: bytes | None) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        try:
            if snapshot is None:
                path.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(snapshot)
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise VersionControlError(
                "版本更新记录恢复失败",
                code="last_update_restore_failed",
                status=500,
            ) from exc

    def _github_json(self, path: str, *, allow_not_found: bool = False) -> Any:
        url = f"https://api.github.com/repos/{self.config.github_slug}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "LDXPLocalMonitor-VersionUpdater",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = str(os.environ.get("LDXP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener(request, timeout=15) as response:
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            if exc.code == 403:
                detail = "GitHub API 暂时拒绝请求或已达到访问频率限制"
                code = "github_rate_limited"
            else:
                detail = f"GitHub API 返回 HTTP {exc.code}"
                code = "github_http_error"
            raise VersionControlError(detail, code=code, status=502, retryable=True) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VersionControlError(
                "无法连接 GitHub，请检查网络后重试",
                code="github_unavailable",
                status=502,
                retryable=True,
            ) from exc
        if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
            raise VersionControlError(
                "GitHub 返回的数据过大",
                code="github_response_too_large",
                status=502,
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VersionControlError(
                "GitHub 返回了无效数据",
                code="github_invalid_response",
                status=502,
                retryable=True,
            ) from exc

    def _latest_version_file(self) -> str:
        branch = quote(self.config.branch, safe="")
        payload = self._github_json(f"contents/VERSION?ref={branch}", allow_not_found=True)
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return ""
        try:
            value = base64.b64decode(str(payload.get("content") or ""), validate=False).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            return ""
        return value[:80]

    def _archive_remote_metadata(self) -> dict[str, str]:
        branch = quote(self.config.branch, safe="")
        latest = self._github_json(f"commits/{branch}")
        latest_commit = str(latest.get("sha") if isinstance(latest, dict) else "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", latest_commit):
            raise VersionControlError(
                "GitHub 未返回有效的最新提交",
                code="github_invalid_commit",
                status=502,
                retryable=True,
            )
        latest_version = self._latest_version_file()
        if not latest_version:
            raise VersionControlError(
                "GitHub 主分支缺少有效 VERSION 文件",
                code="github_version_missing",
                status=502,
                retryable=True,
            )
        return {"commit": latest_commit, "version": latest_version}

    def _download_archive(self) -> bytes:
        branch = quote(self.config.branch, safe="")
        url = f"https://codeload.github.com/{self.config.github_slug}/zip/{branch}"
        request = Request(
            url,
            headers={"Accept": "application/zip", "User-Agent": "LDXPLocalMonitor-VersionUpdater"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=60) as response:
                payload = response.read(MAX_GITHUB_ARCHIVE_BYTES + 1)
        except HTTPError as exc:
            raise VersionControlError(
                f"GitHub 源码下载返回 HTTP {exc.code}",
                code="github_archive_http_error",
                status=502,
                retryable=True,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VersionControlError(
                "GitHub 源码下载失败，请检查网络后重试",
                code="github_archive_unavailable",
                status=502,
                retryable=True,
            ) from exc
        if len(payload) > MAX_GITHUB_ARCHIVE_BYTES:
            raise VersionControlError("GitHub 源码包过大", code="github_archive_too_large", status=502)
        return payload

    def _check_archive_updates(self, state: dict[str, Any]) -> dict[str, Any]:
        remote = self._archive_remote_metadata()
        current_commit = str(state.get("current_commit") or "").lower()
        same_commit = bool(current_commit and current_commit == remote["commit"])
        same_version = state["current_version"].lstrip("v") == remote["version"].lstrip("v")
        update_available = not same_commit if current_commit else not same_version
        status = "update_available" if update_available else "up_to_date"
        message = (
            "发现可用源码更新，升级前将自动备份数据库和当前代码"
            if update_available
            else "当前已是 GitHub 最新版本"
        )
        return {
            "ok": True,
            **state,
            "status": status,
            "message": message,
            "update_available": update_available,
            "update_ready": update_available,
            "latest_version": remote["version"],
            "latest_commit": remote["commit"],
            "latest_short_commit": remote["commit"][:8],
            "ahead_by": 1 if update_available else 0,
            "behind_by": 0,
            "remote_ahead_by": 1 if update_available else 0,
            "local_ahead_by": 0,
            "comparison_source": "github-version",
            "github_commit": remote["commit"],
            "release": None,
            "checked_at": self._now().isoformat(timespec="seconds"),
            "database": self._backups.database_status(),
            "last_update": self._read_last_update(),
        }

    def check_updates(self) -> dict[str, Any]:
        state = self._repository_state()
        if state["installation_mode"] == "archive":
            return self._check_archive_updates(state)
        branch = quote(self.config.branch, safe="")
        self._fetch_remote()
        remote_commit = self._remote_commit().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", remote_commit):
            raise VersionControlError(
                "GitHub 远端引用不是有效提交",
                code="git_invalid_remote_commit",
                status=502,
                retryable=True,
            )
        latest_commit = remote_commit
        github_commit = ""
        try:
            latest = self._github_json(f"commits/{branch}")
            if isinstance(latest, dict) and re.fullmatch(r"[0-9a-fA-F]{40}", str(latest.get("sha") or "")):
                github_commit = str(latest["sha"]).lower()
        except VersionControlError:
            latest = None
        try:
            release = self._github_json("releases/latest", allow_not_found=True)
        except VersionControlError:
            # Git refs are authoritative for update safety; release metadata is optional.
            release = None
        release_info = None
        latest_version = ""
        if isinstance(release, dict):
            latest_version = str(release.get("tag_name") or "").strip()[:80]
            release_info = {
                "tag": latest_version,
                "name": str(release.get("name") or latest_version).strip()[:160],
                "published_at": release.get("published_at"),
                "url": str(release.get("html_url") or "").strip(),
            }
        if not latest_version:
            latest_version = self._remote_version_file()
        if not latest_version:
            try:
                latest_version = self._latest_version_file()
            except VersionControlError:
                latest_version = ""
        latest_version = latest_version or state["current_version"]

        local_ahead_by, remote_ahead_by = self._commit_distance(
            state["current_commit"].lower(), remote_commit
        )
        if state["current_commit"].lower() == remote_commit:
            check_status = "up_to_date"
            message = "当前已是 GitHub 最新版本"
        elif remote_ahead_by > 0 and local_ahead_by == 0:
            check_status = "update_available"
            message = f"发现 {remote_ahead_by} 个可用更新提交，升级前将自动备份数据"
        elif local_ahead_by > 0 and remote_ahead_by == 0:
            check_status = "local_ahead"
            message = "本地版本领先于 GitHub 主分支"
        elif local_ahead_by > 0 and remote_ahead_by > 0:
            check_status = "diverged"
            message = "本地分支与 GitHub 主分支已分叉，无法自动更新"
        else:
            check_status = "comparison_unavailable"
            message = "发现版本差异，但 GitHub 无法确认快进关系"

        update_available = check_status == "update_available"
        return {
            "ok": True,
            **state,
            "status": check_status,
            "message": message,
            "update_available": update_available,
            "update_ready": bool(update_available and state["can_update"]),
            "latest_version": latest_version,
            "latest_commit": latest_commit,
            "latest_short_commit": latest_commit[:8],
            "ahead_by": remote_ahead_by,
            "behind_by": local_ahead_by,
            "remote_ahead_by": remote_ahead_by,
            "local_ahead_by": local_ahead_by,
            "comparison_source": "git-fetch",
            "github_commit": github_commit or None,
            "release": release_info,
            "checked_at": self._now().isoformat(timespec="seconds"),
            "database": self._backups.database_status(),
            "last_update": self._read_last_update(),
        }

    def _require_update_ready(self, state: dict[str, Any]) -> None:
        if not state["repository_matches"]:
            raise VersionControlError(
                "当前 origin 与配置的 GitHub 更新仓库不一致",
                code="repository_mismatch",
                status=409,
            )
        if state["branch"] != self.config.branch:
            raise VersionControlError(
                f"自动更新仅支持 {self.config.branch} 分支，当前为 {state['branch'] or 'detached HEAD'}",
                code="wrong_update_branch",
                status=409,
            )
        if not state["worktree_clean"]:
            raise VersionControlError(
                f"工作树有 {state['dirty_file_count']} 项未提交改动，请先处理后再更新",
                code="dirty_worktree",
                status=409,
            )

    def _install_archive_update(self, before: dict[str, Any]) -> dict[str, Any]:
        remote = self._archive_remote_metadata()
        current_commit = str(before.get("current_commit") or "").lower()
        same_commit = bool(current_commit and current_commit == remote["commit"])
        same_version = before["current_version"].lstrip("v") == remote["version"].lstrip("v")
        if same_commit or (not current_commit and same_version):
            return {
                "ok": True,
                **before,
                "status": "up_to_date",
                "updated": False,
                "needs_restart": False,
                "message": "当前已是 GitHub 最新版本",
                "latest_version": remote["version"],
                "latest_commit": remote["commit"],
                "latest_short_commit": remote["commit"][:8],
            }

        payload = self._download_archive()
        inspected = self._archives.inspect(payload)
        if inspected["version"].lstrip("v") != remote["version"].lstrip("v"):
            raise VersionControlError(
                "GitHub 源码包与远端版本信息不一致，请稍后重试",
                code="archive_version_mismatch",
                status=409,
                retryable=True,
            )
        backup = self._backups.create_backup(
            reason="before-update",
            code_commit=before["current_commit"],
            code_version=before["current_version"],
        )
        previous_installed_state = self._archives.installed_state_snapshot()
        previous_last_update = self._file_snapshot(self._last_update_path)
        installed = self._archives.install(payload)
        try:
            self._archives.write_installed_state(
                repository_url=self.config.github_url,
                branch=self.config.branch,
                commit=remote["commit"],
                version=remote["version"],
            )
            after = self._repository_state()
            if after["current_version"].lstrip("v") != remote["version"].lstrip("v"):
                raise VersionControlError(
                    "源码更新后的 VERSION 校验失败",
                    code="archive_update_verification_failed",
                    status=500,
                )
            last_update = {
                "installation_mode": "archive",
                "backup_id": backup["id"],
                "code_backup_id": installed["code_backup"]["id"],
                "previous_commit": before["current_commit"],
                "updated_commit": remote["commit"],
                "previous_version": before["current_version"],
                "updated_version": after["current_version"],
                "updated_at": self._now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write_last_update(last_update)
        except Exception as exc:
            recovery_errors: list[str] = []
            try:
                self._archives.restore_code_backup(installed["code_backup"]["id"])
            except Exception as recovery_exc:
                recovery_errors.append(str(recovery_exc))
            try:
                self._archives.restore_installed_state(previous_installed_state)
            except Exception as recovery_exc:
                recovery_errors.append(str(recovery_exc))
            try:
                self._restore_file_snapshot(self._last_update_path, previous_last_update)
            except Exception as recovery_exc:
                recovery_errors.append(str(recovery_exc))
            if recovery_errors:
                raise VersionControlError(
                    f"源码更新失败，自动恢复未完整完成：{'; '.join(recovery_errors)[:240]}",
                    code="archive_update_recovery_failed",
                    status=500,
                ) from exc
            detail = exc.detail if isinstance(exc, VersionControlError) else "源码更新收尾失败"
            code = exc.code if isinstance(exc, VersionControlError) else "archive_update_finalize_failed"
            raise VersionControlError(
                f"{detail}，代码已自动恢复",
                code=code,
                status=500,
                retryable=isinstance(exc, VersionControlError) and exc.retryable,
            ) from exc
        return {
            "ok": True,
            **after,
            "status": "updated",
            "updated": True,
            "needs_restart": True,
            "previous_commit": before["current_commit"],
            "latest_commit": remote["commit"],
            "latest_short_commit": remote["commit"][:8],
            "message": "源码版已更新，数据库和本地运行数据已保留，请重启服务",
            "backup": backup,
            "code_backup_id": installed["code_backup"]["id"],
            "updated_file_count": installed["file_count"],
            "last_update": {**last_update, "can_rollback": True},
            "database": self._backups.database_status(),
        }

    def install_update(self) -> dict[str, Any]:
        if not self._update_lock.acquire(blocking=False):
            raise VersionControlError(
                "已有版本更新任务正在执行",
                code="update_in_progress",
                status=409,
                retryable=True,
            )
        try:
            before = self._repository_state()
            self._require_update_ready(before)
            if before["installation_mode"] == "archive":
                return self._install_archive_update(before)
            self._fetch_remote()

            # Fetch can take time, so verify the mutable state again before merging.
            current = self._repository_state()
            self._require_update_ready(current)
            remote_commit = self._remote_commit()
            if current["current_commit"] == remote_commit:
                return {
                    "ok": True,
                    **current,
                    "status": "up_to_date",
                    "updated": False,
                    "needs_restart": False,
                    "message": "当前已是 GitHub 最新版本",
                    "latest_commit": remote_commit,
                    "latest_short_commit": remote_commit[:8],
                }

            current_is_ancestor = self._run_git(
                "merge-base", "--is-ancestor", current["current_commit"], remote_commit, check=False
            )
            if current_is_ancestor.returncode != 0:
                remote_is_ancestor = self._run_git(
                    "merge-base", "--is-ancestor", remote_commit, current["current_commit"], check=False
                )
                if remote_is_ancestor.returncode == 0:
                    raise VersionControlError(
                        "本地分支领先于 GitHub，无需自动更新",
                        code="local_ahead",
                        status=409,
                    )
                raise VersionControlError(
                    "本地分支与 GitHub 主分支已分叉，无法快进更新",
                    code="branch_diverged",
                    status=409,
                )

            backup = self._backups.create_backup(
                reason="before-update",
                code_commit=before["current_commit"],
                code_version=before["current_version"],
            )
            self._run_git("merge", "--ff-only", self._remote_ref)
            after = self._repository_state()
            if after["current_commit"] != remote_commit:
                raise VersionControlError(
                    "Git 更新完成后提交校验失败",
                    code="update_verification_failed",
                    status=500,
                )
            last_update = {
                "installation_mode": "git",
                "backup_id": backup["id"],
                "previous_commit": before["current_commit"],
                "updated_commit": remote_commit,
                "previous_version": before["current_version"],
                "updated_version": after["current_version"],
                "updated_at": self._now().astimezone(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write_last_update(last_update)
            return {
                "ok": True,
                **after,
                "status": "updated",
                "updated": True,
                "needs_restart": True,
                "previous_commit": before["current_commit"],
                "latest_commit": remote_commit,
                "latest_short_commit": remote_commit[:8],
                "message": "版本已更新，请重启服务以加载新代码",
                "backup": backup,
                "last_update": {**last_update, "can_rollback": True},
                "database": self._backups.database_status(),
            }
        finally:
            self._update_lock.release()

    def create_backup(self, reason: str = "manual") -> dict[str, Any]:
        state = self._repository_state()
        return {
            "ok": True,
            "backup": self._backups.create_backup(
                reason=reason,
                code_commit=state["current_commit"],
                code_version=state["current_version"],
            ),
            "database": self._backups.database_status(),
            "message": "数据备份已完成",
        }

    def list_backups(self) -> dict[str, Any]:
        return self._backups.list_backups()

    def delete_backup(self, backup_id: Any) -> dict[str, Any]:
        selected_id = str(backup_id or "").strip()
        last_update = self._read_last_update() or {}
        if selected_id and selected_id == str(last_update.get("backup_id") or "").strip():
            raise VersionControlError(
                "最近一次升级备份用于版本回退，暂不能删除",
                code="backup_in_use",
                status=409,
            )
        deleted = self._backups.delete_backup(backup_id)
        return {
            "ok": True,
            "status": "deleted",
            "deleted_backup_id": deleted["id"],
            "backups": self._backups.list_backups(),
            "database": self._backups.database_status(),
            "message": "数据备份已删除",
        }

    def restore_backup(self, backup_id: Any) -> dict[str, Any]:
        state = self._repository_state()
        return self._backups.restore_backup(
            backup_id,
            create_safety_backup=True,
            code_commit=state["current_commit"],
            code_version=state["current_version"],
        )

    def rollback_update(self, backup_id: Any = None, *, restore_data: bool = False) -> dict[str, Any]:
        if not self._update_lock.acquire(blocking=False):
            raise VersionControlError(
                "已有版本操作正在执行",
                code="update_in_progress",
                status=409,
                retryable=True,
            )
        try:
            state = self._repository_state()
            self._require_update_ready(state)
            last_update = self._read_last_update()
            selected_id = str(backup_id or (last_update or {}).get("backup_id") or "").strip()
            if not last_update or not selected_id:
                raise VersionControlError(
                    "没有可回退的版本更新记录",
                    code="rollback_not_available",
                    status=409,
                )
            if selected_id != str(last_update.get("backup_id")):
                raise VersionControlError(
                    "指定备份不是最近一次版本更新的备份",
                    code="rollback_backup_mismatch",
                    status=409,
                )
            if state["current_commit"] != str(last_update.get("updated_commit")):
                raise VersionControlError(
                    "当前提交已发生变化，不能直接回退该版本",
                    code="rollback_commit_mismatch",
                    status=409,
                )
            safety_backup = self._backups.create_backup(
                reason="before-rollback",
                code_commit=state["current_commit"],
                code_version=state["current_version"],
            )
            if str(last_update.get("installation_mode") or "git") == "archive":
                code_backup_id = str(last_update.get("code_backup_id") or "").strip()
                if not code_backup_id:
                    raise VersionControlError(
                        "下载版更新缺少代码备份，无法回退",
                        code="rollback_code_backup_missing",
                        status=409,
                    )
                self._archives.restore_code_backup(code_backup_id)
                previous_commit = str(last_update.get("previous_commit") or "")
                previous_version = str(last_update.get("previous_version") or self._read_current_version())
                self._archives.write_installed_state(
                    repository_url=self.config.github_url,
                    branch=self.config.branch,
                    commit=previous_commit,
                    version=previous_version,
                )
                restored = None
                if restore_data:
                    restored = self._backups.restore_backup(
                        selected_id,
                        create_safety_backup=False,
                        code_commit=previous_commit,
                        code_version=previous_version,
                    )
                after = self._repository_state()
                return {
                    "ok": True,
                    "status": "rolled_back",
                    "previous_commit": after["current_commit"],
                    "safety_backup_id": safety_backup["id"],
                    "restored_backup_id": selected_id if restored else None,
                    "data_restored": bool(restored),
                    "needs_restart": True,
                    "installation_mode": "archive",
                    "database": self._backups.database_status(),
                    "message": "下载版代码已回退，请重启服务；数据按选择处理",
                }
            previous_commit = str(last_update.get("previous_commit") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", previous_commit):
                raise VersionControlError(
                    "回退记录中的提交无效",
                    code="rollback_commit_invalid",
                    status=409,
                )
            self._run_git("reset", "--hard", previous_commit)
            restored = None
            if restore_data:
                restored = self._backups.restore_backup(
                    selected_id,
                    create_safety_backup=False,
                    code_commit=previous_commit,
                    code_version=str(last_update.get("previous_version") or ""),
                )
            after = self._repository_state()
            return {
                "ok": True,
                "status": "rolled_back",
                "previous_commit": after["current_commit"],
                "safety_backup_id": safety_backup["id"],
                "restored_backup_id": selected_id if restored else None,
                "data_restored": bool(restored),
                "needs_restart": True,
                "database": self._backups.database_status(),
                "message": "代码已回退，请重启服务；数据按选择处理",
            }
        finally:
            self._update_lock.release()
