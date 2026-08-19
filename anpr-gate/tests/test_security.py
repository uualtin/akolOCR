import json
import unittest

from app.security import SessionManager, hash_password, validate_password, verify_password


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


class SecurityTests(unittest.TestCase):
    def test_argon2_password_policy_and_verification(self):
        with self.assertRaises(ValueError):
            validate_password("too-short")
        password_hash = hash_password("correct horse battery staple")
        self.assertTrue(password_hash.startswith("$argon2id$"))
        self.assertTrue(verify_password(password_hash, "correct horse battery staple"))
        self.assertFalse(verify_password(password_hash, "incorrect password"))

    def test_login_rate_limit(self):
        manager = SessionManager(FakeRedis(), "session", 60, True)
        for _ in range(5):
            manager.login_failed("admin", "203.0.113.4")
        self.assertTrue(manager.login_rate_limited("admin", "203.0.113.4"))
        self.assertFalse(manager.login_rate_limited("admin", "203.0.113.5"))
        manager.login_succeeded("admin", "203.0.113.4")
        self.assertFalse(manager.login_rate_limited("admin", "203.0.113.4"))


if __name__ == "__main__":
    unittest.main()
