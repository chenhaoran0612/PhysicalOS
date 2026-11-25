"""Issue #45: an HTML page is a document of the material library.

It is accepted by what is inside it, only ever handed out as a download, and
parsed like any document: its text is taken out by the rules parser in the
child process (scripts and styles are dropped, nothing is fetched or run) and
that text goes to the knowledge service. Every page is made up here."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import library_support as lib
from library_support import LibraryCase, sha
from test_material_parse import ParseCase

from neuxmind_cloud import library_types as types
from neuxmind_cloud import material_parse
from neuxmind_cloud import material_sandbox as sandbox
from neuxmind_cloud.material_sandbox import ExtractFailed, Limits

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>示例驱动安装说明</title>
<style>body{color:red} .hidden-style-text{}</style>
<script>var secretScript = "do not keep";</script></head>
<body><nav><a href="/home">首页</a></nav>
<h1>demo_motion_driver 1.4.0</h1>
<p>适用于 Ubuntu&nbsp;22.04 &amp; ROS&nbsp;2 humble。下载：<a
   href="https://example.test/demo-1.4.0.tar.gz">安装包</a></p>
<h2>安装步骤</h2>
<ol><li>解压安装包</li><li>运行 <code>colcon build</code></li></ol>
<pre>sudo apt install ros-humble-demo-motion
source /opt/ros/humble/setup.bash</pre>
<table><tr><th>话题</th><th>类型</th></tr><tr><td>/cmd_vel</td><td>geometry_msgs/msg/Twist</td></tr></table>
<template><p>template text</p></template>
<!-- a comment that is not kept -->
<img src="https://example.test/track.gif" alt="示意图">
</body></html>
""".encode()
ONLY_SCRIPT = b"<!DOCTYPE html><html><head><script>document.write('built later')</script></head><body></body></html>"


class TypeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nxhtml-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def accepted(self, data: bytes) -> bool:
        path = os.path.join(self.tmp, "candidate")
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            types.check_file(types.BY_ID["html"], path, len(data))
        except types.Unsupported:
            return False
        return True

    def test_both_suffixes_are_a_document(self):
        for name in ("安装说明.html", "GUIDE.HTM"):
            cleaned, fmt = types.clean_filename(name)
            self.assertEqual((fmt.id, fmt.kind), ("html", types.DOCUMENT))
            self.assertEqual(cleaned, name.rsplit(".", 1)[0] + "." + name.rsplit(".", 1)[1].lower())
        sentence = types.supported_text()
        self.assertLess(sentence.index("文本（"), sentence.index("网页（.html、.htm）"))
        self.assertLess(sentence.index("网页（"), sentence.index("URDF（"))
        self.assertIn(".htm", types.EXTENSIONS)

    def test_a_page_in_any_of_the_text_encodings_is_accepted(self):
        text = PAGE.decode()
        for data in (PAGE, b"\xef\xbb\xbf" + PAGE, text.encode("gb18030"), text.encode("utf-16"),
                     b"\n  <p>a fragment without a head</p>\n"):
            self.assertTrue(self.accepted(data))

    def test_what_is_not_a_page_is_refused(self):
        for data in (b"\x7fELF\x02\x01\x01" + bytes(200), b"MZ\x90\x00" + bytes(100), b"plain words, no markup",
                     b"%PDF-1.7\n<html></html>", b"<html>\x00\x01\x02</html>", b""):
            self.assertFalse(self.accepted(data))


class UploadTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@html.test")

    def test_a_page_is_stored_and_listed_as_a_document(self):
        material = self.upload(self.user, "安装说明.html", PAGE)
        self.assertEqual((material["format"], material["kind"], material["filename"]), ("html", "document", "安装说明.html"))
        info = self.ok(self.get("/library/formats", self.user))
        self.assertIn(".html", info["extensions"])
        self.assertIn(".htm", info["extensions"])
        self.assertIn(".html", info["supported"])

    def test_a_program_named_like_a_page_is_refused(self):
        body = self.error(self.first_refusal(self.user, "程序.html", b"\x7fELF\x02\x01\x01" + bytes(300)),
                          415, "unsupported_type")
        self.assertIn(".html", body["supported"])
        self.assertEqual(self.stored_files(), [])

    def test_a_stored_page_is_only_ever_a_download(self):
        material = self.upload(self.user, "page.html", PAGE)
        response = self.content(self.user, material["id"])
        self.assertEqual(response.content, PAGE)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-checksum-sha256"], sha(PAGE))


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nxhtml-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def facts(self, data: bytes, limits: Limits = Limits()) -> dict:
        path = os.path.join(self.tmp, "material")
        with open(path, "wb") as handle:
            handle.write(data)
        return sandbox.extract("html", path, limits)

    def test_the_text_keeps_headings_lists_commands_tables_and_link_targets(self):
        facts = self.facts(PAGE)
        text = facts["text"]
        lines = text.split("\n")
        self.assertEqual((facts["title"], facts["truncated"]), ("示例驱动安装说明", False))
        self.assertEqual(lines[0], "# 示例驱动安装说明")
        self.assertIn("# demo_motion_driver 1.4.0", lines)
        self.assertIn("## 安装步骤", lines)
        self.assertIn("适用于 Ubuntu 22.04 & ROS 2 humble。下载：安装包 (https://example.test/demo-1.4.0.tar.gz)", lines)
        self.assertIn("- 解压安装包", lines)
        self.assertIn("- 运行 colcon build", lines)
        self.assertIn("```\nsudo apt install ros-humble-demo-motion\nsource /opt/ros/humble/setup.bash\n```", text)
        self.assertIn("话题 | 类型", lines)
        self.assertIn("/cmd_vel | geometry_msgs/msg/Twist", lines)
        self.assertIn("首页", lines)                      # a link inside the site: its words, no address

    def test_scripts_styles_templates_and_comments_are_left_out(self):
        text = self.facts(PAGE)["text"]
        for unwanted in ("secretScript", "do not keep", "color:red", "template text", "a comment", "track.gif", "/home"):
            self.assertNotIn(unwanted, text)
        self.assertNotIn("\n\n\n", text)

    def test_other_encodings_are_read(self):
        for data in (PAGE.decode().encode("gb18030"), PAGE.decode().encode("utf-16"), b"\xef\xbb\xbf" + PAGE):
            self.assertIn("## 安装步骤", self.facts(data)["text"].split("\n"))

    def test_broken_markup_still_gives_its_text(self):
        facts = self.facts("<p>第一段<p>第二段<b>加粗<li>一项</ul></div>&unknown; &#x4e2d; &#99999999; 尾".encode())
        lines = facts["text"].split("\n")
        self.assertEqual(lines[:2], ["第一段", "第二段加粗"])
        self.assertIn("- 一项", lines)
        self.assertIn("&unknown; 中", facts["text"])
        self.assertEqual(self.facts(b"<div>" * 100_000 + b"deep")["text"], "deep")

    def test_a_script_that_is_never_closed_hides_what_follows(self):
        self.assertEqual(self.facts(b"<p>shown</p><script>alert(1) <p>not shown</p>")["text"], "shown")

    def test_a_link_only_shows_a_web_address(self):
        text = self.facts(b'<a href="javascript:alert(1)">run</a> <a href="file:///etc/passwd">file</a> '
                          b'<a href="HTTPS://example.test/a b">site</a> '
                          b'<a href="https://example.test/x">https://example.test/x</a>')["text"]
        self.assertEqual(text, "run file site https://example.test/x")

    def test_a_page_whose_words_come_from_a_script_has_no_text(self):
        self.assertEqual(self.facts(ONLY_SCRIPT)["text"], "")

    def test_a_long_page_is_cut_at_the_limit(self):
        facts = self.facts(("<p>" + "一行文字。" * 20 + "</p>\n").encode() * 500, Limits(html_text_bytes=3000))
        self.assertTrue(facts["truncated"])
        self.assertLessEqual(len(facts["text"].encode()), 3000)
        self.assertGreater(len(facts["text"].encode()), 2000)
        facts["text"].encode().decode()                    # cut between characters, never inside one

    def test_a_page_above_the_size_limit_is_refused(self):
        with self.assertRaises(ExtractFailed) as caught:
            self.facts(PAGE, Limits(html_bytes=200))
        self.assertEqual(caught.exception.code, "too_large")


    def test_a_table_row_stays_on_one_line_whatever_its_cells_hold(self):
        lines = self.facts(b"<table><tr><td><p>Max speed</p></td><td><p>1.5 m/s</p></td></tr>"
                           b"<tr><td>Max<br>torque</td><td><h3>40</h3><ul><li>N</li><li>m</li></ul></td></tr>"
                           b"<tr><td>Name</td><td><table><tr><td>a</td><td>b</td></tr></table></td><td>Type</td></tr>"
                           b"<tr><td><pre>ros2 topic\nlist</pre></td><td>cmd</td></tr></table><p>after</p>")["text"].split("\n")
        self.assertEqual(lines, ["Max speed | 1.5 m/s", "Max torque | 40 N m", "Name | a | b | Type",
                                 "ros2 topic list | cmd", "after"])

    def test_cells_and_rows_without_a_table_do_not_break_the_reader(self):
        self.assertEqual(self.facts(b"<td>a</td><td>b</td></tr></table></td><p>c</p>")["text"], "a | b\nc")

    def test_a_page_with_only_a_title_has_no_text(self):
        facts = self.facts(b"<html><head><title>Docs</title></head><body><div id=app></div>"
                           b"<script>render()</script></body></html>")
        self.assertEqual((facts["text"], facts["title"]), ("", "Docs"))

    def test_a_title_that_is_never_closed_does_not_swallow_the_page(self):
        facts = self.facts(b"<html><head><title>My Page<body><p>hi</p></body></html>")
        self.assertEqual((facts["title"], facts["text"]), ("My Page", "# My Page\nhi"))
        self.assertEqual(self.facts(b"<title>a<b>b</b>c</title>x")["text"], "# a\nbc\nx")

    def test_a_comment_that_is_never_closed_is_not_text(self):
        self.assertEqual(self.facts(b"<p>a</p><!-- never closed <p>b</p>")["text"], "a")
        self.assertEqual(self.facts(b"<p>&lt;!-- shown as text --&gt; b</p>")["text"], "<!-- shown as text --> b")

    def test_line_ends_of_every_kind_separate_words_and_lines(self):
        self.assertEqual(self.facts(b"<p>one\rtwo\x0cthree\x0bfour</p>")["text"], "one two three four")
        self.assertEqual(self.facts(b"<pre>a\rb\r\nc\n\n\n  d</pre>")["text"], "```\na\nb\nc\n\n  d\n```")

    def test_commands_are_fenced_so_a_comment_line_is_not_a_heading(self):
        text = self.facts(b"<p>run</p><pre>\n# install deps\npip install x\n</pre><p>done</p>")["text"]
        self.assertEqual(text, "run\n```\n# install deps\npip install x\n```\ndone")

    def test_nested_lists_keep_their_level(self):
        text = self.facts(b"<ul><li>a<ul><li>b<ol><li>c</li></ol></li></ul></li><li>d</li></ul>")["text"]
        self.assertEqual(text, "- a\n  - b\n    - c\n- d")

    def test_inline_markup_neither_splits_nor_joins_words(self):
        self.assertEqual(self.facts(b"<p><b>a</b>b <i>c</i>d<span> e</span></p><P>UPPER</P><SCRIPT>x()</SCRIPT>")["text"],
                         "ab cd e\nUPPER")
        self.assertEqual(self.facts(b"<p>a</p><script src=x /><iframe /><svg/><p>b</p>")["text"], "a\nb")

    def test_a_link_without_words_or_around_blocks_keeps_its_address_apart(self):
        self.assertEqual(self.facts(b"<a href=https://e.test/x></a>text")["text"], "https://e.test/x text")
        self.assertEqual(self.facts(b"<a href=https://e.test/a><div>One</div><div>Two</div></a> after")["text"],
                         "One\nTwo\nhttps://e.test/a after")

    def test_an_address_cannot_close_its_own_brackets(self):
        text = self.facts(b"<a href='https://x.test/a)![i](https://evil.test/p.png)'>go</a>")["text"]
        self.assertEqual(text, "go")

    def test_control_and_direction_characters_are_dropped(self):
        text = self.facts("<p>a&#x9d;b&#x202e;c&#1;d&#x2028;e</p>".encode())["text"]
        self.assertEqual(text, "abcd e")

    def test_broken_markup_that_is_slow_to_read_fails_within_the_page_limit(self):
        started = time.monotonic()
        for data in (b"x</a>" * 100_000, b"<i>ab" * 2_000_000):
            facts = self.facts(data, Limits(html_cpu_seconds=20))
            self.assertTrue(facts["text"].startswith(("x", "ab")))
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(self.facts(b"<i>ab" * 2_000_000)["truncated"])

    def test_a_page_that_takes_too_long_fails_alone(self):
        # An unfinished start tag repeated: the standard library's reader needs minutes for it on some versions.
        started = time.monotonic()
        try:
            self.facts(b"<a " * 40_000, Limits(html_cpu_seconds=2))
        except ExtractFailed as exc:
            self.assertEqual(exc.code, "parse_timeout")
        self.assertLess(time.monotonic() - started, 15)

    def test_the_title_line_counts_against_the_limit(self):
        facts = self.facts(("<title>" + "题" * 80 + "</title><p>" + "字" * 2000 + "</p>").encode(), Limits(html_text_bytes=900))
        self.assertTrue(facts["truncated"])
        self.assertLessEqual(len(facts["text"].encode()), 900)


