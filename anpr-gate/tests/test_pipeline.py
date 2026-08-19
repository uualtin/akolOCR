import tempfile
import unittest
from pathlib import Path

from app.anpr.recognizer import Detection
from app.authorization.database import PlateDatabase
from app.pipeline import AnprPipeline
from app.trigger.base import GateTrigger


class RecordingTrigger(GateTrigger):
    def __init__(self):
        self.plates = []

    def open(self, plate: str) -> None:
        self.plates.append(plate)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = PlateDatabase(Path(self.temporary.name) / "anpr.db")
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def pipeline(self, trigger):
        return AnprPipeline(
            database=self.database,
            trigger=trigger,
            min_confidence=0.8,
            cooldown_seconds=60,
            min_plate_length=5,
            detection_confirm_frames=2,
            detection_confirm_seconds=2,
        )

    @staticmethod
    def detection(plate=" 34 abc-123 ", confidence=0.95):
        return Detection(plate, confidence, (1, 2, 10, 12))

    def test_unauthorized_plate_does_not_audit_or_open(self):
        trigger = RecordingTrigger()
        pipeline = self.pipeline(trigger)
        pipeline.process([self.detection()])
        pipeline.process([self.detection()])
        self.assertEqual(trigger.plates, [])
        self.assertEqual(self.database.recent_audit(), [])

    def test_authorized_plate_is_confirmed_audited_and_cooled_down(self):
        self.database.add_plate("34ABC123")
        trigger = RecordingTrigger()
        pipeline = self.pipeline(trigger)
        pipeline.process([self.detection()])
        pipeline.process([self.detection()])
        pipeline.process([self.detection()])
        self.assertEqual(trigger.plates, ["34ABC123"])
        self.assertEqual(
            [item["plate"] for item in self.database.recent_audit()], ["34ABC123"]
        )

    def test_entry_and_exit_pipelines_are_independent(self):
        self.database.add_plate("34ABC123")
        entry, exit_ = RecordingTrigger(), RecordingTrigger()
        entry_pipeline = AnprPipeline(
            database=self.database,
            trigger=entry,
            min_confidence=0.8,
            cooldown_seconds=60,
            detection_confirm_frames=1,
        )
        exit_pipeline = AnprPipeline(
            database=self.database,
            trigger=exit_,
            min_confidence=0.8,
            cooldown_seconds=60,
            detection_confirm_frames=1,
        )
        entry_pipeline.process([self.detection()])
        exit_pipeline.process([self.detection()])
        self.assertEqual(entry.plates, ["34ABC123"])
        self.assertEqual(exit_.plates, ["34ABC123"])
        self.assertEqual(len(self.database.recent_audit()), 2)


if __name__ == "__main__":
    unittest.main()
