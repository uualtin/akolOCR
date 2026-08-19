import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import CameraSettings, Settings
from app.main import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = Settings(
            cameras=(
                CameraSettings("entry", "Giriş", "", ""),
                CameraSettings("exit", "Çıkış", "", ""),
            ),
            gate_trigger_type="console",
            anpr_process_fps=1.5,
            min_confidence=0.8,
            min_plate_length=5,
            detection_confirm_frames=2,
            detection_confirm_seconds=2,
            trigger_cooldown_seconds=15,
            database_path=Path(self.temporary.name) / "anpr.db",
            api_host="127.0.0.1",
            api_port=8000,
        )
        self.app = create_app(settings, start_cameras=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_dashboard_access_list_and_audit_api(self):
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Giriş kamerası", dashboard.text)
        self.assertIn("Çıkış kamerası", dashboard.text)

        created = self.client.post(
            "/plates", data={"plate": "34 abc-123"}, follow_redirects=False
        )
        self.assertEqual(created.status_code, 303)
        self.assertTrue(self.app.state.context.database.contains("34ABC123"))

        self.app.state.context.database.add_audit("34ABC123")
        audit = self.client.get("/api/audit")
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()[0]["plate"], "34ABC123")

        deleted = self.client.post(
            "/plates/34ABC123/delete", follow_redirects=False
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertFalse(self.app.state.context.database.contains("34ABC123"))

    def test_health_and_camera_errors(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["cameras"]["entry"]["online"])
        self.assertEqual(self.client.get("/video-feed/unknown.mjpeg").status_code, 404)
        self.assertEqual(self.client.get("/video-feed/entry.mjpeg").status_code, 503)


if __name__ == "__main__":
    unittest.main()
