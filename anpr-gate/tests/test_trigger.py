import json
import unittest
from unittest.mock import patch

from app.trigger.http import HttpGateTrigger


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return b""


class HttpGateTriggerTests(unittest.TestCase):
    def test_posts_only_open_payload_to_selected_gate_url(self):
        captured = []

        def open_request(request, timeout):
            captured.append((request, timeout))
            return FakeResponse()

        with patch("app.trigger.http.urlopen", side_effect=open_request):
            trigger = HttpGateTrigger(
                "http://entry-relay.invalid/open", gate_id="entry"
            )
            trigger.open("34ABC123")

        request, timeout = captured[0]
        self.assertEqual(request.full_url, "http://entry-relay.invalid/open")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"plate": "34ABC123"})
        self.assertEqual(timeout, 3.0)


if __name__ == "__main__":
    unittest.main()
