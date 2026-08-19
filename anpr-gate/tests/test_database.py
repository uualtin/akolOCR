import tempfile
import unittest
from pathlib import Path

from app.authorization.database import DuplicatePlateError, PlateDatabase


class PlateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = PlateDatabase(Path(self.temporary.name) / "anpr.db")
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_access_list_add_duplicate_and_delete(self):
        self.database.add_plate("34ABC123")
        self.assertTrue(self.database.contains("34ABC123"))
        with self.assertRaises(DuplicatePlateError):
            self.database.add_plate("34ABC123")
        self.assertTrue(self.database.delete_plate("34ABC123"))
        self.assertFalse(self.database.contains("34ABC123"))

    def test_audit_contains_only_plate_and_sysdate_payload(self):
        created = self.database.add_audit("34ABC123")
        self.assertEqual(created["plate"], "34ABC123")
        self.assertTrue(created["sysdate"])
        self.assertEqual(self.database.recent_audit(), [created])


if __name__ == "__main__":
    unittest.main()
