import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import web
from app.security import SessionManager, hash_password


class FakeRedis:
    def __init__(self):
        self.values = {}

    def setex(self, key, _, value): self.values[key] = value
    def get(self, key): return self.values.get(key)
    def expire(self, *_): return True
    def delete(self, key): self.values.pop(key, None)
    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]
    def publish(self, *_): return 1
    def ping(self): return True


class FakeDatabase:
    def __init__(self):
        self.user = {
            "id": 1,
            "username": "admin",
            "password_hash": hash_password("correct horse battery staple"),
            "is_active": True,
            "locked_until": None,
        }
        self.events = []
        self.audits = []

    def initialize(self): pass
    def get_user_by_username(self, username):
        return self.user if username == "admin" else None
    def login_succeeded(self, *_): pass
    def login_failed(self, *_): pass
    def create_audit(self, *args, **kwargs): self.audits.append((args, kwargs))
    def get_gate(self, gate_id): return {"id": gate_id, "name": gate_id}
    def upsert_event(self, event):
        self.events = [item for item in self.events if item["id"] != event["id"]]
        self.events.append(dict(event))


class WebSecurityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.database = FakeDatabase()
        self.sessions = SessionManager(self.redis, "anpr_session", 3600, True)
        self.patches = [
            patch.object(web, "redis_client", self.redis),
            patch.object(web, "database", self.database),
            patch.object(web, "sessions", self.sessions),
        ]
        for active in self.patches:
            active.start()
        self.client = TestClient(web.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        for active in reversed(self.patches):
            active.stop()

    def login(self):
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery staple"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        set_cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("secure", set_cookie)
        self.assertIn("samesite=strict", set_cookie)
        token = response.cookies["anpr_session"]
        return json.loads(self.redis.get(f"session:{token}"))["csrf"]

    def test_login_session_csrf_and_security_headers(self):
        anonymous = self.client.get("/stream/entry.mjpeg")
        self.assertEqual(anonymous.status_code, 401)
        anonymous_snapshot = self.client.get(
            "/snapshot/0440dd8e-e960-40c8-83f4-d7f95b480c34/full"
        )
        self.assertEqual(anonymous_snapshot.status_code, 401)
        self.assertEqual(anonymous.headers["x-frame-options"], "DENY")
        csrf = self.login()
        rejected = self.client.post(
            "/gates/entry/open",
            data={"reason": "Test", "confirm": "OPEN", "csrf": "wrong"},
        )
        self.assertEqual(rejected.status_code, 403)
        opened = self.client.post(
            "/gates/entry/open",
            data={"reason": "Yetkili test açılışı", "confirm": "OPEN", "csrf": csrf},
            follow_redirects=False,
        )
        self.assertEqual(opened.status_code, 303)
        self.assertEqual(self.database.events[-1]["source"], "manual")
        self.assertEqual(self.database.events[-1]["trigger_status"], "disabled")
        self.assertEqual(self.database.events[-1]["manual_reason"], "Yetkili test açılışı")
        self.assertTrue(any(args[1] == "gate.manual_open" for args, _ in self.database.audits))

    def test_login_rate_limit(self):
        for _ in range(5):
            response = self.client.post(
                "/login", data={"username": "nobody", "password": "bad-password"}
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/login", data={"username": "nobody", "password": "bad-password"}
        )
        self.assertEqual(blocked.status_code, 429)


if __name__ == "__main__":
    unittest.main()
