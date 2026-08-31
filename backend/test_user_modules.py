import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import main
from user import routes, security
from user.auth import AuthService
from user.errors import AlreadyInitialized, AuthenticationRequired, Forbidden, InvalidCredentials, UserServiceError
from user import store


@contextmanager
def isolated_database(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class UserServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "users.db"
        self.clock_value = "2026-08-31T14:00:00+00:00"

        def now():
            return self.clock_value

        def database():
            return isolated_database(self.path)

        self.service = AuthService(database, now=now)

    def tearDown(self):
        self.directory.cleanup()

    def test_password_hash_is_salted_and_verifies(self):
        first = security.hash_password("correct horse")
        second = security.hash_password("correct horse")
        self.assertNotEqual(first, second)
        self.assertTrue(security.verify_password("correct horse", first))
        self.assertFalse(security.verify_password("wrong horse", first))
        self.assertFalse(security.verify_password("correct horse", "malformed"))
        # Invalid base64 in a persisted hash must be treated as a failed
        # credential check, not as a server error.
        self.assertFalse(security.verify_password("correct horse", "pbkdf2_sha256$240000$%%%$%%%"))

    def test_setup_creates_admin_session_and_rejects_second_setup(self):
        status = self.service.installation_status()
        self.assertTrue(status["needs_setup"])
        result = self.service.setup(
            {
                "username": "admin",
                "password": "password123",
                "display_name": "主管理员",
                "mode": "external",
                "allow_registration": True,
            },
            user_agent="test",
        )
        self.assertTrue(result["token"])
        self.assertEqual(result["user"]["role"], "admin")
        self.assertTrue(result["installation"]["auth_required"])
        self.assertTrue(self.service.authenticate({"Authorization": f"Bearer {result['token']}"}))
        with self.assertRaises(AlreadyInitialized):
            self.service.setup({"username": "other", "password": "password123", "mode": "self_use"})

    def test_login_register_expiry_and_logout(self):
        self.service.setup(
            {"username": "admin", "password": "password123", "mode": "external", "allow_registration": True}
        )
        login = self.service.login({"username": "admin", "password": "password123"})
        with self.assertRaises(InvalidCredentials):
            self.service.login({"username": "admin", "password": "bad-pass"})
        registered = self.service.register({"username": "reader", "password": "reader123"})
        self.assertEqual(registered["user"]["role"], "user")
        headers = {"Authorization": f"Bearer {login['token']}"}
        self.assertTrue(self.service.authenticate(headers))
        self.clock_value = "2026-09-01T15:00:01+00:00"
        self.assertIsNone(self.service.authenticate(headers))
        self.clock_value = "2026-08-31T14:00:00+00:00"
        fresh = self.service.login({"username": "admin", "password": "password123"})
        self.assertEqual(self.service.logout({"Authorization": f"Bearer {fresh['token']}"}), {"ok": True})
        self.assertIsNone(self.service.authenticate({"Authorization": f"Bearer {fresh['token']}"}))

    def test_registration_disabled_and_last_admin_guards(self):
        setup = self.service.setup({"username": "admin", "password": "password123", "mode": "external"})
        with self.assertRaises(Forbidden):
            self.service.register({"username": "reader", "password": "reader123"})
        with self.assertRaises(UserServiceError) as raised:
            self.service.delete_user(setup["user"]["id"], actor_id=999)
        self.assertEqual(raised.exception.code, "last_admin")
        created = self.service.create_user({"username": "second", "password": "second123", "role": "admin"})
        self.assertEqual(created["user"]["role"], "admin")
        self.service.delete_user(created["user"]["id"], actor_id=setup["user"]["id"])
        with self.assertRaises(UserServiceError) as raised:
            self.service.update_user(setup["user"]["id"], {"role": "user"}, actor_id=999)
        self.assertEqual(raised.exception.code, "last_admin")

    def test_settings_keep_install_timestamp_and_normalize_mode(self):
        setup = self.service.setup({"username": "admin", "password": "password123", "mode": "self_use"})
        stamp = setup["installation"]["initialized_at"]
        value = self.service.update_settings(
            {"system": {"mode": "public", "allow_registration": True, "session_ttl_hours": 48},
             "basic": {"site_name": "外部服务", "base_url": "https://example.test/"}}
        )
        self.assertEqual(value["mode"], "external")
        self.assertTrue(value["allow_registration"])
        self.assertEqual(value["system"]["session_ttl_hours"], 48)
        self.assertEqual(value["basic"]["base_url"], "https://example.test")
        self.assertEqual(self.service.installation_status()["initialized_at"], stamp)

    def test_legacy_enabled_admin_migrates_install_state(self):
        self.service.initialize()
        with self.service._database() as connection:
            stamp = self.clock_value
            connection.execute(
                "INSERT INTO users(username, password_hash, display_name, role, enabled, created_at, updated_at) VALUES(?, ?, ?, 'admin', 1, ?, ?)",
                ("legacy-admin", security.hash_password("password123"), "Legacy", stamp, stamp),
            )
        status = self.service.installation_status()
        self.assertTrue(status["initialized"])
        self.assertFalse(status["needs_setup"])
        with self.service._database() as connection:
            state = connection.execute("SELECT initialized, initialized_at FROM install_state WHERE id = 1").fetchone()
        self.assertEqual(state["initialized"], 1)
        self.assertEqual(state["initialized_at"], stamp)


class UserRoutePolicyTests(unittest.TestCase):
    def test_external_policy_distinguishes_public_user_and_admin(self):
        installation = {"mode": "external", "needs_setup": False}
        routes.authorization("GET", "/api/watches", installation=installation, principal=None)
        routes.authorization("GET", "/api/settings", installation=installation, principal={"role": "user"})
        with self.assertRaises(AuthenticationRequired):
            routes.authorization("POST", "/api/sub2api/import", installation=installation, principal=None)
        with self.assertRaises(Forbidden):
            routes.authorization("POST", "/api/sub2api/import", installation=installation, principal={"role": "user"})
        routes.authorization("POST", "/api/sub2api/import", installation=installation, principal={"role": "admin"})

    def test_aggregate_settings_redacts_system_for_ordinary_user(self):
        directory = tempfile.TemporaryDirectory()
        try:
            path = Path(directory.name) / "settings.db"
            service = AuthService(lambda: isolated_database(path), now=lambda: "2026-08-31T14:00:00+00:00")
            setup = service.setup({"username": "admin", "password": "password123", "mode": "external"})
            reader = service.create_user({"username": "reader", "password": "reader123"})
            responses = []
            routes.handle_get(
                "/api/settings",
                send_json=lambda value, status=200: responses.append((status, value)),
                service=service,
                headers={"Authorization": "Bearer " + "invalid"},
                principal=reader["user"],
            )
            self.assertEqual(responses[0][0], 200)
            self.assertIn("basic", responses[0][1])
            self.assertNotIn("system", responses[0][1])
        finally:
            directory.cleanup()

    def test_self_use_keeps_monitoring_public_but_protects_management(self):
        installation = {"mode": "self_use", "initialized": True, "needs_setup": False}
        routes.authorization("GET", "/api/watches", installation=installation, principal=None)
        routes.authorization("GET", "/api/preorders", installation=installation, principal=None)
        routes.authorization("GET", "/api/settings/checkout", installation=installation, principal=None)
        routes.authorization("PUT", "/api/settings/checkout", installation=installation, principal=None)
        routes.authorization("GET", "/api/redeem/config", installation=installation, principal=None)
        routes.authorization("PUT", "/api/redeem/config", installation=installation, principal=None)
        routes.authorization("POST", "/api/watches", installation=installation, principal=None)
        routes.authorization("POST", "/api/sub2api/automation/run", installation=installation, principal=None)
        with self.assertRaises(AuthenticationRequired):
            routes.authorization("GET", "/api/users", installation=installation, principal=None)
        with self.assertRaises(AuthenticationRequired):
            routes.authorization("GET", "/api/settings/system", installation=installation, principal=None)
        with self.assertRaises(Forbidden):
            routes.authorization("GET", "/api/sub2api/config", installation=installation, principal={"role": "user"})


class MainUserRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "main.db"

    def tearDown(self):
        self.directory.cleanup()

    def request_api(self, method, path, payload=None, headers=None):
        body = json.dumps(payload or {}).encode("utf-8")
        handler = object.__new__(main.ApiHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
        handler.rfile = BytesIO(body)
        responses = []
        handler._send_json = lambda data, status=200, *_args: responses.append((status, data))
        getattr(handler, f"do_{method}")()
        return responses[-1]

    def test_setup_login_and_external_admin_gate(self):
        with patch.object(main, "database", side_effect=lambda: isolated_database(self.path)):
            main.init_database()
            status, initial = self.request_api("GET", "/api/install/status")
            self.assertEqual(status, 200)
            self.assertTrue(initial["needs_setup"])
            setup_status, setup = self.request_api(
                "POST",
                "/api/install/setup",
                {"username": "admin", "password": "password123", "mode": "external"},
            )
            self.assertEqual(setup_status, 200)
            token = setup["token"]
            denied_status, denied = self.request_api("POST", "/api/sub2api/automation/run")
            self.assertEqual(denied_status, 401)
            self.assertEqual(denied["code"], "auth_required")
            allowed_status, allowed = self.request_api(
                "GET", "/api/users", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(allowed_status, 200)
            self.assertEqual(allowed["total"], 1)

    def test_auth_setup_alias_preorders_view_and_user_scoped_checkout(self):
        with patch.object(main, "database", side_effect=lambda: isolated_database(self.path)):
            main.init_database()
            setup_status, setup = self.request_api(
                "POST",
                "/api/auth/setup",
                {"username": "admin", "password": "password123", "mode": "external"},
            )
            self.assertEqual(setup_status, 200)
            admin_token = setup["token"]
            reader = main.USER_SERVICE.create_user({"username": "reader", "password": "reader123"})
            reader_login = main.USER_SERVICE.login({"username": "reader", "password": "reader123"})
            reader_headers = {"Authorization": f"Bearer {reader_login['token']}"}

            # The external ordinary-user view must not expose the shared
            # management queue, but it should not break the monitoring boot.
            preorders_status, preorders = self.request_api("GET", "/api/preorders", headers=reader_headers)
            self.assertEqual(preorders_status, 200)
            self.assertEqual(preorders, [])

            admin_profile_status, _ = self.request_api(
                "PUT",
                "/api/settings/checkout",
                {"contact": "admin@example.test", "query_password": "admin-secret"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(admin_profile_status, 200)
            reader_profile_status, reader_profile = self.request_api(
                "GET", "/api/settings/checkout", headers=reader_headers
            )
            self.assertEqual(reader_profile_status, 200)
            self.assertEqual(reader_profile["contact"], "")
            self.assertEqual(reader_profile["query_password"], "")

            save_status, saved = self.request_api(
                "PUT",
                "/api/settings/checkout",
                {"contact": "reader@example.test", "query_password": "reader-secret"},
                headers=reader_headers,
            )
            self.assertEqual(save_status, 200)
            self.assertEqual(saved["contact"], "reader@example.test")
            admin_read_status, admin_read = self.request_api(
                "GET", "/api/settings/checkout", headers={"Authorization": f"Bearer {admin_token}"}
            )
            self.assertEqual(admin_read_status, 200)
            self.assertEqual(admin_read["contact"], "admin@example.test")
            self.assertEqual(admin_read["query_password"], "admin-secret")

    def test_authenticated_user_can_change_own_password_and_old_session_is_revoked(self):
        with patch.object(main, "database", side_effect=lambda: isolated_database(self.path)):
            main.init_database()
            setup_status, setup = self.request_api(
                "POST",
                "/api/install/setup",
                {"username": "admin", "password": "password123", "mode": "external"},
            )
            self.assertEqual(setup_status, 200)
            reader = main.USER_SERVICE.create_user({"username": "reader", "password": "reader123"})
            reader_login = main.USER_SERVICE.login({"username": "reader", "password": "reader123"})
            headers = {"Authorization": f"Bearer {reader_login['token']}"}

            wrong_status, wrong = self.request_api(
                "POST",
                "/api/auth/password",
                {"current_password": "wrong-pass", "password": "reader456"},
                headers=headers,
            )
            self.assertEqual(wrong_status, 401)
            self.assertEqual(wrong["code"], "invalid_credentials")

            changed_status, changed = self.request_api(
                "POST",
                "/api/auth/password",
                {"current_password": "reader123", "password": "reader456"},
                headers=headers,
            )
            self.assertEqual(changed_status, 200)
            self.assertEqual(changed["user_id"], reader["user"]["id"])

            old_me_status, old_me = self.request_api("GET", "/api/auth/me", headers=headers)
            self.assertEqual(old_me_status, 200)
            self.assertFalse(old_me["authenticated"])

            new_login = main.USER_SERVICE.login({"username": "reader", "password": "reader456"})
            self.assertTrue(new_login["authenticated"])
