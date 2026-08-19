import json
import tempfile
import time
import unittest
from pathlib import Path

from app.worker_state import WorkerState


class WorkerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = WorkerState(Path(self.temp.name) / "worker.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_is_fail_closed_when_missing_or_stale(self):
        self.assertFalse(self.state.is_allowed("34ABC123", 7 * 86400))
        self.state.replace_access([{"plate": "34ABC123"}])
        self.assertTrue(self.state.is_allowed("34ABC123", 7 * 86400))
        with self.state._connect() as conn:
            conn.execute(
                "UPDATE state_meta SET value=? WHERE key='access_refreshed_at'",
                (str(time.time() - 7 * 86400 - 1),),
            )
        self.assertFalse(self.state.is_allowed("34ABC123", 7 * 86400))

    def test_cooldown_survives_process_restart(self):
        self.assertFalse(self.state.is_in_cooldown("34ABC123", 15))
        self.state.mark_triggered("34ABC123")
        reopened = WorkerState(self.state.path)
        self.assertTrue(reopened.is_in_cooldown("34ABC123", 15))

    def test_outbox_upsert_and_eviction_status(self):
        event = {"id": "event-1", "snapshot_status": "local", "value": 1}
        self.state.enqueue_event(event)
        event["value"] = 2
        self.state.enqueue_event(event)
        self.assertEqual(self.state.outbox_count(), 1)
        self.state.mark_snapshot_evicted("event-1")
        pending = self.state.pending_events()
        self.assertEqual(pending[0]["value"], 2)
        self.assertEqual(pending[0]["snapshot_status"], "evicted")
        self.state.delete_event("event-1")
        self.assertEqual(self.state.outbox_count(), 0)

        self.state.mark_snapshot_evicted("already-in-postgres")
        self.assertEqual(
            self.state.pending_snapshot_updates(),
            [("already-in-postgres", "evicted")],
        )
        self.assertEqual(self.state.outbox_count(), 1)
        self.state.delete_snapshot_update("already-in-postgres")
        self.assertEqual(self.state.outbox_count(), 0)


if __name__ == "__main__":
    unittest.main()
