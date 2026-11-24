"""Issue #43, part 1: skill packages live in the cloud (``/skills``), per organisation.

Covered: publishing (validated by the spec, the same digest the robot computes at install), the same
release sent again, a changed release under the same version, listing, downloading, deleting, who may
do what, tenancy, the per-organisation bound and hostile packages.
"""
import json
import tempfile

import support  # noqa: F401
from support import API
from skill_support import SKILLS_READ, SKILLS_WRITE, SkillCase, example_package, package

from neuxmind_cloud import skill_store


class StoreCase(SkillCase):
    def publish_raw(self, pkg: dict, user=None, **extra):
        return self.post("/skills/releases", {"package": pkg, **extra}, user or self.owner)

    def publish(self, pkg: dict, user=None, **extra) -> dict:
        return self.ok(self.publish_raw(pkg, user, **extra), 201)


class Publishing(StoreCase):
    def test_a_published_package_is_listed_described_and_downloaded_unchanged(self):
        pkg = example_package("goto_place")
        release = self.publish(pkg, notes="第一次发布")
        self.assertEqual((release["skill_id"], release["version"], release["title"]), ("goto_place", "0.1.0", "去指定地点"))
        self.assertEqual(release["requires"], ["base.navigate_to_place", "safety.stop"])
        self.assertEqual(release["optional"], ["sensor.battery"])
        self.assertTrue(release["motion"])
        self.assertEqual(release["timeout_s"], 1800)
        self.assertEqual([p["name"] for p in release["parameters"]], ["place", "max_speed"])
        self.assertEqual(release["files"], [{"path": "skill.py", "size": len(pkg["files"]["skill.py"].encode())}])
        self.assertEqual(release["size"], len(pkg["files"]["skill.py"].encode()))
        self.assertEqual((release["notes"], release["created_by"]), ("第一次发布", f"user:{self.owner['user']['id']}"))
        self.assertRegex(release["digest"], r"^[0-9a-f]{64}$")

        listed = self.ok(self.get("/skills", self.owner))["skills"]
        self.assertEqual([(s["id"], s["title"], s["versions"]) for s in listed], [("goto_place", "去指定地点", ["0.1.0"])])
        self.assertEqual(listed[0]["latest"]["digest"], release["digest"])
        self.assertEqual(self.ok(self.get("/skills/goto_place", self.owner))["releases"][0]["digest"], release["digest"])
        self.assertEqual(self.ok(self.get("/skills/goto_place/releases/0.1.0", self.owner))["digest"], release["digest"])

        got = self.ok(self.get("/skills/goto_place/releases/0.1.0/package", self.owner))
        self.assertEqual(got["release"]["digest"], release["digest"])
        self.assertEqual(got["package"]["files"], pkg["files"])
        self.assertEqual((got["package"]["id"], got["package"]["entry"]), ("goto_place", "skill.py:run"))
        self.assertEqual(got["package"]["parameters"], pkg["parameters"])

    def test_the_digest_is_the_one_the_robot_computes_when_it_installs_the_package(self):
        from neuxmind_runtime.skills import SkillStore
        from neuxmind_runtime.store import Store
        pkg = example_package("goto_place")
        release = self.publish(pkg)
        manifest = {"capabilities": [{"id": "base.navigate_to_place"}, {"id": "safety.stop"}]}
        with tempfile.TemporaryDirectory() as state:
            installed = SkillStore(Store(state), f"{state}/skills", manifest).install(json.loads(json.dumps(pkg)))
            self.assertEqual(release["digest"], installed["digest"])
            # ... and what the cloud hands out installs to the same digest
            downloaded = self.ok(self.get("/skills/goto_place/releases/0.1.0/package", self.owner))["package"]
            again = SkillStore(Store(f"{state}/b"), f"{state}/b/skills", manifest).install(downloaded)
            self.assertEqual(again["digest"], release["digest"])

    def test_motion_is_false_for_a_skill_that_only_reads(self):
        self.assertFalse(self.publish(example_package("status_report"))["motion"])
        self.assertTrue(self.publish(package("x.vendor", requires=[{"id": "x.vendor.dance"}]))["motion"])

    def test_the_same_release_again_returns_the_first_one(self):
        first = self.publish(package())
        again = self.ok(self.publish_raw(package(), notes="再发一次"), 200)
        self.assertTrue(again["existing"])
        self.assertEqual((again["digest"], again["created_at"], again["notes"]),
                         (first["digest"], first["created_at"], first["notes"]))
        self.assertEqual(len(self.audits("skill.release.create")), 1)

    def test_other_content_under_the_same_version_is_refused(self):
        self.publish(package())
        changed = package(files={"skill.py": "def run(ctx, times=1):\n    return None\n"})
        self.error(self.publish_raw(changed), 409, "skill_version_exists")
        self.publish(package(version="0.2.0", files=changed["files"]))
        skill = self.ok(self.get("/skills", self.owner))["skills"][0]
        self.assertEqual((skill["versions"], skill["latest"]["version"]), (["0.2.0", "0.1.0"], "0.2.0"))
        self.assertEqual([r["version"] for r in self.ok(self.get("/skills/demo.wave", self.owner))["releases"]],
                         ["0.2.0", "0.1.0"])

    def test_versions_are_ordered_as_versions_not_as_text(self):
        for version in ("0.9.0", "0.10.0", "0.2.0"):
            self.publish(package(version=version))
        self.assertEqual(self.ok(self.get("/skills", self.owner))["skills"][0]["versions"], ["0.10.0", "0.9.0", "0.2.0"])

    def test_skills_are_listed_by_id(self):
        for skill_id in ("zeta", "alpha", "mid.one"):
            self.publish(package(skill_id))
        self.assertEqual([s["id"] for s in self.ok(self.get("/skills", self.owner))["skills"]], ["alpha", "mid.one", "zeta"])

    def test_deleting_a_release(self):
        self.publish(package())
        self.publish(package(version="0.2.0"))
        self.assertEqual(self.ok(self.delete("/skills/demo.wave/releases/0.1.0", self.owner)), {"deleted": True})
        self.error(self.get("/skills/demo.wave/releases/0.1.0", self.owner), 404, "skill_not_found")
        self.error(self.delete("/skills/demo.wave/releases/0.1.0", self.owner), 404, "skill_not_found")
        self.ok(self.delete("/skills/demo.wave/releases/0.2.0", self.owner))
        self.error(self.get("/skills/demo.wave", self.owner), 404, "skill_not_found")
        self.assertEqual(self.ok(self.get("/skills", self.owner))["skills"], [])
        self.assertEqual([target for _actor, target, _detail in self.audits("skill.release.delete")],
                         ["demo.wave@0.1.0", "demo.wave@0.2.0"])
        self.publish(package())                       # a deleted version may be published again

    def test_what_does_not_exist_is_404(self):
        for path in ("/skills/nope", "/skills/nope/releases/0.1.0", "/skills/nope/releases/0.1.0/package",
                     "/skills/Bad%20Id", "/skills/demo.wave/releases/not-a-version"):
            with self.subTest(path=path):
                self.error(self.get(path, self.owner), 404, "skill_not_found")


