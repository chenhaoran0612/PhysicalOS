"""Issue #34 (R1-13): a finished upload is parsed by itself; the result is shown, edited and confirmed.

URDF and packages are read by rules; documents go to a stand-in for the
knowledge service (fake_weknora) whose "model" answers what each test says.
Every file is made up by the test."""
from __future__ import annotations

import json
import time

import fake_weknora
import library_support as lib
import parse_support as files
from library_support import LibraryCase
from support import API

from neuxmind_cloud import material_parse
from neuxmind_cloud.material_parse_models import MaterialParse

DOC_ANSWERS = {
    "driver_name": {"driver_name": "demo_motion_driver", "driver_version": "1.4.0",
                    "source": "https://example.test/demo_motion_driver-1.4.0.tar.gz",
                    "os": ["Ubuntu 22.04"], "ros2_distro": ["humble"]},
    "install_steps": {"install_steps": ["sudo apt install ros-humble-demo-motion", "source /opt/ros/humble/setup.bash"]},
    "topics": {"topics": [{"name": "/cmd_vel", "type": "geometry_msgs/msg/Twist"}, {"name": "/odom", "type": "nav_msgs/msg/Odometry"}],
               "actions": None},
}


def answer_by_question(answers: dict):
    def answer(question: str, _knowledge_ids):
        for key, value in answers.items():
            if f'"{key}"' in question:
                return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"
        return "{}"
    return answer


class ParseCase(LibraryCase):
    weknora = True

    def setUp(self):
        self.fake = fake_weknora.FakeWeKnora() if self.weknora else None
        if self.fake:
            self.addCleanup(self.fake.close)
            self.fake.answer = answer_by_question(DOC_ANSWERS)
        super().setUp()
        self.owner = self.register("owner@parse.test")
        self.clock = time.time()

    def make_settings(self, **overrides):
        if self.fake:
            overrides = {"weknora_url": self.fake.url, "weknora_api_key": fake_weknora.API_KEY,
                         "weknora_kb_id": self.fake.kb_id, **overrides}
        return super().make_settings(**overrides)

    def parse_all(self, rounds: int = 40) -> None:
        """Run the worker's steps until nothing is due, moving the clock on between rounds."""
        for _ in range(rounds):
            self.clock += 120
            material_parse.run_pending(self.db, self.settings, at=self.clock)

    def result(self, material_id: str, user=None) -> dict:
        return self.ok(self.get(f"/library/materials/{material_id}/parse", user or self.owner))

    def put(self, material_id: str, body: dict, user=None):
        return self.client.put(f"{API}/library/materials/{material_id}/parse", json=body,
                               headers=(user or self.owner)["headers"])

    def confirm(self, material_id: str, body: dict, user=None):
        return self.post(f"/library/materials/{material_id}/parse/confirm", body, user or self.owner)

    def upload_parsed(self, name: str, data: bytes, **labels) -> dict:
        material = self.upload(self.owner, name, data, **labels)
        self.assertEqual(material["status"], "parsing")
        self.parse_all()
        return self.result(material["id"])


