import os
import unittest
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_component_camera_settings_encode_credentials(self):
        environment = {
            "ENTRY_RTSP_URL": "",
            "EXIT_RTSP_URL": "",
            "RTSP_URL": "",
            "ENTRY_RTSP_HOST": "192.168.254.115",
            "ENTRY_RTSP_USERNAME": "admin@example",
            "ENTRY_RTSP_PASSWORD": "p@ss:word",
            "ENTRY_RTSP_PATH": "Streaming/Channels/102",
            "EXIT_RTSP_HOST": "192.168.254.116",
            "EXIT_RTSP_USERNAME": "admin",
            "EXIT_RTSP_PASSWORD": "exit pass",
            "EXIT_RTSP_PATH": "Streaming/Channels/102",
            "GATE_TRIGGER_TYPE": "console",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_env()
        self.assertEqual(
            settings.cameras[0].rtsp_url,
            "rtsp://admin%40example:p%40ss%3Aword@192.168.254.115:554/Streaming/Channels/102",
        )
        self.assertIn("exit%20pass@192.168.254.116", settings.cameras[1].rtsp_url)


if __name__ == "__main__":
    unittest.main()
