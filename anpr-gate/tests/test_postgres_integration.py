import os
import unittest
import uuid
from datetime import UTC, datetime

from app.db import Database


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["TEST_DATABASE_URL"]
        if "_test" not in url:
            raise unittest.SkipTest("TEST_DATABASE_URL must name a dedicated *_test database")
        cls.database = Database(url)
        cls.database.initialize()
        cls.database.seed_camera_gate("entry", "Entry", "entry", "entry", "disabled")

    def test_sqlite_style_import_and_event_replay_are_idempotent(self):
        plate = f"T{uuid.uuid4().hex[:7].upper()}"
        self.database.import_access_entry(plate)
        self.database.import_access_entry(plate)
        matches = [row for row in self.database.active_access_entries() if row["plate"] == plate]
        self.assertEqual(len(matches), 1)

        event_id = str(uuid.uuid4())
        event = {
            "id": event_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": "automatic",
            "camera_id": "entry",
            "gate_id": "entry",
            "plate": plate,
            "confidence": 0.91,
            "trigger_status": "pending",
            "trigger_duration_ms": None,
            "trigger_error": None,
            "full_snapshot_path": "local:test_full.jpg",
            "crop_snapshot_path": "local:test_crop.jpg",
            "snapshot_status": "local",
            "requested_by": None,
            "manual_reason": None,
            "client_ip": None,
        }
        self.database.upsert_event(event)
        event["trigger_status"] = "success"
        event["trigger_duration_ms"] = 123
        self.database.upsert_event(event)
        stored = self.database.get_event(event_id)
        self.assertEqual(stored["trigger_status"], "success")
        self.assertEqual(stored["trigger_duration_ms"], 123)


if __name__ == "__main__":
    unittest.main()