class UrdfParseTest(ParseCase):
    weknora = False

    def test_every_joint_limit_is_read_and_waits_for_confirmation(self):
        result = self.upload_parsed("arm.urdf", files.URDF_OK)
        self.assertEqual((result["kind"], result["status"]), ("urdf", "parsed"))
        joints = {j["name"]: j for j in result["values"]["joints"]}
        self.assertEqual((joints["shoulder"]["lower"], joints["shoulder"]["upper"], joints["shoulder"]["velocity"],
                          joints["shoulder"]["effort"]), (-1.57, 1.57, 2.0, 40.0))
        self.assertEqual((joints["rail"]["upper"], joints["rail"]["effort"]), (0.4, 120.0))
        self.assertEqual((joints["spin"]["velocity"], joints["spin"]["lower"]), (10.0, None))
        self.assertEqual(len(result["values"]["links"]), 5)
        self.assertEqual(result["missing"], [])
        self.assertIn("不会自动生效", result["note"])
        confirmed = self.ok(self.confirm(result["material"]["id"], {"revision": result["revision"]}))
        self.assertEqual((confirmed["status"], confirmed["confirmed_by"]), ("usable", "owner@parse.test"))
        self.assertIsNotNone(confirmed["confirmed_at"])
        material = self.ok(self.get(f"/library/materials/{result['material']['id']}", self.owner))
        self.assertEqual(material["status"], "usable")

    def test_missing_limits_are_named_and_can_be_filled_in(self):
        result = self.upload_parsed("half.urdf", files.URDF_GAPS)
        self.assertEqual(result["status"], "failed")
        self.assertIn("关节 j2 的位置下限、位置上限、速度上限、力矩上限", result["status_detail"])
        self.assertIn("关节 j1 的位置下限、位置上限", result["missing"])
        self.assertTrue(any("xacro 表达式" in w for w in result["warnings"]))
        j1 = next(j for j in result["values"]["joints"] if j["name"] == "j1")
        self.assertEqual(j1["raw"], {"lower": "${-pi/2}", "upper": "${pi/2}"})
        refused = self.error(self.confirm(result["material"]["id"], {"revision": result["revision"]}), 409, "parse_incomplete")
        self.assertIn("关节 j2", "".join(refused["missing"]))
        joints = [{**j, "lower": -1.5708, "upper": 1.5708, "velocity": j["velocity"] or 1.0, "effort": j["effort"] or 2.0}
                  for j in result["values"]["joints"]]
        saved = self.ok(self.put(result["material"]["id"], {"revision": result["revision"], "values": {"joints": joints}}))
        self.assertEqual((saved["status"], saved["missing"], saved["edited_by"]), ("parsed", [], "owner@parse.test"))
        done = self.ok(self.confirm(result["material"]["id"], {"revision": saved["revision"]}))
        self.assertEqual(done["status"], "usable")

    def test_a_hostile_urdf_fails_alone(self):
        bad = self.upload(self.owner, "xxe.urdf", files.XXE)
        good = self.upload(self.owner, "arm.urdf", files.URDF_OK)
        self.parse_all()
        failed = self.result(bad["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("DOCTYPE", failed["status_detail"])
        self.assertNotIn("root:", json.dumps(failed, ensure_ascii=False))
        self.assertEqual(self.result(good["id"])["status"], "parsed")


class PackageParseTest(ParseCase):
    weknora = False

    def check(self, name: str, data: bytes, expected: tuple) -> dict:
        result = self.upload_parsed(name, data)
        values = result["values"]
        self.assertEqual(result["kind"], "package")
        self.assertEqual((values["package_name"], values["package_version"], values["architecture"]), expected)
        self.assertEqual(values["sha256"], lib.sha(data))
        self.assertEqual(result["status"], "parsed")
        self.assertIn("没有被解析", result["note"])
        return result

    def test_deb(self):
        result = self.check("demo.deb", files.deb_bytes(), ("demo-driver", "2.3.1-1", ["arm64"]))
        self.assertIn("https://example.test/driver", result["values"]["notes"])

    def test_zip_with_package_xml_and_programs(self):
        data = files.zip_bytes({"demo_driver/package.xml": files.package_xml(), "demo_driver/lib/libdemo.so": files.elf(0xB7)})
        result = self.check("demo.zip", data, ("demo_driver", "0.4.2", ["arm64"]))
        self.assertTrue(any("机器类型" in w for w in result["warnings"]))

    def test_tar_gz_with_debian_control(self):
        import gzip
        data = gzip.compress(files.tar_bytes({"demo/DEBIAN/control": files.CONTROL, "demo/usr/bin/demo": files.elf(0x3E)}),
                             mtime=0)
        self.check("demo.tar.gz", data, ("demo-driver", "2.3.1-1", ["arm64"]))

    def test_a_package_without_metadata_is_filled_in_by_a_person(self):
        data = files.zip_bytes({"blob/readme.txt": b"nothing to see here" * 10})
        result = self.upload_parsed("plain.zip", data)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing"], ["名称", "版本", "适用架构"])
        saved = self.ok(self.confirm(result["material"]["id"], {
            "revision": result["revision"], "values": {"package_name": "plain", "package_version": "1.0",
                                                       "architecture": ["all"], "sha256": "0" * 64}}))
        self.assertEqual(saved["status"], "usable")
        self.assertEqual(saved["values"]["sha256"], lib.sha(data))          # the checksum cannot be edited

    def test_names_leading_out_of_the_package_are_shown(self):
        data = files.zip_bytes({"../escape.sh": b"#!/bin/sh\n", "demo/package.xml": files.package_xml()})
        result = self.upload_parsed("evil.zip", data)
        self.assertTrue(any("指向包外" in w for w in result["warnings"]))

    def test_a_tar_gz_bomb_fails_alone(self):
        self.settings.library_parse_unpacked_bytes = 8 * 1024 * 1024
        result = self.upload_parsed("bomb.tar.gz", files.zero_gzip_members(64))
        self.assertEqual(result["status"], "failed")
        self.assertIn("解压炸弹", result["status_detail"])


class DocumentParseTest(ParseCase):
    def test_a_document_goes_to_the_knowledge_service_and_its_answers_fill_the_items(self):
        result = self.upload_parsed("manual.docx", lib.docx_bytes("驱动手册"), device_id="robot-0123456789ab",
                                    device_name="一号机")
        values = result["values"]
        self.assertEqual((values["driver_name"], values["driver_version"], values["ros2_distro"]),
                         ("demo_motion_driver", "1.4.0", ["humble"]))
        self.assertEqual(values["topics"][0], {"name": "/cmd_vel", "type": "geometry_msgs/msg/Twist"})
        self.assertEqual(values["actions"], [])
        self.assertEqual((result["status"], result["parser"]), ("parsed", "weknora"))
        sent = self.fake.uploads[0]
        self.assertTrue(sent["fileName"].endswith(".docx"))
        self.assertEqual(sent["metadata"]["neuxmind_material_id"], result["material"]["id"])
        self.assertEqual(sent["metadata"]["neuxmind_device_id"], "robot-0123456789ab")
        self.assertEqual(sent["sha256"], result["material"]["sha256"])
        self.assertEqual(len(self.fake.questions), 3)
        knowledge_id = next(iter(self.fake.knowledge))
        for question in self.fake.questions:
            self.assertEqual(question["knowledge_ids"], [knowledge_id])
            self.assertFalse(question["web_search_enabled"])
        self.assertEqual(self.fake.sessions_open, set())
        self.assertIn("大模型", result["note"])

    def test_questions_wait_until_the_service_has_finished_with_the_document(self):
        # A real WeKnora (0.6.3, 2026-11-23) says "finalizing" while it still makes the summary; the first
        # question, asked then, got "no document attached" and the driver's name and version stayed empty.
        self.fake.polls_finalizing = 2
        result = self.upload_parsed("manual.md", b"# demo_motion_driver 1.4.0\n\nhow to install it\n")
        self.assertEqual((result["status"], result["values"]["driver_name"]), ("parsed", "demo_motion_driver"))
        self.assertEqual(len(self.fake.questions), 3)
        self.assertEqual({tuple(q["knowledge_status"]) for q in self.fake.questions}, {("completed",)})

    def test_items_the_document_does_not_state_are_named(self):
        self.fake.answer = answer_by_question({**DOC_ANSWERS, "driver_name": {
            **DOC_ANSWERS["driver_name"], "ros2_distro": None, "source": "资料中未提及"}})
        result = self.upload_parsed("sheet.xlsx", lib.xlsx_bytes())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing"], ["从哪里取驱动", "适用的 ROS 2 发行版"])
        self.assertIn("缺：从哪里取驱动、适用的 ROS 2 发行版", result["status_detail"])
        done = self.ok(self.confirm(result["material"]["id"], {"revision": result["revision"],
                                                               "values": {"source": "厂家提供的 U 盘"},
                                                               "not_applicable": ["ros2_distro"]}))
        self.assertEqual((done["status"], done["not_applicable"]), ("usable", ["ros2_distro"]))

    def test_an_answer_that_is_not_json_leaves_its_items_empty(self):
        self.fake.answer = lambda question, ids: "我不知道。<script>alert(1)</script>"
        result = self.upload_parsed("notes.md", b"# notes\n\njust text\n")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len([w for w in result["warnings"] if "格式" in w]), 3)
        self.assertEqual(self.fake.uploads[0]["fileName"], "notes.md")

    def test_long_and_odd_answers_are_cut_and_cleaned(self):
        self.fake.answer = answer_by_question({**DOC_ANSWERS, "install_steps": {
            "install_steps": ["x" * 5000, "‮evil\u0000step", 42, {"nested": True}] + [f"s{i}" for i in range(300)]}})
        result = self.upload_parsed("long.txt", b"some manual text\n" * 20)
        steps = result["values"]["install_steps"]
        self.assertEqual(len(steps[0]), 1000)
        self.assertEqual(steps[1], "evil step")
        self.assertEqual(len(steps), 100)
        self.assertEqual(self.fake.uploads[0]["fileName"], "long.txt")

    def test_yaml_and_xml_are_sent_as_text(self):
        self.upload_parsed("params.yaml", b"speed: 1\n")
        self.assertEqual(self.fake.uploads[0]["fileName"], "params.txt")

    def test_the_service_failing_to_parse_is_shown_and_can_be_filled_in(self):
        self.fake.fail_parse = lambda upload: "PDF 无法打开"
        result = self.upload_parsed("broken.pdf", lib.pdf_bytes())
        self.assertEqual(result["status"], "failed")
        self.assertIn("PDF 无法打开", result["status_detail"])
        self.assertEqual(self.fake.questions, [])

    def test_a_file_above_the_service_limit_is_not_sent(self):
        self.settings.weknora_max_bytes = 1000
        result = self.upload_parsed("big.pdf", lib.pdf_bytes("x" * 2000))
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))
        self.assertIn("超过解析服务接收的上限", result["status_detail"])

    def test_external_references_keep_a_document_away_from_the_service(self):
        data = files.docx_with(files.rels(("image", "http://192.0.2.10/tracker.png")))
        result = self.upload_parsed("linked.docx", data)
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))
        self.assertIn("外部引用", result["status_detail"])

    def test_a_macro_is_noted_and_not_run(self):
        result = self.upload_parsed("macro.docx", files.docx_with(macro=True))
        self.assertEqual(result["status"], "parsed")
        self.assertTrue(any("宏" in w for w in result["warnings"]))

    def test_an_unreachable_service_is_retried_then_reported(self):
        material = self.upload(self.owner, "manual.pdf", lib.pdf_bytes())
        self.fake.close()
        self.parse_all(rounds=3)
        waiting = self.result(material["id"])
        self.assertEqual(waiting["status"], "parsing")
        self.assertTrue(waiting["retrying"])
        self.assertIn("连不上解析服务", waiting["error"])
        self.parse_all(rounds=40)
        result = self.result(material["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn(f"试了 {material_parse.MAX_ATTEMPTS} 次", result["status_detail"])

    def test_a_wrong_key_is_reported_at_once(self):
        self.settings.weknora_api_key = "not-the-key"
        result = self.upload_parsed("manual.pdf", lib.pdf_bytes())
        self.assertEqual(result["status"], "failed")
        self.assertIn("NEUXMIND_WEKNORA_API_KEY", result["status_detail"])

    def test_the_same_file_for_two_devices_is_asked_about_once_and_removed_with_the_last(self):
        data = lib.docx_bytes("同一本手册")
        first = self.upload_parsed("manual.docx", data, device_id="robot-aaaaaaaaaaaa")
        second = self.upload_parsed("manual.docx", data, device_id="robot-bbbbbbbbbbbb")
        self.assertNotEqual(first["material"]["id"], second["material"]["id"])
        self.assertEqual(second["values"]["driver_name"], "demo_motion_driver")
        self.assertEqual((len(self.fake.uploads), len(self.fake.questions)), (1, 3))
        self.ok(self.delete(f"/library/materials/{first['material']['id']}", self.owner))
        material_parse.cleanup(self.db, material_parse._service(self.settings))
        self.assertEqual(self.fake.deleted, [])
        self.ok(self.delete(f"/library/materials/{second['material']['id']}", self.owner))
        material_parse.cleanup(self.db, material_parse._service(self.settings))
        self.assertEqual(len(self.fake.deleted), 1)
        with self.db.transaction() as s:
            self.assertEqual(s.query(MaterialParse).count(), 0)


class HardeningTest(ParseCase):
    """What the security review of issue #34 found: every step ends in an outcome for its one material."""

    def test_huge_numbers_and_deep_nesting_in_answers_end_the_job(self):
        nested = "[" * 2000 + "]" * 2000
        self.fake.answer = lambda question, ids: (
            '{"driver_name": ' + "9" * 400 + ', "driver_version": ' + nested + ', "source": "x"}' if '"driver_name"' in question
            else '{"install_steps": [[[[[[["deep"]]]]]]], "topics": ' + nested + '}')
        result = self.upload_parsed("notes.md", b"# notes\n")
        self.assertEqual(result["stage"], "done")
        self.assertIsNone(result["values"]["driver_name"])
        self.assertEqual(result["values"]["source"], "x")
        self.assertEqual(result["status"], "failed")

    def test_a_huge_number_from_a_person_is_refused_quietly(self):
        result = self.upload_parsed("manual.docx", lib.docx_bytes())
        saved = self.put(result["material"]["id"], {"revision": result["revision"], "values": {"driver_version": int("9" * 400)}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIsNone(saved.json()["values"]["driver_version"])

    def test_a_missing_file_ends_the_job(self):
        material = self.upload(self.owner, "manual.docx", lib.docx_bytes("缺文件"))
        import os
        os.unlink(f"{self.library_dir}/objects/{self.owner['org_id']}/{material['sha256'][:2]}/{material['sha256']}")
        self.parse_all()
        result = self.result(material["id"])
        self.assertEqual((result["stage"], result["status"]), ("done", "failed"))
        self.assertIn("不在云端", result["status_detail"])

    def test_padding_keeps_a_document_with_external_references_away(self):
        padding = {f"pad/_rels/p{i}.xml.rels": b"<Relationships/>" for i in range(301)}
        data = files.docx_with(files.rels(("image", "http://192.0.2.10/x.png")), extra=padding)
        result = self.upload_parsed("padded.docx", data)
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))

    def test_an_old_word_file_with_an_include_field_is_not_sent(self):
        data = files.ole_with("WordDocument") + b"\x13 includepicture \"http://192.0.2.10/x.png\""
        result = self.upload_parsed("old.doc", data)
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))
        self.assertIn("INCLUDEPICTURE", result["status_detail"])

    def test_a_copy_that_keeps_going_away_ends_the_job(self):
        original = material_parse.weknora.WeKnora.ask

        def gone(self, knowledge_id, question):
            raise material_parse.weknora.WeKnoraError("gone", retry=False, status=404, gone=True)
        material_parse.weknora.WeKnora.ask = gone
        self.addCleanup(setattr, material_parse.weknora.WeKnora, "ask", original)
        result = self.upload_parsed("manual.docx", lib.docx_bytes("总是丢"))
        self.assertEqual((result["stage"], result["status"]), ("done", "failed"))
        self.assertIn("副本丢了", result["status_detail"])
        self.assertEqual(len(self.fake.uploads), material_parse.MAX_RESENDS + 1)

    def test_a_missing_question_interface_is_not_taken_for_a_lost_copy(self):
        self.settings.weknora_kb_id = self.fake.kb_id
        self.fake.status_override = None
        original = self.fake.server.RequestHandlerClass.do_POST

        def no_sessions(handler):
            if handler.path.startswith("/api/v1/sessions"):
                handler._body()
                return handler._send(404, {"success": False, "message": "no route"})
            return original(handler)
        self.fake.server.RequestHandlerClass.do_POST = no_sessions
        self.addCleanup(setattr, self.fake.server.RequestHandlerClass, "do_POST", original)
        result = self.upload_parsed("manual.docx", lib.docx_bytes("没有问答接口"))
        self.assertEqual((result["status"], len(self.fake.uploads)), ("failed", 1))
        self.assertIn("开启问答失败（HTTP 404）", result["status_detail"])

    def test_a_copy_removed_before_the_questions_is_sent_again(self):
        self.fake.drop_before_chat = True
        result = self.upload_parsed("manual.docx", lib.docx_bytes("再传一次"))
        self.assertEqual((result["status"], len(self.fake.uploads)), ("parsed", 2))

    def test_a_job_that_took_a_copy_being_removed_starts_over(self):
        first = self.upload_parsed("manual.docx", lib.docx_bytes("共用"))
        with self.db.transaction() as s:
            copy = s.get(MaterialParse, first["material"]["id"]).knowledge_id
            s.add(MaterialParse(material_id="mat_" + "0" * 20, org_id=self.owner["org_id"], sha256="0" * 64,
                                kind="document", stage="indexing", knowledge_id=copy))
        material_parse._forget_copy(self.db, copy, first["material"]["id"])
        with self.db.transaction() as s:
            other = s.get(MaterialParse, "mat_" + "0" * 20)
            self.assertEqual((other.stage, other.knowledge_id), ("queued", ""))
            self.assertEqual(s.get(MaterialParse, first["material"]["id"]).knowledge_id, copy)

    def test_a_revision_must_be_an_integer(self):
        result = self.upload_parsed("arm.urdf", files.URDF_OK)
        for revision in (True, float(result["revision"]), str(result["revision"])):
            self.error(self.put(result["material"]["id"], {"revision": revision, "values": {}}), 409, "parse_changed")

    def test_marking_not_applicable_alone_names_the_editor(self):
        self.fake.answer = answer_by_question({**DOC_ANSWERS, "driver_name": {**DOC_ANSWERS["driver_name"], "ros2_distro": None}})
        result = self.upload_parsed("sheet.xlsx", lib.xlsx_bytes())
        saved = self.ok(self.put(result["material"]["id"], {"revision": result["revision"], "not_applicable": ["ros2_distro"]}))
        self.assertEqual((saved["edited_by"], saved["status"]), ("owner@parse.test", "parsed"))

    def test_rules_and_documents_can_run_on_separate_threads(self):
        urdf = self.upload(self.owner, "arm.urdf", files.URDF_OK)
        doc = self.upload(self.owner, "manual.docx", lib.docx_bytes("分开"))
        for _ in range(5):
            self.clock += 120
            material_parse.run_pending(self.db, self.settings, at=self.clock, kinds=("document",))
        self.assertEqual(self.result(urdf["id"])["status"], "parsing")
        self.assertEqual(self.result(doc["id"])["status"], "parsed")
        material_parse.run_pending(self.db, self.settings, at=self.clock, kinds=("urdf", "package"))
        self.assertEqual(self.result(urdf["id"])["status"], "parsed")