class Permissions(StoreCase):
    def member(self, email: str, role: str) -> dict:
        other = self.register(email)
        self.ok(self.post("/org/members", {"email": email, "role": role}, self.owner))
        other["headers"] = {**other["headers"], "X-NeuxMind-Org": self.owner["org_id"]}
        return other

    def test_another_organisation_sees_nothing_and_changes_nothing(self):
        self.publish(package())
        other = self.register("other@example.com")
        self.assertEqual(self.ok(self.get("/skills", other))["skills"], [])
        for path in ("/skills/demo.wave", "/skills/demo.wave/releases/0.1.0", "/skills/demo.wave/releases/0.1.0/package"):
            self.error(self.get(path, other), 404, "skill_not_found")
        self.error(self.delete("/skills/demo.wave/releases/0.1.0", other), 404, "skill_not_found")
        # the same id and version in another organisation is that organisation's own release
        theirs = self.publish(package(files={"skill.py": "def run(ctx, times=1):\n    return 'theirs'\n"}), other)
        mine = self.ok(self.get("/skills/demo.wave/releases/0.1.0", self.owner))
        self.assertNotEqual(theirs["digest"], mine["digest"])

    def test_a_field_operator_reads_but_does_not_publish_or_delete(self):
        self.publish(package())
        operator = self.member("op@example.com", "operator")
        self.assertEqual(len(self.ok(self.get("/skills", operator))["skills"]), 1)
        self.ok(self.get("/skills/demo.wave/releases/0.1.0/package", operator))
        self.error(self.publish_raw(package(version="0.2.0"), operator), 403, "forbidden")
        self.error(self.delete("/skills/demo.wave/releases/0.1.0", operator), 403, "forbidden")
        developer = self.member("dev@example.com", "developer")
        self.publish(package(version="0.2.0"), developer)

    def test_api_keys_need_the_scope(self):
        reader, writer = self.api_key(self.owner, [SKILLS_READ]), self.api_key(self.owner, [SKILLS_WRITE])
        self.publish(package(), writer)
        self.assertEqual(len(self.ok(self.get("/skills", reader))["skills"]), 1)
        self.error(self.get("/skills", writer), 403, "scope_missing")
        self.error(self.publish_raw(package(version="0.2.0"), reader), 403, "scope_missing")
        self.error(self.delete("/skills/demo.wave/releases/0.1.0", reader), 403, "scope_missing")
        self.error(self.get("/skills", self.robot_key), 403, "scope_missing")
        self.error(self.get("/skills", self.caller), 403, "scope_missing")
        self.error(self.client.get(f"{API}/skills"), 401)

    def test_an_unverified_account_does_not_publish(self):
        fresh = self.register("fresh@example.com", verify=False)
        self.error(self.publish_raw(package(), fresh), 403, "email_unverified")


