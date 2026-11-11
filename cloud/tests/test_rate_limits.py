"""Review C-03: login throttling by (email, ip), ip and email; registration per ip; trusted proxies."""
import time
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401
from support import API, PASSWORD, CloudTestCase

from neuxmind_cloud import auth
from neuxmind_cloud.db import advisory_key, advisory_lock

WRONG = "wrong-password!"


class LoginThrottleTest(CloudTestCase):
    # TestClient's peer is "testclient"; trusting it lets X-Forwarded-For pick the client address.
    settings_overrides = {"trusted_proxies": ["testclient"], "login_max_failures": 3, "login_ip_max_failures": 6,
                          "login_email_soft_limit": 5, "login_max_delay_seconds": 30}

    def login(self, email, password, ip):
        return self.client.post(f"{API}/auth/login", json={"email": email, "password": password},
                                headers={"X-Forwarded-For": f"203.0.113.250, {ip}"})

    def test_attacker_cannot_lock_victim_out_from_another_address(self):
        self.register("victim@example.com")
        for _ in range(3):
            self.error(self.login("victim@example.com", WRONG, "198.51.100.1"), 401)
        self.error(self.login("victim@example.com", PASSWORD, "198.51.100.1"), 429, "rate_limited")   # attacker pair
        self.ok(self.login("victim@example.com", PASSWORD, "192.0.2.10"))                           # victim elsewhere
        # The victim's success must not reset the attacker's counter.
        self.error(self.login("victim@example.com", WRONG, "198.51.100.1"), 429, "rate_limited")

    def test_credential_stuffing_limited_per_ip(self):
        for i in range(6):
            self.error(self.login(f"user{i}@example.com", WRONG, "198.51.100.2"), 401)
        self.error(self.login("someone-else@example.com", WRONG, "198.51.100.2"), 429, "rate_limited")
        self.error(self.login("someone-else@example.com", WRONG, "198.51.100.3"), 401)   # other ips unaffected

    def test_distributed_guessing_gets_progressive_delay_not_lockout(self):
        self.register("target@example.com")
        clock = [time.time()]
        with mock.patch.object(auth, "now", lambda: clock[0]):
            for i in range(5):
                self.error(self.login("target@example.com", WRONG, f"198.51.100.{10 + i}"), 401)
                clock[0] += 0.1
            body = self.error(self.login("target@example.com", PASSWORD, "192.0.2.10"), 429, "rate_limited")
            self.assertLessEqual(body["retry_after"], 2)
            clock[0] += 1.5
            self.ok(self.login("target@example.com", PASSWORD, "192.0.2.10"))
            # A sustained distributed attack: attempts inside the delay are refused
            # unrecorded, so guessing slows to a trickle ...
            recorded = 0
            for i in range(200):
                clock[0] += 0.5
                status = self.login("target@example.com", WRONG, f"198.51.100.{i % 250}").status_code
                self.assertIn(status, (401, 429))
                recorded += status == 401
            self.assertLess(recorded, 30)          # 100 s of attack, 200 tries → only a handful checked
            # ... and the owner is never locked out for longer than the cap.
            response = self.login("target@example.com", PASSWORD, "192.0.2.10")
            if response.status_code == 429:
                self.assertLessEqual(response.json()["retry_after"], 31)
                clock[0] += response.json()["retry_after"]
                response = self.login("target@example.com", PASSWORD, "192.0.2.10")
            self.ok(response)


class UntrustedForwardedForTest(CloudTestCase):
    def test_client_supplied_forwarded_for_is_ignored(self):
        self.register("xff@example.com")
        for i in range(3):
            self.error(self.client.post(f"{API}/auth/login", json={"email": "xff@example.com", "password": WRONG},
                                        headers={"X-Forwarded-For": f"10.0.0.{i}"}), 401)
        self.error(self.client.post(f"{API}/auth/login", json={"email": "xff@example.com", "password": PASSWORD},
                                    headers={"X-Forwarded-For": "10.9.9.9"}), 429, "rate_limited")


class RegistrationThrottleTest(CloudTestCase):
    settings_overrides = {"register_ip_max_per_hour": 2}

    def test_registrations_per_ip(self):
        self.register("r1@example.com")
        self.register("r2@example.com")
        self.error(self.post("/auth/register", {"email": "r3@example.com", "password": PASSWORD}), 429, "rate_limited")


class AdvisoryLockTest(CloudTestCase):
    def test_postgres_takes_sorted_xact_locks_sqlite_noop(self):
        class FakeSession:
            def __init__(self, dialect):
                self.calls = []
                self.dialect = dialect

            def get_bind(self):
                return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

            def execute(self, statement, params=None):
                self.calls.append((str(statement), params))

        pg = FakeSession("postgresql")
        advisory_lock(pg, "login-ip:1.2.3.4", "login-email:a@b.c", "login-ip:1.2.3.4")
        self.assertEqual([c[0] for c in pg.calls], ["SELECT pg_advisory_xact_lock(:k)"] * 2)
        self.assertEqual([c[1]["k"] for c in pg.calls],
                         [advisory_key("login-email:a@b.c"), advisory_key("login-ip:1.2.3.4")])
        self.assertTrue(all(-2 ** 63 <= c[1]["k"] < 2 ** 63 for c in pg.calls))
        lite = FakeSession("sqlite")
        advisory_lock(lite, "x")
        self.assertEqual(lite.calls, [])
