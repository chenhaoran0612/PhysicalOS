"""Round 03, contract §5.3: the desktop's built-in engineering runtime (Codex
0.146.1 app-server, Responses streaming + function tools) through a metered
gateway session.

``fixtures/codex-0.146.1-responses.json`` holds request bodies recorded from
the real Codex 0.146.1 binary (see the file's ``_comment``). These tests check
that the session allow-list accepts exactly those shapes, that what reaches the
upstream is what the README says (``client_metadata`` dropped,
``prompt_cache_key`` scoped per organisation, ``store: false``), that the
refusals with a safety reason are kept, that the Echo test double can drive a
tool loop, and that a tool-loop client never sees 409 ``session_busy`` for its
next call after the upstream's terminal event.
"""
import copy
import json
import threading
import time
from pathlib import Path

import httpx

import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase, LiveServer, sse_events

from neuxmind_cloud import gateway
from neuxmind_cloud.errors import ApiError
from neuxmind_cloud.session_params import sanitize, scoped_cache_key
from neuxmind_cloud.upstream import EchoProvider

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "codex-0.146.1-responses.json").read_text())
BODIES = [r["body"] for r in RECORDED["requests"]]
TOOLS_LLM = {**ECHO_LLM, "id": "echo-tools", "name": "Echo tools (test double)",
             "upstream": {"provider": "echo", "behavior": "tools", "tool_name": "exec_command",
                          "tool_arguments": {"cmd": "printf ok > done.txt"}}}


def codex_body(index=0, **changes):
    body = copy.deepcopy(BODIES[index])
    body.update(changes)
    return body


class RecordedShapesTest(CloudTestCase):
    """Pure allow-list checks on the recorded bodies."""

    def test_every_recorded_codex_request_is_accepted(self):
        self.assertEqual(RECORDED["codex_version"], "0.146.1")
        for body in BODIES:
            forward, bound, requested, stream = sanitize("/responses", body)
            self.assertTrue(stream)
            self.assertIsNone(requested)                  # Codex sends no output limit; the gateway writes it
            self.assertNotIn("client_metadata", forward)  # validated, never forwarded
            self.assertIs(forward["store"], False)
            self.assertEqual(forward["include"], ["reasoning.encrypted_content"])
            self.assertIn("prompt_cache_key", forward)
            self.assertNotIn("model", forward)
            # the bound covers every forwarded byte
            self.assertGreater(bound, len(json.dumps(forward, ensure_ascii=False, separators=(",", ":")).encode()))

    def test_recorded_shapes_cover_what_the_runtime_actually_sends(self):
        kinds = {item.get("type") for body in BODIES for item in body["input"]}
        self.assertTrue({"message", "function_call", "function_call_output", "reasoning"} <= kinds)
        self.assertIn("namespace", {tool["type"] for tool in BODIES[0]["tools"]})

    def refused(self, body, code, status=400):
        with self.assertRaises(ApiError) as caught:
            sanitize("/responses", body)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code), caught.exception.message)
        return caught.exception

    def test_refusals_with_a_safety_reason_are_kept(self):
        # store=true keeps data on the operator's provider account
        self.refused(codex_body(store=True), "parameter_not_allowed")
        # include: only the encrypted reasoning of this response
        self.refused(codex_body(include=["file_search_call.results"]), "parameter_not_allowed")
        self.refused(codex_body(include=["message.input_image.image_url"]), "parameter_not_allowed")
        # still refused upstream state / pricing tiers
        for extra in ({"previous_response_id": "resp_1"}, {"service_tier": "priority"}, {"metadata": {}},
                      {"conversation": "c"}, {"background": True}, {"truncation": "auto"}):
            self.refused(codex_body(**extra), "parameter_not_allowed")
        # built-in / custom tools, also inside a namespace
        self.refused(codex_body(tools=[{"type": "web_search"}]), "tool_not_allowed")
        self.refused(codex_body(tools=[{"type": "custom", "name": "apply_patch", "format": {"type": "text"}}]),
                     "tool_not_allowed")
        self.refused(codex_body(tools=[{"type": "namespace", "name": "ns", "tools": [{"type": "web_search"}]}]),
                     "tool_not_allowed")
        self.refused(codex_body(tools=[{"type": "namespace", "name": "ns", "tools": [
            {"type": "namespace", "name": "inner", "tools": []}]}]), "tool_not_allowed")
        self.refused(codex_body(tools=[{"type": "namespace", "name": "ns", "tools": [], "x": 1}]),
                     "parameter_not_allowed")

    def test_input_items_stay_text_only(self):
        image = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
        self.refused(codex_body(input=[{"type": "function_call_output", "call_id": "c", "output": image}]),
                     "content_not_allowed")
        ok = codex_body(input=[{"type": "function_call_output", "call_id": "c",
                                "output": [{"type": "input_text", "text": "tool result"}]}])
        sanitize("/responses", ok)
        self.refused(codex_body(input=[{"type": "reasoning", "id": "rs", "summary": [], "extra": 1}]),
                     "parameter_not_allowed")
        self.refused(codex_body(input=[{"type": "reasoning", "summary": [{"type": "image", "text": "x"}]}]),
                     "invalid_parameter")
        self.refused(codex_body(input=[{"type": "custom_tool_call", "call_id": "c", "name": "x", "input": "y"}]),
                     "content_not_allowed")
        self.refused(codex_body(input=[{"type": "item_reference", "id": "msg_1"}]), "content_not_allowed")

    def test_client_metadata_cannot_carry_large_payloads(self):
        self.refused(codex_body(client_metadata={"k": "v" * 5000}), "invalid_parameter")
        self.refused(codex_body(client_metadata={f"k{i}": "v" for i in range(40)}), "invalid_parameter")
        self.refused(codex_body(client_metadata=["x"]), "invalid_parameter")
        self.refused(codex_body(prompt_cache_key="k" * 300), "invalid_parameter")

    def test_cache_key_is_scoped_per_organisation(self):
        a, b = scoped_cache_key("org-a", "thread-1"), scoped_cache_key("org-b", "thread-1")
        self.assertNotEqual(a, b)
        self.assertEqual(a, scoped_cache_key("org-a", "thread-1"))
        self.assertTrue(a.startswith("nx-"))
        self.assertNotIn("thread-1", a)