class Limits(StoreCase):
    settings_overrides = {"skill_org_max_releases": 3}

    def test_the_organisation_bound(self):
        for index in range(3):
            self.publish(package(version=f"0.{index}.0"))
        self.error(self.publish_raw(package(version="0.9.0")), 409, "skill_quota_exceeded")
        self.ok(self.publish_raw(package(version="0.0.0")), 200)          # one that is already there is not a new one
        self.publish(package(), self.register("other@example.com"))       # another organisation has its own room
        self.ok(self.delete("/skills/demo.wave/releases/0.0.0", self.owner))
        self.publish(package(version="0.9.0"))

    def test_a_package_larger_than_the_general_body_limit_is_accepted_up_to_its_own(self):
        self.assertEqual(self.settings.max_body_bytes, 200_000)
        big = package(files={"skill.py": "def run(ctx, times=1):\n    return 1\n", "data.txt": "x" * 400_000})
        self.assertEqual(self.publish(big)["size"], 400_000 + len(big["files"]["skill.py"]))
        too_big = json.dumps({"package": package(files={"skill.py": "x" * (3 * 1024 * 1024)})})
        response = self.client.post(f"{API}/skills/releases", content=too_big,
                                    headers={**self.owner["headers"], "Content-Type": "application/json"})
        self.error(response, 413, "payload_too_large")
        # the larger bound is not spent on the unauthenticated, and no other route has it
        response = self.client.post(f"{API}/skills/releases", content=too_big, headers={"Content-Type": "application/json"})
        self.error(response, 401)
        response = self.client.post(f"{API}/api-keys", content=json.dumps({"name": "x" * 300_000}),
                                    headers={**self.owner["headers"], "Content-Type": "application/json"})
        self.error(response, 413, "payload_too_large")

    def test_the_package_limits_are_those_of_the_robot(self):
        self.assertEqual((skill_store.MAX_FILES, skill_store.MAX_FILE_BYTES, skill_store.MAX_TOTAL_BYTES),
                         (200, 512 * 1024, 1536 * 1024))
        from neuxmind_runtime import skills as on_robot
        self.assertEqual((skill_store.MAX_FILES, skill_store.MAX_FILE_BYTES, skill_store.MAX_TOTAL_BYTES),
                         (on_robot.MAX_FILES, on_robot.MAX_FILE_BYTES, on_robot.MAX_TOTAL_BYTES))


