import unittest
from unittest.mock import patch

from app.anpr.normalize import normalize_plate
from app.anpr.recognizer import Detection
from app.worker import DetectionSelector


class DetectionSelectorTests(unittest.TestCase):
    def test_normalization_removes_spaces_and_punctuation(self):
        self.assertEqual(normalize_plate(" 34 abc-123 "), "34ABC123")

    def test_confirmation_confidence_length_and_cooldown(self):
        selector = DetectionSelector(0.8, 5, 2, 2.0, 15.0)
        raw = [Detection(" 34 ab-123 ", 0.95, (1, 2, 10, 12))]
        with patch("app.worker.time.monotonic", return_value=100.0):
            normalized = selector.normalize(raw)
            self.assertEqual(normalized[0].text, "34AB123")
            self.assertEqual(selector.confirmed(normalized), [])
        with patch("app.worker.time.monotonic", return_value=101.0):
            self.assertEqual(selector.confirmed(normalized), normalized)
            selector.mark("34AB123")
        with patch("app.worker.time.monotonic", return_value=102.0):
            self.assertEqual(selector.confirmed(normalized), [])
        with patch("app.worker.time.monotonic", return_value=117.0):
            self.assertEqual(selector.confirmed(normalized), [])
        with patch("app.worker.time.monotonic", return_value=118.0):
            self.assertEqual(selector.confirmed(normalized), normalized)

        rejected = selector.normalize([Detection("A-1", 1.0, (0, 0, 1, 1))])
        self.assertEqual(rejected, [])
        self.assertEqual(
            selector.confirmed([Detection("34LOW1", 0.2, (0, 0, 1, 1))]), []
        )


if __name__ == "__main__":
    unittest.main()