class NoServiceTest(ParseCase):
    weknora = False

    def test_without_a_knowledge_service_documents_are_filled_in_by_hand(self):
        result = self.upload_parsed("manual.pdf", lib.pdf_bytes())
        self.assertEqual(result["status"], "failed")
        self.assertIn("没有配置文档解析服务", result["status_detail"])
        values = {"driver_name": "d", "driver_version": "1", "source": "U 盘", "os": "Ubuntu 22.04",
                  "ros2_distro": ["humble"], "install_steps": ["按手册第 3 章"], "topics": ["/odom"]}
        done = self.ok(self.confirm(result["material"]["id"], {"revision": result["revision"], "values": values}))
        self.assertEqual((done["status"], done["values"]["os"], done["values"]["topics"]),
                         ("usable", ["Ubuntu 22.04"], [{"name": "/odom", "type": ""}]))


class EditingTest(ParseCase):
    weknora = False

    def setUp(self):
        super().setUp()
        self.result_ = self.upload_parsed("arm.urdf", files.URDF_OK)
        self.material_id = self.result_["material"]["id"]

    def test_a_stale_revision_is_refused(self):
        self.ok(self.put(self.material_id, {"revision": self.result_["revision"], "values": {"robot_name": "a"}}))
        self.error(self.put(self.material_id, {"revision": self.result_["revision"], "values": {"robot_name": "b"}}),
                   409, "parse_changed")

    def test_unknown_items_and_wrong_not_applicable_are_refused(self):
        revision = self.result_["revision"]
        self.error(self.put(self.material_id, {"revision": revision, "values": {"run": "rm -rf /"}}), 400,
                   "invalid_parse_values")
        self.error(self.put(self.material_id, {"revision": revision, "values": {}, "not_applicable": ["robot_name"]}), 400,
                   "invalid_parse_values")
        self.error(self.put(self.material_id, {"revision": revision, "values": "x"}), 400, "invalid_parse_values")

    def test_editing_a_confirmed_result_needs_a_new_confirmation(self):
        done = self.ok(self.confirm(self.material_id, {"revision": self.result_["revision"]}))
        edited = self.ok(self.put(self.material_id, {"revision": done["revision"], "values": {"robot_name": "renamed"}}))
        self.assertEqual((edited["status"], edited["confirmed_by"], edited["confirmed_at"]), ("parsed", "", None))

    def test_parsing_again_starts_over(self):
        done = self.ok(self.confirm(self.material_id, {"revision": self.result_["revision"]}))
        again = self.ok(self.post(f"/library/materials/{self.material_id}/parse/retry", {}, self.owner))
        self.assertEqual(again["status"], "parsing")
        self.error(self.confirm(self.material_id, {"revision": again["revision"]}), 409, "parse_running")
        self.error(self.post(f"/library/materials/{self.material_id}/parse/retry", {}, self.owner), 409, "parse_running")
        self.parse_all()
        result = self.result(self.material_id)
        self.assertEqual((result["status"], result["confirmed_by"]), ("parsed", ""))
        self.assertGreater(result["revision"], done["revision"])

    def test_who_may_do_what(self):
        operator = self.member(self.owner, "field@parse.test", "operator")
        self.assertEqual(self.result(self.material_id, operator)["status"], "parsed")
        self.error(self.put(self.material_id, {"revision": self.result_["revision"], "values": {}}, operator), 403)
        self.error(self.confirm(self.material_id, {"revision": self.result_["revision"]}, operator), 403)
        self.error(self.post(f"/library/materials/{self.material_id}/parse/retry", {}, operator), 403)
        stranger = self.register("stranger@else.test")
        self.error(self.get(f"/library/materials/{self.material_id}/parse", stranger), 404)
        self.error(self.confirm(self.material_id, {"revision": self.result_["revision"]}, stranger), 404)
        self.error(self.client.get(f"{API}/library/materials/{self.material_id}/parse"), 401)

    def test_the_audit_log_names_who_edited_and_confirmed(self):
        self.ok(self.confirm(self.material_id, {"revision": self.result_["revision"]}))
        from neuxmind_cloud.models import AuditLog
        with self.db.transaction() as s:
            actions = [row.action for row in s.query(AuditLog).filter(AuditLog.target == self.material_id)]
        self.assertIn("library.parse_finished", actions)
        self.assertIn("library.parse_confirmed", actions)