class _Recording(EchoProvider):
    sent: list = []

    def open_stream(self, path, body):
        type(self).sent.append(copy.deepcopy(body))
        return super().open_stream(path, body)


class _GatewayBase(CloudTestCase):
    """Recorded Codex requests through the real app (TestClient) with the Echo
    tool-loop double."""

    def provider_factory(self):
        _Recording.sent = []
        return lambda upstream: _Recording(upstream.get("behavior", "ok"), 0, upstream.get("tool_name"),
                                           upstream.get("tool_arguments"))

    def setUp(self):
        super().setUp()
        self.user = self.register("codex@example.com")
        self.user["org_id"] = self.ok(self.get("/me", self.user))["current_org_id"]
        self.publish(TOOLS_LLM)
        self.entitle(self.user, "echo-tools")
        self.fund(self.user, 10_000)

    def open(self, max_tokens=200_000, user=None):
        user = user or self.user
        self._n = getattr(self, "_n", 0) + 1
        return self.ok(self.post("/gateway/sessions", {"request_id": f"codex-{self._n}", "model": "echo-tools",
                                                       "max_tokens": max_tokens, "max_minutes": 15}, user), 201)

    def call(self, session, body):
        response = self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/responses", json=body,
                                    headers={"Authorization": f"Bearer {session['access_token']}"})
        return response