class HostilePackages(StoreCase):
    def refused(self, pkg, **extra) -> dict:
        return self.error(self.post_text("/skills/releases", {"package": pkg, **extra}, self.owner["headers"]), 400,
                          "invalid_skill_package")

    def test_packages_that_are_not_packages(self):
        code = "def run(ctx):\n    return 1\n"
        bad = {
            "not an object": "skill",
            "wrong schema": package(schema="skill.package.v9"),
            "bad id": package("Bad Id"),
            "bad version": package(version="one"),
            "unknown capability": package(requires=[{"id": "base.teleport"}]),
            "no capability": package(requires=[]),
            "capability of the wrong type": package(requires=[7]),
            "no files": package(files={}),
            "files not an object": package(files=["skill.py"]),
            "entry not in files": package(entry="other.py:run"),
            "bad entry": package(entry="skill.py"),
            "path escapes": package(files={"skill.py": code, "../evil.py": code}),
            "absolute path": package(files={"skill.py": code, "/etc/passwd": code}),
            "backslash path": package(files={"skill.py": code, "a\\b.py": code}),
            "file and directory": package(files={"skill.py": code, "skill.py/x.py": code}),
            "content not text": package(files={"skill.py": code, "data.bin": 7}),
            "NUL in a file": package(files={"skill.py": code + "\x00"}),
            "NUL in the title": package(title="a\x00b"),
            "half a surrogate pair": package(files={"skill.py": code, "note.txt": "\ud800"}),
            "one file too large": package(files={"skill.py": code, "big.txt": "x" * (512 * 1024 + 1)}),
            "too many files": package(files={"skill.py": code, **{f"f{i}.txt": "x" for i in range(200)}}),
            "timeout out of range": package(timeout_s=10 ** 9),
            "parameter without a type": package(parameters=[{"name": "n"}]),
            "parameter range upside down": package(parameters=[{"name": "n", "type": "int", "min": 5, "max": 1}]),
            "parameter declared twice": package(parameters=[{"name": "n", "type": "int"}, {"name": "n", "type": "int"}]),
            "default outside the range": package(parameters=[{"name": "n", "type": "int", "min": 1, "default": 0}]),
            "nested too deep": package(constraints=json.loads("{\"a\":" * 100 + "1" + "}" * 100)),
            "a version the store could never address": package(version="10000000000.0.0"),
            "a version with an endless suffix": package(version="1.0.0-" + "a" * 41),
            "a number no float can hold": package(parameters=[{"name": "n", "type": "int", "max": 10 ** 400}]),
            "a timeout no float can hold": package(timeout_s=10 ** 400),
        }
        for name, pkg in bad.items():
            with self.subTest(name):
                self.refused(pkg)
        self.assertEqual(self.ok(self.get("/skills", self.owner))["skills"], [])

    def test_total_size_is_bounded(self):
        files = {"skill.py": "def run(ctx):\n    return 1\n", **{f"f{i}.txt": "x" * (500 * 1024) for i in range(4)}}
        self.refused(package(files=files))

    def test_not_a_number_is_refused(self):
        self.refused(package(constraints={"base.velocity": {"max_vx": {"min": float("nan")}}}))
        self.refused(package(parameters=[{"name": "n", "type": "float", "max": float("inf")}]))

    def test_notes_are_bounded_text(self):
        for notes in (7, "x" * 2001, "a\x00b"):
            with self.subTest(notes=str(notes)[:10]):
                self.error(self.post("/skills/releases", {"package": package(), "notes": notes}, self.owner), 400,
                           "invalid_request")
        self.error(self.post("/skills/releases", ["package"], self.owner), 400)
        self.error(self.post("/skills/releases", {}, self.owner), 400, "invalid_skill_package")
