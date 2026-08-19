import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from app.gate import ProductionGate


class FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, _): return b""


class GateTests(unittest.TestCase):
    def test_unknown_driver_is_rejected(self):
        with self.assertRaises(ValueError):
            ProductionGate("typo")

    def test_disabled_driver_never_calls_network(self):
        result = ProductionGate("disabled").open(
            event_id="id-1", gate_id="entry", source="automatic", plate="34ABC123"
        )
        self.assertEqual(result.status, "disabled")

    def test_http_driver_sends_auth_and_idempotency(self):
        captured = []

        def open_request(request, timeout):
            captured.append((request, timeout))
            return FakeResponse()

        with patch("app.gate.urlopen", side_effect=open_request):
            event_id = "0440dd8e-e960-40c8-83f4-d7f95b480c34"
            gate = ProductionGate("http", "https://relay.invalid/open", "secret-token")
            result = gate.open(
                event_id=event_id,
                gate_id="entry",
                source="automatic",
                plate="34ABC123",
            )
            self.assertEqual(result.status, "success")
            request, timeout = captured[0]
            headers = {key.lower(): value for key, value in request.header_items()}
            payload = json.loads(request.data)
            self.assertEqual(headers["authorization"], "Bearer secret-token")
            self.assertEqual(headers["idempotency-key"], event_id)
            self.assertEqual(payload["plate"], "34ABC123")
            self.assertEqual(timeout, 3)

    def test_http_failure_is_bounded_and_not_retried(self):
        gate = ProductionGate("http", "https://relay.invalid/open", "secret-token")
        with patch("app.gate.urlopen", side_effect=URLError("offline")) as mocked:
            result = gate.open(event_id="id-2", gate_id="exit", source="manual")
        self.assertEqual(result.status, "failed")
        self.assertIn("URLError", result.error)
        self.assertNotIn("secret-token", result.error)
        mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