class CodexThroughGatewayTest(_GatewayBase):
    def test_recorded_requests_stream_through_and_settle(self):
        session = self.open()
        first = codex_body(0, input=[i for i in BODIES[0]["input"] if i["type"] == "message"])
        response = self.call(session, first)
        self.assertEqual(response.status_code, 200, response.text)
        events = sse_events(response.text)
        names = [n for n, _ in events]
        self.assertEqual(names[-1], "neuxmind.metering")
        self.assertEqual(names[-2], "response.completed")
        done = [json.loads(d) for n, d in events if n == "response.output_item.done"]
        self.assertEqual(done[0]["item"]["type"], "function_call")
        self.assertEqual(done[0]["item"]["name"], "exec_command")
        self.assertTrue(all(json.loads(d).get("simulated") for n, d in events if n.startswith("response.")))
        metering = json.loads(events[-1][1])
        self.assertEqual(metering["status"], "succeeded")
        # what reached the upstream
        sent = _Recording.sent[-1]
        self.assertNotIn("client_metadata", sent)
        self.assertIs(sent["store"], False)
        self.assertEqual(sent["include"], ["reasoning.encrypted_content"])
        self.assertEqual(sent["prompt_cache_key"], scoped_cache_key(self.user["org_id"], first["prompt_cache_key"]))
        self.assertEqual(sent["max_output_tokens"], metering["output_limit"])
        self.assertEqual([t["type"] for t in sent["tools"]], [t["type"] for t in first["tools"]])

        # The follow-up request (with function_call_output) gets a message.
        response = self.call(session, codex_body(0))
        events = sse_events(response.text)
        done = [json.loads(d) for n, d in events if n == "response.output_item.done"]
        self.assertEqual(done[0]["item"]["type"], "message")
        state = self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))
        self.assertEqual(state["inflight_tokens"], 0)
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c1"},
                                   self.user))
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["usage_tokens"], state["used_tokens"])
        self.assertGreater(closed["credits_charged"], 0)

    def test_reasoning_item_round_trip_is_accepted(self):
        session = self.open()
        response = self.call(session, codex_body(2))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sse_events(response.text)[-1][0], "neuxmind.metering")
        reasoning = [i for i in _Recording.sent[-1]["input"] if i["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertTrue(reasoning[0]["encrypted_content"].startswith("gAAAAB"))

    def test_budget_too_small_for_a_codex_request_is_a_plain_402(self):
        # A Codex request is ~50 KB (instructions + ~20 tool schemas); its input
        # bound alone exceeds a 30k-token session.
        session = self.open(max_tokens=30_000)
        body = self.error(self.call(session, codex_body(0)), 402, "session_budget_exhausted")
        self.assertLess(body["remaining"], 50_000)

    def test_two_organisations_get_different_cache_keys(self):
        other = self.register("codex2@example.com")
        other["org_id"] = self.ok(self.get("/me", other))["current_org_id"]
        self.entitle(other, "echo-tools")
        self.fund(other, 10_000)
        self.call(self.open(), codex_body(0))
        self.call(self.open(user=other), codex_body(0))
        keys = [b["prompt_cache_key"] for b in _Recording.sent[-2:]]
        self.assertEqual(len(set(keys)), 2)

    def test_echo_tools_card_validation(self):
        bad = {**TOOLS_LLM, "id": "bad-tools", "upstream": {"provider": "echo", "behavior": "tools", "tool_name": ""}}
        self.error(self.post("/admin/models", bad, self.operator()), 400)
        bad["upstream"] = {"provider": "echo", "behavior": "tools", "tool_arguments": "x"}
        self.error(self.post("/admin/models", bad, self.operator()), 400)


class TerminalEventAfterSettlementTest(CloudTestCase):
    """A tool-loop client sends its next call the moment it sees
    ``response.completed``. The gateway must have settled the call by then,
    or that next call hits 409 ``session_busy`` (ADR-016)."""

    def setUp(self):
        super().setUp()
        self.user = self.register("loop@example.com")
        self.publish(TOOLS_LLM)
        self.entitle(self.user, "echo-tools")
        self.fund(self.user, 10_000)
        self.live = LiveServer(self.app)
        self.http = httpx.Client(base_url=self.live.url, trust_env=False, timeout=20)
        original = gateway._settle_stream

        def slow_settle(db, call, outcome):
            time.sleep(0.6)       # widen the window that existed before the fix
            return original(db, call, outcome)
        gateway._settle_stream = slow_settle
        self.addCleanup(setattr, gateway, "_settle_stream", original)

    def tearDown(self):
        self.http.close()
        self.live.stop()
        super().tearDown()

    def test_next_call_right_after_the_terminal_event_is_not_busy(self):
        session = self.ok(self.post("/gateway/sessions", {"request_id": "loop", "model": "echo-tools",
                                                          "max_tokens": 400_000}, self.user), 201)
        url = f"{API}/gateway/sessions/{session['session_id']}/v1/responses"
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        first = codex_body(0, input=[i for i in BODIES[0]["input"] if i["type"] == "message"])
        statuses, seen_completed = [], threading.Event()
        with self.http.stream("POST", url, json=first, headers=headers) as response:
            self.assertEqual(response.status_code, 200)
            for line in response.iter_lines():
                if line == "event: response.completed":
                    seen_completed.set()
                    # exactly what Codex does: next request immediately
                    nxt = self.http.post(url, json=codex_body(0), headers=headers)
                    statuses.append(nxt.status_code)
                    break
        self.assertTrue(seen_completed.is_set())
        self.assertEqual(statuses, [200], "next tool-loop call was refused while the previous one settled")


def _padded(total_bytes, index=0):
    """A recorded Codex body grown with tool outputs, like a long session (review Q3-07)."""
    body = codex_body(index)
    chunk = "x" * 24_000
    n = 0
    while len(json.dumps(body).encode()) < total_bytes:
        n += 1
        body["input"] += [{"type": "function_call", "call_id": f"call_pad_{n}", "name": "exec_command", "arguments": "{}"},
                          {"type": "function_call_output", "call_id": f"call_pad_{n}", "output": chunk}]
    return body


class IterThreeReviewFixesTest(_GatewayBase):
    """Round 03 review: Q3-07 (session body limit), Q3-15 (cache key always
    scoped), mutation survivors C4 (+64 bound for the key) and C5 (128 functions)."""

    def test_session_calls_accept_bodies_above_the_general_limit_but_stay_budgeted(self):
        body = _padded(1_200_000)                       # > 1 MB (and > the 200 kB test limit)
        self.assertGreater(len(body["input"]), 90)
        # small session: the bytes count towards the input bound -> budget refusal, not 413
        refused = self.error(self.call(self.open(max_tokens=200_000), body), 402, "session_budget_exhausted")
        self.assertLessEqual(refused["remaining"], 200_000)
        # a session large enough for it goes through
        self.fund(self.user, 30_000)
        response = self.call(self.open(max_tokens=2_000_000), body)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(len(_Recording.sent[-1]["input"]), len(body["input"]))

    def test_other_routes_keep_the_general_limit(self):
        response = self.client.post(f"{API}/auth/login", content=b"{" + b" " * 300_000 + b"}",
                                    headers={"Content-Type": "application/json"})
        body = self.error(response, 413, "payload_too_large")
        self.assertEqual(body["max_bytes"], 200_000)
        self.assertNotIn("会话历史过长", body["error"])

    def test_cache_key_is_written_even_when_the_client_sends_none(self):
        body = codex_body(0)
        del body["prompt_cache_key"]
        self.call(self.open(), body)
        self.call(self.open(), codex_body(0))
        without, with_key = _Recording.sent[-2:]
        self.assertEqual(without["prompt_cache_key"], scoped_cache_key(self.user["org_id"], "default"))
        self.assertTrue(with_key["prompt_cache_key"].startswith("nx-"))
        self.assertNotEqual(without["prompt_cache_key"], with_key["prompt_cache_key"])

    def test_bound_includes_the_written_cache_key(self):
        from neuxmind_cloud.session_params import tool_count
        for keep in (True, False):
            body = codex_body(0)
            if not keep:
                del body["prompt_cache_key"]
            forward, bound, _, _ = sanitize("/responses", body)
            raw = len(json.dumps(forward, ensure_ascii=False, separators=(",", ":")).encode())
            self.assertEqual(bound, raw + 64 * (len(body["input"]) + tool_count(forward["tools"])) + 64)

    def test_function_total_is_capped_at_128_including_namespaces(self):
        fn = lambda i: {"type": "function", "name": f"f{i}", "parameters": {"type": "object"}}
        ns = {"type": "namespace", "name": "group", "tools": [fn(f"n{i}") for i in range(28)]}
        sanitize("/responses", codex_body(0, tools=[fn(i) for i in range(100)] + [ns]))     # exactly 128
        with self.assertRaises(ApiError) as caught:
            sanitize("/responses", codex_body(0, tools=[fn(i) for i in range(101)] + [ns]))  # 129
        self.assertEqual(caught.exception.status, 400)


class SessionBodyLimitTest(_GatewayBase):
    settings_overrides = {"session_max_body_bytes": 600_000}

    def test_oversized_session_call_is_413_with_a_hint(self):
        body = self.error(self.call(self.open(max_tokens=200_000), _padded(700_000)), 413, "payload_too_large")
        self.assertEqual(body["max_bytes"], 600_000)
        self.assertIn("会话历史过长", body["error"])