class ParseTest(ParseCase):
    def test_the_text_of_a_page_goes_to_the_knowledge_service(self):
        result = self.upload_parsed("安装说明.html", PAGE, device_id="robot-0123456789ab", device_name="一号机")
        self.assertEqual((result["status"], result["parser"]), ("parsed", "weknora"))
        self.assertEqual(result["values"]["driver_name"], "demo_motion_driver")
        self.assertEqual(len(self.fake.uploads), 1)
        sent = self.fake.uploads[0]
        self.assertEqual(sent["fileName"], "安装说明.md")
        self.assertTrue(sent["part_filename"].endswith(".md"))
        text = sent["content"].decode("utf-8")
        self.assertIn("# demo_motion_driver 1.4.0", text.split("\n"))
        self.assertNotIn("secretScript", text)
        self.assertNotIn("<h1>", text)
        self.assertNotEqual(sent["sha256"], result["material"]["sha256"])
        self.assertEqual(sent["metadata"]["neuxmind_material_id"], result["material"]["id"])
        self.assertEqual(len(self.fake.questions), 3)

    def test_the_short_suffix_is_parsed_the_same_way(self):
        result = self.upload_parsed("guide.htm", PAGE)
        self.assertEqual((result["status"], self.fake.uploads[0]["fileName"]), ("parsed", "guide.md"))

    def test_a_page_without_words_is_not_sent(self):
        result = self.upload_parsed("app.html", ONLY_SCRIPT)
        self.assertEqual((result["status"], self.fake.uploads, self.fake.questions), ("failed", [], []))
        self.assertIn("没有可以读出的文字", result["status_detail"])

    def test_the_service_limit_counts_the_text_not_the_page(self):
        self.settings.weknora_max_bytes = 1000
        page = b"<html><script>" + b"x" * 5000 + b"</script><body><p>demo_motion_driver 1.4.0</p></body></html>"
        result = self.upload_parsed("heavy.html", page)
        self.assertEqual((result["status"], self.fake.uploads[0]["content"]), ("parsed", b"demo_motion_driver 1.4.0\n"))

    def test_a_cut_page_says_so(self):
        cut = Limits(html_text_bytes=3000)
        with mock.patch.object(material_parse, "_limits", lambda settings: cut):
            result = self.upload_parsed("long.html", ("<p>" + "一行文字。" * 20 + "</p>\n").encode() * 500)
        self.assertEqual(result["status"], "parsed")
        self.assertLessEqual(self.fake.uploads[0]["size"], 3001)
        self.assertTrue(any("只解析了前面" in warning for warning in result["warnings"]))

    def test_a_page_the_rules_cannot_read_fails_alone(self):
        small = Limits(html_bytes=100)
        with mock.patch.object(material_parse, "_limits", lambda settings: small):
            result = self.upload_parsed("big.html", PAGE)
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))
        other = self.upload_parsed("notes.md", b"# demo\n")
        self.assertEqual(other["status"], "parsed")

    def test_the_same_page_for_two_devices_is_converted_and_asked_about_once(self):
        self.upload_parsed("a.html", PAGE, device_id="robot-0123456789ab", device_name="一号机")
        second = self.upload_parsed("a.html", PAGE, device_id="robot-0123456789ac", device_name="二号机")
        self.assertEqual((second["status"], len(self.fake.uploads), len(self.fake.questions)), ("parsed", 1, 3))

    def test_a_page_with_only_a_title_is_not_sent(self):
        result = self.upload_parsed("spa.html", b"<html><head><title>Docs</title></head><body><script>r()</script></body></html>")
        self.assertEqual((result["status"], self.fake.uploads), ("failed", []))

    def test_the_note_about_a_cut_page_stays_when_it_is_parsed_again_or_reused(self):
        cut = Limits(html_text_bytes=3000)
        page = ("<p>" + "一行文字。" * 20 + "</p>\n").encode() * 500
        with mock.patch.object(material_parse, "_limits", lambda settings: cut):
            first = self.upload_parsed("long.html", page, device_id="robot-0123456789ab", device_name="一号机")
            self.ok(self.post(f"/library/materials/{first['material']['id']}/parse/retry", {}, self.owner))
            self.parse_all()
            again = self.result(first["material"]["id"])
            second = self.upload_parsed("long.html", page, device_id="robot-0123456789ac", device_name="二号机")
        self.assertEqual((again["status"], second["status"], len(self.fake.uploads)), ("parsed", "parsed", 1))
        for result in (first, again, second):
            self.assertTrue(any("只解析了前面" in warning for warning in result["warnings"]), result["warnings"])

    def test_a_page_whose_copy_went_away_is_converted_and_sent_again(self):
        self.fake.drop_before_chat = True
        result = self.upload_parsed("guide.html", PAGE)
        self.assertEqual((result["status"], len(self.fake.uploads)), ("parsed", 2))
        self.assertEqual({upload["fileName"] for upload in self.fake.uploads}, {"guide.md"})
        self.assertEqual(self.fake.uploads[0]["sha256"], self.fake.uploads[1]["sha256"])

    def test_no_temporary_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(tempfile, "tempdir", scratch):
            self.upload_parsed("guide.html", PAGE)
            self.assertEqual(os.listdir(scratch), [])


class NoServiceTest(ParseCase):
    weknora = False

    def test_without_a_knowledge_service_a_page_is_filled_in_by_hand(self):
        result = self.upload_parsed("guide.html", PAGE)
        self.assertEqual(result["status"], "failed")
        self.assertIn("WeKnora", result["status_detail"])


if __name__ == "__main__":
    unittest.main()
