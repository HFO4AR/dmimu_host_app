import tempfile
import unittest

from dmimu.service import Service
from dmimu.storage import Settings
from dmimu.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(self.temp.name)
        self.service = Service(self.settings, port_provider=lambda: [])
        self.app = create_app(self.settings, self.service)
        self.client = self.app.test_client()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def csrf(self):
        return self.client.get("/api/session").json["data"]["csrf"]

    def test_agent_requires_separate_credentials(self):
        self.assertEqual(self.client.get("/api/agent/v1/status").status_code, 401)
        headers = {"Authorization": "Bearer " + self.settings.values["agent_token"]}
        self.assertTrue(self.client.get("/api/agent/v1/status", headers=headers).json["ok"])

    def test_shared_trajectory_requires_auth_and_valid_indices(self):
        self.assertEqual(self.client.get("/api/agent/v1/trajectory").status_code, 401)
        headers = {"Authorization": "Bearer " + self.settings.values["agent_token"]}
        data = self.client.get("/api/agent/v1/trajectory?after=0&epoch=0", headers=headers).json["data"]
        self.assertEqual(data["points"], [])
        self.assertIn("status", data)
        for query in ("after=-1", "after=12001", "epoch=abc"):
            self.assertEqual(self.client.get("/api/agent/v1/trajectory?" + query, headers=headers).status_code, 400)

    def test_browser_mutation_requires_csrf(self):
        data = {"action": "demo"}
        self.assertEqual(self.client.post("/api/v1/actions", json=data).status_code, 403)
        response = self.client.post("/api/v1/actions", json=data, headers={"X-CSRF-Token": self.csrf(), "Idempotency-Key": "browser-demo"})
        self.assertEqual(response.status_code, 202)

    def test_nonlocal_browser_requires_login(self):
        r = self.client.get("/api/v1/status", environ_overrides={"REMOTE_ADDR": "192.168.6.10"})
        self.assertEqual(r.status_code, 401)

    def test_cross_origin_write_is_rejected(self):
        response = self.client.post("/api/v1/settings", json={}, headers={"Origin": "https://evil.example", "X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 403)

    def test_settings_never_expose_tokens_or_password_hash(self):
        data = self.client.get("/api/v1/settings").json["data"]
        self.assertNotIn("agent_token", data)
        self.assertNotIn("password_hash", data)
        self.assertNotIn("secret", data)

    def test_lan_enabling_requires_password(self):
        r = self.client.post("/api/v1/settings", json={"lan_enabled": True}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.settings.values["lan_enabled"])

    def test_setting_validation_is_atomic(self):
        r = self.client.post("/api/v1/settings", json={"agent_enabled": False, "baudrate": 42}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.settings.values["agent_enabled"])

    def test_password_change_revokes_existing_remote_sessions(self):
        from werkzeug.security import generate_password_hash
        self.settings.values["password_hash"] = generate_password_hash("old-password")
        remote = self.app.test_client()
        lan = {"REMOTE_ADDR": "192.168.6.10"}
        self.assertEqual(remote.post("/api/login", json={"password": "old-password"}, environ_overrides=lan).status_code, 200)
        self.assertEqual(remote.get("/api/v1/status", environ_overrides=lan).status_code, 200)
        changed = self.client.post("/api/v1/settings", json={"password": "new-password"}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(remote.get("/api/v1/status", environ_overrides=lan).status_code, 401)

    def test_remote_agent_requires_https(self):
        response = self.client.get("/api/agent/v1/status", headers={"Authorization": "Bearer " + self.settings.values["agent_token"]}, environ_overrides={"REMOTE_ADDR": "192.168.6.10"})
        self.assertEqual(response.json["error"]["code"], "HTTPS_REQUIRED")

    def test_agent_cannot_change_access_settings(self):
        response = self.client.post("/api/agent/v1/settings", json={"agent_enabled": False}, headers={"Authorization": "Bearer " + self.settings.values["agent_token"]})
        self.assertEqual(response.status_code, 404)

    def test_json_arrays_and_malformed_data_report_json_errors(self):
        for value in ([], None):
            response = self.client.post("/api/v1/actions", json=value, headers={"X-CSRF-Token": self.csrf()})
            self.assertFalse(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
