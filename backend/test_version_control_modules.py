import base64
import io
import json
import sqlite3
import stat
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from user import routes as user_routes
from user.errors import AuthenticationRequired, Forbidden
from version_control.archive import ArchiveUpdateManager
from version_control.errors import VersionControlError
from version_control.routes import handle_delete, handle_get, handle_post
from version_control.service import RepositoryConfig, VersionControlService


CURRENT_COMMIT = "a" * 40
LATEST_COMMIT = "b" * 40
REPOSITORY_URL = "https://github.com/qion888/ldsub2api.git"


class GitStateRunner:
    def __init__(self, *, dirty: str = "", branch: str = "main") -> None:
        self.current_commit = CURRENT_COMMIT
        self.remote_commit = LATEST_COMMIT
        self.dirty = dirty
        self.branch = branch
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        arguments = tuple(command[3:])
        returncode = 0
        stdout = ""
        stderr = ""
        if arguments == ("rev-parse", "--is-inside-work-tree"):
            stdout = "true\n"
        elif arguments == ("rev-parse", "HEAD"):
            stdout = f"{self.current_commit}\n"
        elif arguments == ("branch", "--show-current"):
            stdout = f"{self.branch}\n"
        elif arguments == ("remote", "get-url", "origin"):
            stdout = f"{REPOSITORY_URL}\n"
        elif arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            stdout = self.dirty
        elif arguments in {
            ("fetch", "--prune", "origin", "refs/heads/main:refs/remotes/origin/main"),
            ("fetch", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main"),
        }:
            pass
        elif arguments == ("rev-parse", "refs/remotes/origin/main"):
            stdout = f"{self.remote_commit}\n"
        elif arguments == ("rev-list", "--left-right", "--count", f"{self.current_commit}...{self.remote_commit}"):
            stdout = "0 3\n"
        elif arguments == ("show", "refs/remotes/origin/main:VERSION"):
            stdout = "2.2.0\n"
        elif arguments == ("merge-base", "--is-ancestor", self.current_commit, self.remote_commit):
            returncode = 0
        elif arguments == ("merge", "--ff-only", "refs/remotes/origin/main"):
            self.current_commit = self.remote_commit
        elif arguments == ("reset", "--hard", CURRENT_COMMIT):
            self.current_commit = CURRENT_COMMIT
        else:
            raise AssertionError(f"Unexpected git command: {arguments}")
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode("utf-8")


class RawResponse(FakeResponse):
    def read(self, _limit):
        return self.payload


class GitHubOpener:
    def __init__(self):
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        url = request.full_url
        if url.endswith("/commits/main"):
            return FakeResponse({"sha": LATEST_COMMIT})
        if url.endswith("/releases/latest"):
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        if "/contents/VERSION?ref=main" in url:
            content = base64.b64encode(b"2.2.0\n").decode("ascii")
            return FakeResponse({"encoding": "base64", "content": content})
        if url.endswith(f"/compare/{CURRENT_COMMIT}...main"):
            return FakeResponse({"ahead_by": 3, "behind_by": 0})
        raise AssertionError(f"Unexpected GitHub URL: {url}")


class VersionControlServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".git").mkdir()
        (self.root / "VERSION").write_text("2.1.0\n", encoding="utf-8")
        (self.root / "backend").mkdir()
        connection = sqlite3.connect(self.root / "backend" / "monitor.db")
        connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO settings(value) VALUES ('fixture')")
        connection.commit()
        connection.close()
        self.config = RepositoryConfig(root=self.root, repository_url=REPOSITORY_URL)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_version_info_reports_repository_and_update_readiness(self):
        runner = GitStateRunner()
        service = VersionControlService(self.config, runner=runner)

        result = service.version_info()

        self.assertEqual(result["current_version"], "2.1.0")
        self.assertEqual(result["current_short_commit"], "aaaaaaaa")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["repository_url"], "https://github.com/qion888/ldsub2api")
        self.assertTrue(result["worktree_clean"])
        self.assertTrue(result["repository_matches"])
        self.assertTrue(result["can_update"])

    def test_check_updates_uses_branch_when_repository_has_no_release(self):
        runner = GitStateRunner()
        opener = GitHubOpener()
        service = VersionControlService(
            self.config,
            runner=runner,
            opener=opener,
            now=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
        )

        result = service.check_updates()

        self.assertEqual(result["status"], "update_available")
        self.assertTrue(result["update_available"])
        self.assertTrue(result["update_ready"])
        self.assertEqual(result["latest_version"], "2.2.0")
        self.assertEqual(result["latest_commit"], LATEST_COMMIT)
        self.assertEqual(result["ahead_by"], 3)
        self.assertIsNone(result["release"])
        self.assertEqual(result["checked_at"], "2026-08-31T10:00:00+00:00")

    def test_install_update_fetches_and_fast_forwards_then_verifies_head(self):
        runner = GitStateRunner()
        service = VersionControlService(self.config, runner=runner)

        result = service.install_update()

        self.assertTrue(result["updated"])
        self.assertTrue(result["needs_restart"])
        self.assertEqual(result["previous_commit"], CURRENT_COMMIT)
        self.assertEqual(result["current_commit"], LATEST_COMMIT)
        commands = [command[3:] for command in runner.commands]
        self.assertIn(
            ["fetch", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main"],
            commands,
        )
        self.assertIn(["merge", "--ff-only", "refs/remotes/origin/main"], commands)

    def test_install_update_refuses_dirty_worktree_before_fetch(self):
        runner = GitStateRunner(dirty=" M backend/main.py\n?? local.txt\n")
        service = VersionControlService(self.config, runner=runner)

        with self.assertRaises(VersionControlError) as raised:
            service.install_update()

        self.assertEqual(raised.exception.code, "dirty_worktree")
        self.assertFalse(any(command[3:4] == ["fetch"] for command in runner.commands))

    def test_manual_backup_restore_and_safety_backup_preserve_sqlite_data(self):
        runner = GitStateRunner()
        service = VersionControlService(self.config, runner=runner)
        created = service.create_backup()
        backup_id = created["backup"]["id"]
        database_path = self.root / "backend" / "monitor.db"
        connection = sqlite3.connect(database_path)
        connection.execute("UPDATE settings SET value = 'changed'")
        connection.commit()
        connection.close()

        restored = service.restore_backup(backup_id)

        self.assertEqual(restored["status"], "restored")
        self.assertTrue(restored["safety_backup_id"])
        connection = sqlite3.connect(database_path)
        self.assertEqual(connection.execute("SELECT value FROM settings").fetchone()[0], "fixture")
        connection.close()
        self.assertEqual(service.list_backups()["total"], 2)

    def test_rollback_update_resets_code_without_restoring_data_by_default(self):
        runner = GitStateRunner()
        runner.current_commit = LATEST_COMMIT
        service = VersionControlService(self.config, runner=runner)
        backup = service.create_backup()["backup"]
        service._write_last_update({
            "backup_id": backup["id"],
            "previous_commit": CURRENT_COMMIT,
            "updated_commit": LATEST_COMMIT,
            "previous_version": "2.1.0",
            "updated_version": "2.2.0",
        })

        result = service.rollback_update()

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["previous_commit"], CURRENT_COMMIT)
        self.assertFalse(result["data_restored"])
        self.assertTrue(result["safety_backup_id"])
        self.assertTrue(result["needs_restart"])

    def test_delete_backup_removes_verified_files(self):
        service = VersionControlService(self.config, runner=GitStateRunner())
        created = service.create_backup()["backup"]

        result = service.delete_backup(created["id"])

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["deleted_backup_id"], created["id"])
        self.assertEqual(result["backups"]["total"], 0)
        self.assertFalse(Path(created["path"]).exists())

    def test_delete_backup_protects_last_update_rollback_backup(self):
        service = VersionControlService(self.config, runner=GitStateRunner())
        created = service.create_backup()["backup"]
        service._write_last_update({"backup_id": created["id"], "can_rollback": True})

        with self.assertRaises(VersionControlError) as raised:
            service.delete_backup(created["id"])

        self.assertEqual(raised.exception.code, "backup_in_use")
        self.assertTrue(Path(created["path"]).exists())


class ArchiveInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "VERSION").write_text("2.1.0\n", encoding="utf-8")
        (self.root / "backend").mkdir()
        (self.root / "backend" / "main.py").write_text("OLD_CODE = True\n", encoding="utf-8")
        (self.root / "frontend").mkdir()
        (self.root / "frontend" / "package.json").write_text('{"version":"old"}\n', encoding="utf-8")
        (self.root / "start.ps1").write_text("Write-Host old\n", encoding="utf-8")
        (self.root / ".runtime").mkdir()
        (self.root / ".runtime" / "preserved.txt").write_text("keep", encoding="utf-8")
        connection = sqlite3.connect(self.root / "backend" / "monitor.db")
        connection.execute("CREATE TABLE settings (value TEXT)")
        connection.execute("INSERT INTO settings(value) VALUES ('fixture')")
        connection.commit()
        connection.close()
        self.config = RepositoryConfig(root=self.root, repository_url=REPOSITORY_URL)
        self.archive_payload = self._archive()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _archive(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ldsub2api-main/VERSION", "2.2.0\n")
            archive.writestr("ldsub2api-main/backend/main.py", "NEW_CODE = True\n")
            archive.writestr("ldsub2api-main/frontend/package.json", '{"version":"new"}\n')
            archive.writestr("ldsub2api-main/start.ps1", "Write-Host new\n")
            archive.writestr("ldsub2api-main/.runtime/preserved.txt", "overwrite")
            archive.writestr("ldsub2api-main/backend/monitor.db", b"not-a-database")
        return stream.getvalue()

    def _opener(self, request, timeout):
        url = request.full_url
        if url.endswith("/commits/main"):
            return FakeResponse({"sha": LATEST_COMMIT})
        if "/contents/VERSION?ref=main" in url:
            content = base64.b64encode(b"2.2.0\n").decode("ascii")
            return FakeResponse({"encoding": "base64", "content": content})
        if url.endswith("/zip/main"):
            return RawResponse(self.archive_payload)
        raise AssertionError(f"Unexpected GitHub URL: {url}")

    @staticmethod
    def _no_git(command, **kwargs):
        raise AssertionError(f"Archive installation must not invoke Git: {command}")

    def test_archive_installation_reports_update_without_invoking_git(self):
        service = VersionControlService(self.config, runner=self._no_git, opener=self._opener)

        initial = service.version_info()
        checked = service.check_updates()

        self.assertEqual(initial["installation_mode"], "archive")
        self.assertTrue(initial["can_update"])
        self.assertEqual(checked["status"], "update_available")
        self.assertEqual(checked["comparison_source"], "github-version")
        self.assertEqual(checked["latest_version"], "2.2.0")

    def test_archive_update_preserves_data_and_supports_code_rollback(self):
        service = VersionControlService(self.config, runner=self._no_git, opener=self._opener)

        updated = service.install_update()

        self.assertTrue(updated["updated"])
        self.assertEqual(updated["installation_mode"], "archive")
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8").strip(), "2.2.0")
        self.assertEqual((self.root / "backend" / "main.py").read_text(encoding="utf-8"), "NEW_CODE = True\n")
        self.assertEqual((self.root / ".runtime" / "preserved.txt").read_text(encoding="utf-8"), "keep")
        connection = sqlite3.connect(self.root / "backend" / "monitor.db")
        self.assertEqual(connection.execute("SELECT value FROM settings").fetchone()[0], "fixture")
        connection.execute("UPDATE settings SET value = 'after-update'")
        connection.commit()
        connection.close()

        rolled_back = service.rollback_update()

        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertEqual(rolled_back["installation_mode"], "archive")
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8").strip(), "2.1.0")
        self.assertEqual((self.root / "backend" / "main.py").read_text(encoding="utf-8"), "OLD_CODE = True\n")
        connection = sqlite3.connect(self.root / "backend" / "monitor.db")
        self.assertEqual(connection.execute("SELECT value FROM settings").fetchone()[0], "after-update")
        connection.close()

    def test_archive_update_restores_code_and_metadata_when_finalization_fails(self):
        service = VersionControlService(self.config, runner=self._no_git, opener=self._opener)
        service._archives.write_installed_state(
            repository_url=self.config.github_url,
            branch=self.config.branch,
            commit=CURRENT_COMMIT,
            version="2.1.0",
        )
        previous_installed = service._archives.installed_state_path.read_bytes()
        service._write_last_update({"marker": "previous"})
        previous_last_update = service._last_update_path.read_bytes()
        original_write = service._write_last_update

        def fail_after_write(payload):
            original_write(payload)
            raise OSError("simulated finalization failure")

        service._write_last_update = fail_after_write
        with self.assertRaises(VersionControlError) as raised:
            service.install_update()

        self.assertEqual(raised.exception.code, "archive_update_finalize_failed")
        self.assertIn("代码已自动恢复", raised.exception.detail)
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8").strip(), "2.1.0")
        self.assertEqual((self.root / "backend" / "main.py").read_text(encoding="utf-8"), "OLD_CODE = True\n")
        self.assertEqual(service._archives.installed_state_path.read_bytes(), previous_installed)
        self.assertEqual(service._last_update_path.read_bytes(), previous_last_update)


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = ArchiveUpdateManager(self.root, now=lambda: datetime.now(timezone.utc))

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _payload(extra_entries=(), *, include_required=True):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if include_required:
                archive.writestr("project-main/VERSION", "2.2.0\n")
                archive.writestr("project-main/backend/main.py", "APP = True\n")
                archive.writestr("project-main/frontend/package.json", "{}\n")
                archive.writestr("project-main/start.ps1", "Write-Host start\n")
            for name, value in extra_entries:
                archive.writestr(name, value)
        return stream.getvalue()

    def test_rejects_path_traversal(self):
        payload = self._payload((("project-main/../outside.py", "bad"),))

        with self.assertRaises(VersionControlError) as raised:
            self.manager.inspect(payload)

        self.assertEqual(raised.exception.code, "archive_path_invalid")
        self.assertFalse((self.root.parent / "outside.py").exists())

    def test_rejects_symbolic_links(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project-main/VERSION", "2.2.0\n")
            archive.writestr("project-main/backend/main.py", "APP = True\n")
            archive.writestr("project-main/frontend/package.json", "{}\n")
            archive.writestr("project-main/start.ps1", "Write-Host start\n")
            link = zipfile.ZipInfo("project-main/backend/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../VERSION")

        with self.assertRaises(VersionControlError) as raised:
            self.manager.inspect(stream.getvalue())

        self.assertEqual(raised.exception.code, "archive_entry_invalid")

    def test_rejects_archives_missing_project_files(self):
        payload = self._payload((("project-main/README.md", "incomplete"),), include_required=False)

        with self.assertRaises(VersionControlError) as raised:
            self.manager.inspect(payload)

        self.assertEqual(raised.exception.code, "archive_project_invalid")


class VersionControlRouteTests(unittest.TestCase):
    def request_get(self, principal):
        responses = []
        handled = handle_get(
            "/api/version",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal=principal,
            version_info=lambda: {"ok": True, "current_version": "2.1.0"},
        )
        return handled, responses[-1]

    def test_routes_require_an_administrator(self):
        handled, response = self.request_get(None)
        self.assertTrue(handled)
        self.assertEqual(response[0], 401)

        _, response = self.request_get({"role": "user"})
        self.assertEqual(response[0], 403)

        _, response = self.request_get({"role": "admin"})
        self.assertEqual(response, (200, {"ok": True, "current_version": "2.1.0"}))

    def test_post_routes_dispatch_check_and_update(self):
        responses = []
        calls = []
        handled = handle_post(
            "/api/version/update",
            {},
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal={"role": "admin"},
            check_updates=lambda: calls.append("check"),
            install_update=lambda: calls.append("update") or {"updated": True},
        )
        self.assertTrue(handled)
        self.assertEqual(calls, ["update"])
        self.assertEqual(responses[-1], (200, {"updated": True}))

    def test_post_routes_dispatch_backup_restore_and_rollback_payloads(self):
        responses = []
        calls = []
        handled = handle_post(
            "/api/version/restore",
            {"backup_id": "backup-1"},
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal={"role": "admin"},
            check_updates=lambda: {},
            install_update=lambda: {},
            restore_backup=lambda backup_id: calls.append(("restore", backup_id)) or {"restored": True},
        )
        self.assertTrue(handled)
        self.assertEqual(calls, [("restore", "backup-1")])
        self.assertEqual(responses[-1], (200, {"restored": True}))

        handled = handle_post(
            "/api/version/rollback",
            {"backup_id": "backup-1", "restore_data": True},
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal={"role": "admin"},
            check_updates=lambda: {},
            install_update=lambda: {},
            rollback_update=lambda backup_id, restore_data=False: calls.append(("rollback", backup_id, restore_data)) or {"rolled_back": True},
        )
        self.assertTrue(handled)
        self.assertEqual(calls[-1], ("rollback", "backup-1", True))

    def test_delete_route_dispatches_backup_deletion_and_requires_admin(self):
        responses = []
        calls = []
        handled = handle_delete(
            "/api/version/backups/backup-1234",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal={"role": "admin"},
            delete_backup=lambda backup_id: calls.append(backup_id) or {"deleted": True},
        )

        self.assertTrue(handled)
        self.assertEqual(calls, ["backup-1234"])
        self.assertEqual(responses[-1], (200, {"deleted": True}))

        responses.clear()
        self.assertTrue(handle_delete(
            "/api/version/backups/backup-1234",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            principal={"role": "user"},
            delete_backup=lambda _backup_id: {"deleted": True},
        ))
        self.assertEqual(responses[-1][0], 403)

    def test_global_policy_keeps_version_operations_admin_only(self):
        installation = {"initialized": True, "needs_setup": False, "mode": "self_use"}
        with self.assertRaises(AuthenticationRequired):
            user_routes.authorization("GET", "/api/version", installation=installation, principal=None)
        with self.assertRaises(Forbidden):
            user_routes.authorization(
                "POST",
                "/api/version/update",
                installation=installation,
                principal={"role": "user"},
            )
        user_routes.authorization(
            "POST",
            "/api/version/update",
            installation=installation,
            principal={"role": "admin"},
        )
        with self.assertRaises(AuthenticationRequired):
            user_routes.authorization(
                "DELETE",
                "/api/version/backups/backup-1234",
                installation=installation,
                principal=None,
            )
        with self.assertRaises(Forbidden):
            user_routes.authorization(
                "DELETE",
                "/api/version/backups/backup-1234",
                installation=installation,
                principal={"role": "user"},
            )
        user_routes.authorization(
            "DELETE",
            "/api/version/backups/backup-1234",
            installation=installation,
            principal={"role": "admin"},
        )


if __name__ == "__main__":
    unittest.main()
