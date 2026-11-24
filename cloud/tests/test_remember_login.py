"""Issue #36: a sign-in that asks to be remembered lasts as long as the desktop app's own "keep me signed in".

The desktop app signs into the cloud with the same e-mail and password when the user signs into the app.
Its own session lasts 30 days with "保持登录"; a 12-hour cloud session would lapse the same day and the
materials pages would ask for a second sign-in again.
"""
import time

from support import PASSWORD, CloudTestCase

SHORT = 12 * 3600
LONG = 30 * 24 * 3600


class RememberedSessionTest(CloudTestCase):
    def lifetime(self, issued: dict) -> float:
        return issued["expires_at"] - time.time()

    def test_login_without_remember_keeps_the_short_lifetime(self):
        self.register("ann@example.com")

        login = self.ok(self.post("/auth/login", {"email": "ann@example.com", "password": PASSWORD}))

        self.assertAlmostEqual(self.lifetime(login), SHORT, delta=60)

    def test_login_with_remember_lasts_as_long_as_the_app_session(self):
        self.register("bob@example.com")

        login = self.ok(self.post("/auth/login", {"email": "bob@example.com", "password": PASSWORD, "remember": True}))

        self.assertAlmostEqual(self.lifetime(login), LONG, delta=60)
        self.ok(self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {login['token']}"}))

    def test_only_a_real_true_counts_as_remember(self):
        self.register("cat@example.com")

        for value in ("true", 1, "yes", None, False):
            login = self.ok(self.post("/auth/login", {"email": "cat@example.com", "password": PASSWORD,
                                                      "remember": value}))
            self.assertAlmostEqual(self.lifetime(login), SHORT, delta=60, msg=repr(value))

    def test_registration_with_remember_gets_the_long_session_too(self):
        response = self.client.post("/api/v1/auth/register", json={"email": "dan@example.com", "password": PASSWORD,
                                                                   "remember": True})

        self.assertEqual(response.status_code, 201, response.text)
        self.assertAlmostEqual(self.lifetime(response.json()), LONG, delta=60)


class RememberedLifetimeSettingTest(CloudTestCase):
    settings_overrides = {"session_remember_ttl_seconds": 7 * 24 * 3600}

    def test_the_operator_can_set_the_remembered_lifetime(self):
        self.register("eve@example.com")

        login = self.ok(self.post("/auth/login", {"email": "eve@example.com", "password": PASSWORD, "remember": True}))

        self.assertAlmostEqual(login["expires_at"] - time.time(), 7 * 24 * 3600, delta=60)
