"""Issue #48: a skill release in the cloud is for one brand and model of robot.

Covered: publishing with a brand and model, the view, half of it refused, the same release sent
again for another brand and model, saying it once for a release that names none, and a database
from before getting the two columns.
"""
import tempfile

import support  # noqa: F401
from support import API
from skill_support import SkillCase, package
from sqlalchemy import create_engine, inspect, text

from neuxmind_cloud.db import Database

RELEASE = "/skills/demo.wave/releases/0.1.0"
X1 = {"vendor": "Acme", "model": "X1"}


class Product(SkillCase):
    def publish_raw(self, pkg=None, **extra):
        return self.post("/skills/releases", {"package": pkg or package(), **extra}, self.owner)

    def set_product(self, user=None, **body):
        return self.client.put(API + RELEASE + "/product", json=body, headers=(user or self.owner)["headers"])

    def test_a_release_carries_the_brand_and_model_it_was_published_for(self):
        release = self.ok(self.publish_raw(vendor="  Acme ", model="X1"), 201)
        self.assertEqual(release["product"], X1)
        self.assertEqual(self.ok(self.get(RELEASE, self.owner))["product"], X1)
        self.assertEqual(self.ok(self.get("/skills", self.owner))["skills"][0]["latest"]["product"], X1)
        self.assertEqual(self.ok(self.get(RELEASE + "/package", self.owner))["release"]["product"], X1)
        self.assertEqual(self.audits("skill.release.create")[0][2]["product"], X1)

    def test_without_one_the_release_names_none(self):
        self.assertIsNone(self.ok(self.publish_raw(), 201)["product"])

    def test_half_of_it_and_what_is_not_text_are_refused(self):
        for extra in ({"vendor": "Acme"}, {"model": "X1"}, {"vendor": 3, "model": "X1"},
                      {"vendor": "A" * 101, "model": "X1"}, {"vendor": "Ac\x00me", "model": "X1"}):
            self.error(self.publish_raw(**extra), 400, "invalid_product")
        self.assertEqual(self.ok(self.get("/skills", self.owner))["skills"], [])

    def test_the_same_release_again_is_the_first_one_but_not_for_another_brand_and_model(self):
        self.ok(self.publish_raw(**X1), 201)
        again = self.ok(self.publish_raw(vendor="ACME", model="x1"), 200)
        self.assertEqual((again["existing"], again["product"]), (True, X1))
        self.assertEqual(self.ok(self.publish_raw(), 200)["product"], X1)
        refused = self.error(self.publish_raw(vendor="Acme", model="X2"), 409, "skill_other_model")
        self.assertEqual(refused["product"], X1)

    def test_a_release_that_names_none_is_told_once(self):
        self.ok(self.publish_raw(), 201)
        self.error(self.set_product(vendor="Acme"), 400, "invalid_product")
        self.assertEqual(self.ok(self.set_product(**X1))["product"], X1)
        self.assertEqual(self.ok(self.set_product(vendor="acme", model="x1"))["product"], X1)
        self.error(self.set_product(vendor="Acme", model="X2"), 409, "skill_product_set")
        self.assertEqual(len(self.audits("skill.release.product")), 1)
        other = self.register("other@example.com")
        self.error(self.set_product(other, **X1), 404, "skill_not_found")

    def test_publishing_again_with_one_tells_a_release_that_names_none(self):
        self.ok(self.publish_raw(), 201)
        self.assertEqual(self.ok(self.publish_raw(**X1), 200)["product"], X1)
        self.assertEqual(self.ok(self.get(RELEASE, self.owner))["product"], X1)


class OlderDatabase(SkillCase):
    def test_a_table_from_before_gets_the_two_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{tmp}/old.sqlite3"
            engine = create_engine(url)
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE skill_releases (id VARCHAR(40) PRIMARY KEY, org_id VARCHAR(40), skill_id VARCHAR(100))"))
                conn.execute(text("INSERT INTO skill_releases VALUES ('skr-1', 'org-1', 'demo.wave')"))
            engine.dispose()
            db = Database(url)
            db.create_all()
            with db.engine.begin() as conn:
                columns = {c["name"] for c in inspect(conn).get_columns("skill_releases")}
                row = conn.execute(text("SELECT vendor, model FROM skill_releases")).one()
            db.engine.dispose()
        self.assertLessEqual({"vendor", "model"}, columns)
        self.assertEqual(tuple(row), ("", ""))
