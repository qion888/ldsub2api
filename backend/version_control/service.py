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

from .errors import VersionControlError


DEFAULT_REPOSITORY_URL = "https://github.com/qion888/ldsub2api.git"
DEFAULT_BRANCH = "main"
MAX_GITHUB_RESPONSE_BYTES = 1024 * 1024
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
    ) -> None:
        self.config = config
        self._runner = runner
        self._opener = opener
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._update_lock = threading.Lock()

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

    def _read_current_version(self) -> str:
        try:
            value = self.config.version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "dev"
        return value[:80] or "dev"

    def _repository_state(self) -> dict[str, Any]:
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
        }

    def version_info(self) -> dict[str, Any]:
        state = self._repository_state()
        return {
            "ok": True,
            **state,
            "status": "ready" if state["can_update"] else "blocked",
        }

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

    def check_updates(self) -> dict[str, Any]:
        state = self._repository_state()
        branch = quote(self.config.branch, safe="")
        latest = self._github_json(f"commits/{branch}")
        if not isinstance(latest, dict) or not re.fullmatch(r"[0-9a-fA-F]{40}", str(latest.get("sha") or "")):
            raise VersionControlError(
                "GitHub 未返回有效的最新提交",
                code="github_invalid_commit",
                status=502,
                retryable=True,
            )
        latest_commit = str(latest["sha"]).lower()
        release = self._github_json("releases/latest", allow_not_found=True)
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
            latest_version = self._latest_version_file() or state["current_version"]

        ahead_by = 0
        behind_by = 0
        if state["current_commit"].lower() != latest_commit:
            current = quote(state["current_commit"], safe="")
            comparison = self._github_json(f"compare/{current}...{branch}", allow_not_found=True)
            if isinstance(comparison, dict):
                try:
                    ahead_by = max(0, int(comparison.get("ahead_by") or 0))
                    behind_by = max(0, int(comparison.get("behind_by") or 0))
                except (TypeError, ValueError):
                    ahead_by = behind_by = 0

        if state["current_commit"].lower() == latest_commit:
            check_status = "up_to_date"
            message = "当前已是 GitHub 最新版本"
        elif ahead_by > 0 and behind_by == 0:
            check_status = "update_available"
            message = f"发现 {ahead_by} 个可用更新提交"
        elif behind_by > 0 and ahead_by == 0:
            check_status = "local_ahead"
            message = "本地版本领先于 GitHub 主分支"
        elif ahead_by > 0 and behind_by > 0:
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
            "ahead_by": ahead_by,
            "behind_by": behind_by,
            "release": release_info,
            "checked_at": self._now().isoformat(timespec="seconds"),
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
            remote_ref = f"refs/remotes/{self.config.remote}/{self.config.branch}"
            fetch_spec = f"refs/heads/{self.config.branch}:{remote_ref}"
            self._run_git("fetch", "--prune", self.config.remote, fetch_spec)

            # Fetch can take time, so verify the mutable state again before merging.
            current = self._repository_state()
            self._require_update_ready(current)
            remote_commit = self._git_text("rev-parse", remote_ref)
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

            self._run_git("merge", "--ff-only", remote_ref)
            after = self._repository_state()
            if after["current_commit"] != remote_commit:
                raise VersionControlError(
                    "Git 更新完成后提交校验失败",
                    code="update_verification_failed",
                    status=500,
                )
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
            }
        finally:
            self._update_lock.release()
