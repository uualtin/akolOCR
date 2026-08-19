import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from app.anpr.recognizer import Detection
from app.snapshots import SnapshotStore


class SnapshotStoreTests(unittest.TestCase):
    def test_atomic_annotation_crop_and_safe_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory), 10_000_000)
            frame = np.zeros((100, 200, 3), dtype=np.uint8)
            event_id = str(uuid.uuid4())
            full, crop, evicted = store.save(
                event_id,
                "entry",
                frame,
                Detection("34ABC123", 0.91, (25, 30, 125, 80)),
                datetime(2026, 8, 18, 12, tzinfo=UTC),
            )
            self.assertEqual(evicted, [])
            full_path = Path(directory) / full.removeprefix("local:")
            crop_path = Path(directory) / crop.removeprefix("local:")
            self.assertTrue(full_path.is_file())
            self.assertTrue(crop_path.is_file())
            decoded_crop = cv2.imread(str(crop_path))
            self.assertEqual(decoded_crop.shape[:2], (50, 100))
            self.assertNotEqual(SnapshotStore.checksum(full_path), "")
            with self.assertRaises(ValueError):
                store.save(
                    "../../unsafe",
                    "entry",
                    frame,
                    Detection("X", 1, (0, 0, 1, 1)),
                    datetime.now(UTC),
                )
            with self.assertRaises(ValueError):
                store.save(
                    str(uuid.uuid4()),
                    "../../unsafe",
                    frame,
                    Detection("X", 1, (0, 0, 1, 1)),
                    datetime.now(UTC),
                )

    def test_limit_evicts_oldest_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory), 1)
            frame = np.zeros((20, 20, 3), dtype=np.uint8)
            event_id = str(uuid.uuid4())
            _, _, evicted = store.save(
                event_id,
                "exit",
                frame,
                Detection("34ABC123", 1.0, (1, 1, 10, 10)),
                datetime.now(UTC),
            )
            self.assertIn(event_id, evicted)
            self.assertEqual(list(Path(directory).rglob("*.jpg")), [])


if __name__ == "__main__":
    unittest.main()