class WorkerTest(ParseCase):
    weknora = False

    def test_a_job_held_by_another_worker_is_left_alone_until_its_lease_ends(self):
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK)
        material_parse._new_jobs(self.db)
        with self.db.transaction() as s:
            job = s.get(MaterialParse, material["id"])
            job.lease_until, job.lease_owner = self.clock + 500, "someone-else"
        self.assertEqual(material_parse.run_pending(self.db, self.settings, at=self.clock + 100), 0)
        self.assertEqual(self.result(material["id"])["status"], "parsing")
        self.assertEqual(material_parse.run_pending(self.db, self.settings, at=self.clock + 600), 1)
        self.assertEqual(self.result(material["id"])["status"], "parsed")

    def test_a_material_deleted_while_parsing_leaves_nothing_behind(self):
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK)
        material_parse._new_jobs(self.db)
        self.ok(self.delete(f"/library/materials/{material['id']}", self.owner))
        self.parse_all(rounds=2)
        material_parse.cleanup(self.db, None)
        with self.db.transaction() as s:
            self.assertEqual(s.query(MaterialParse).count(), 0)

    def test_devices_keep_their_own_materials(self):
        labels = {"device_id": "robot-aaaaaaaaaaaa", "device_name": "一号机"}
        first = self.upload(self.owner, "arm.urdf", files.URDF_OK, **labels)
        self.assertEqual((first["device_id"], first["device_name"]), ("robot-aaaaaaaaaaaa", "一号机"))
        other = self.upload(self.owner, "arm.urdf", files.URDF_OK, device_id="robot-bbbbbbbbbbbb")
        self.assertNotEqual(first["id"], other["id"])
        self.error(self.start_raw(self.owner, "arm.urdf", files.URDF_OK, **labels), 409, "material_exists")
        listed = self.ok(self.get("/library/materials?device_id=robot-aaaaaaaaaaaa", self.owner))
        self.assertEqual([m["id"] for m in listed["materials"]], [first["id"]])
        self.error(self.start_raw(self.owner, "x.urdf", files.URDF_OK, device_id="../../etc"), 400)
