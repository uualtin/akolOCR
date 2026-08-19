import logging
import unittest

from app.log_buffer import LogStore, WebLogHandler


class LogBufferTests(unittest.TestCase):
    def test_sources_can_be_filtered(self):
        store = LogStore(max_entries=3)
        handler = WebLogHandler(store)
        handler.emit(logging.LogRecord("anpr.camera.rtsp", logging.WARNING, "", 0, "offline", (), None))
        gate_record = logging.LogRecord("anpr.gate", logging.INFO, "", 0, "opened", (), None)
        handler.emit(gate_record)
        entry_record = logging.LogRecord("anpr.pipeline", logging.INFO, "", 0, "allowed", (), None)
        entry_record.camera_id = "entry"
        handler.emit(entry_record)
        self.assertEqual(store.recent("entry")[0]["message"], "allowed")
        self.assertEqual(store.recent("gate")[0]["message"], "opened")
        self.assertEqual(len(store.recent("all")), 3)


if __name__ == "__main__":
    unittest.main()
