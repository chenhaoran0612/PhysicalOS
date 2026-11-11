"""Review C-02 (session passthrough) and the surviving mutant C10 (budget cap).

The upstream is a recording test double: it captures exactly what the gateway
forwards and reports usage the way a real provider would (per returned choice,
up to the output limit it was given — or more, in "greedy" mode).
"""
import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase

from neuxmind_cloud.upstream import UpstreamResult


class RecordingUpstream:
    simulated = False

    def __init__(self, owner):
        self.owner = owner

    def passthrough(self, path, body):
        self.owner.seen.append((path, body))
        n = int(body.get("n", 1))
        limit = body.get("max_output_tokens") or body.get("max_tokens") or body.get("max_completion_tokens") or 0
        completion = self.owner.completion_tokens if self.owner.completion_tokens is not None else limit
        completion *= n
        prompt = 20
        data = {"id": f"up-{len(self.owner.seen)}",
                "choices": [{"index": i, "message": {"role": "assistant", "content": "x"}} for i in range(n)],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
        ref = f"openai_compatible:{data['id']}"
        return data, UpstreamResult("", prompt, completion, prompt + completion, ref)

    def complete(self, messages, max_tokens):  # pragma: no cover - not used here
        raise NotImplementedError


class SessionParamsTest(CloudTestCase):
    def provider_factory(self):
        self.seen = []
        self.completion_tokens = 5
        return lambda upstream: RecordingUpstream(self)

    def setUp(self):
        super().setUp()
        self.user = self.register("sess@example.com")
        self.publish(ECHO_LLM)
        self.publish({**ECHO_LLM, "id": "mct-llm", "upstream": {"provider": "openai_compatible", "model": "m",
                                                               "base_url": "http://127.0.0.1:9/v1",
                                                               "output_param": "max_completion_tokens"}})
        for model in ("echo-llm", "mct-llm"):
            self.entitle(self.user, model)
        self.fund(self.user, 100)

    def open(self, max_tokens=1000, model="echo-llm", request_id="s1"):
        return self.ok(self.post("/gateway/sessions", {"request_id": request_id, "model": model,
                                                       "max_tokens": max_tokens}, self.user), 201)

    def call(self, session, body, path="chat/completions"):
        return self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/{path}", json=body,
                                headers={"Authorization": f"Bearer {session['access_token']}"})

    def test_review_reproduction_n_and_max_completion_tokens(self):
        session = self.open()
        msg = [{"role": "user", "content": "hi"}]
        self.error(self.call(session, {"messages": msg, "n": 50, "max_completion_tokens": 100000}), 400, "n_unsupported")
        self.assertEqual(self.seen, [])
        self.completion_tokens = None   # the upstream uses its whole output limit
        reply = self.ok(self.call(session, {"messages": msg, "n": 1, "max_completion_tokens": 100000}))
        path, body = self.seen[-1]
        self.assertEqual(set(body), {"messages", "n", "max_tokens"})   # output_param of this card: max_tokens
        self.assertEqual(body["n"], 1)
        self.assertLess(body["max_tokens"], 1000)
        self.assertLessEqual(reply["neuxmind"]["used_tokens"], 1000)
        self.assertEqual(len(reply["choices"]), 1)
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.user))
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["credits_charged"], 10)   # ceil(used ≤ 1000 tokens × 10 / 1000)
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits", "balance": 90,
                                                  "reserved": 0, "available": 90})

    def test_output_limit_is_capped_by_remaining_budget(self):
        """Mutant C10 (no budget cap) must fail here."""
        session = self.open(max_tokens=1000)
        msg = [{"role": "user", "content": "hi"}]
        body = {"messages": msg}
        input_bound = len('{"messages":[{"role":"user","content":"hi"}]}'.encode()) + 64
        self.ok(self.call(session, {**body, "max_tokens": 999_999}))
        self.assertEqual(self.seen[-1][1]["max_tokens"], 1000 - input_bound)
        used = 20 + 5
        self.ok(self.call(session, {**body, "max_tokens": 50}))           # a smaller client value is honoured
        self.assertEqual(self.seen[-1][1]["max_tokens"], 50)
        used += 25
        self.ok(self.call(session, body))                                 # no value: the remaining budget
        self.assertEqual(self.seen[-1][1]["max_tokens"], 1000 - used - input_bound)

    def test_output_param_per_upstream_and_responses(self):
        session = self.open(model="mct-llm")
        self.ok(self.call(session, {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 40}))
        self.assertEqual(self.seen[-1][1]["max_completion_tokens"], 40)
        self.assertNotIn("max_tokens", self.seen[-1][1])
        session2 = self.open(request_id="s2")
        self.ok(self.call(session2, {"input": "hello", "max_output_tokens": 10 ** 6, "instructions": "be brief"},
                          path="responses"))
        path, body = self.seen[-1]
        self.assertEqual(path, "/responses")
        # prompt_cache_key: always written by the gateway, scoped per organisation (review Q3-15)
        self.assertEqual(set(body), {"input", "instructions", "max_output_tokens", "prompt_cache_key"})
        self.assertTrue(body["prompt_cache_key"].startswith("nx-"))
        self.assertLess(body["max_output_tokens"], 1000)

    def test_parameters_outside_the_allow_list_are_refused(self):
        session = self.open()
        msg = [{"role": "user", "content": "hi"}]
        for extra in ({"store": True}, {"metadata": {}}, {"service_tier": "priority"}, {"logit_bias": {"1": 5}},
                      {"audio": {}}, {"modalities": ["audio"]}, {"prediction": {}}, {"stream_options": {}},
                      {"web_search_options": {}}):
            body = self.error(self.call(session, {"messages": msg, **extra}), 400, "parameter_not_allowed")
            self.assertEqual(body["parameters"], list(extra))
        for extra in ({"previous_response_id": "resp_x"}, {"conversation": "c"}, {"prompt": {"id": "p"}},
                      {"background": True}, {"include": ["x"]}, {"max_tokens": 5}):
            self.error(self.call(session, {"input": "hi", **extra}, path="responses"), 400, "parameter_not_allowed")
        self.error(self.call(session, {"messages": msg, "temperature": 9}), 400, "invalid_parameter")
        self.error(self.call(session, {"messages": msg, "max_tokens": True}), 400, "invalid_parameter")
        self.assertEqual(self.seen, [])

    def test_only_text_content_and_function_tools(self):
        session = self.open()
        image = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]
        self.error(self.call(session, {"messages": image}), 400, "content_not_allowed")
        self.error(self.call(session, {"input": [{"role": "user", "content": [{"type": "input_image",
                                                                               "image_url": "https://x"}]}]},
                             path="responses"), 400, "content_not_allowed")
        self.error(self.call(session, {"input": [{"type": "input_file", "file_id": "f"}]}, path="responses"), 400,
                   "content_not_allowed")
        self.error(self.call(session, {"input": "hi", "tools": [{"type": "web_search"}]}, path="responses"), 400,
                   "tool_not_allowed")
        self.error(self.call(session, {"messages": [{"role": "user", "content": "hi"}],
                                       "tools": [{"type": "code_interpreter"}]}), 400, "tool_not_allowed")
        self.assertEqual(self.seen, [])
        tool = {"type": "function", "function": {"name": "move", "parameters": {"type": "object"}}}
        text_parts = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        self.ok(self.call(session, {"messages": text_parts, "tools": [tool], "tool_choice": "auto", "temperature": 0.2}))
        self.assertEqual(self.seen[-1][1]["tools"], [tool])

    def test_tools_count_towards_the_input_bound(self):
        session = self.open(max_tokens=300)
        big_tool = {"type": "function", "function": {"name": "t", "description": "d" * 400}}
        self.error(self.call(session, {"messages": [{"role": "user", "content": "hi"}], "tools": [big_tool]}), 402,
                   "session_budget_exhausted")

    def test_overage_in_session_charges_the_hold_withholds_output_and_locks(self):
        session = self.open(max_tokens=1000)
        self.completion_tokens = 50_000          # the upstream ignores the limit
        body = self.error(self.call(session, {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}),
                          502, "usage_exceeds_reservation")
        self.assertNotIn("choices", body)
        hold = body["charged_tokens"]
        self.assertEqual(hold, len('{"messages":[{"role":"user","content":"hi"}]}'.encode()) + 64 + 100)
        self.assertEqual(body["reported_tokens"], 50_020)
        state = self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))
        self.assertEqual((state["used_tokens"], state["overage_calls"], state["overage_tokens"]),
                         (hold, 1, 50_020 - hold))
        self.completion_tokens = 5
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x"}]}), 409, "session_overage_locked")
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"}, self.user))
        self.assertEqual((closed["status"], closed["credits_charged"], closed["overage_calls"]),
                         ("closed", -(-hold * 10 // 1000), 1))
        wallet = self.wallet(self.user)
        self.assertEqual((wallet["balance"], wallet["reserved"]), (100 - closed["credits_charged"], 0))
        overages = self.ok(self.get("/admin/overages", self.operator()))["overages"]
        self.assertEqual(len(overages), 1)
        self.assertEqual((overages[0]["session_id"], overages[0]["charged_tokens"], overages[0]["overage_tokens"]),
                         (session["session_id"], hold, 50_020 - hold))
        self.assertGreater(overages[0]["overage_credits"], 0)
        usage = {u["status"] for u in self.ok(self.get("/usage", self.user))["usage"]}
        self.assertIn("overage", usage)


class NestedParamsTest(SessionParamsTest):
    """Rereview N-09: nested structures (tool_choice, tools[].function, tool_calls, content
    parts, response formats) are strictly allow-listed, not forwarded verbatim."""

    def attack(self, session, body, path="chat/completions", code="parameter_not_allowed"):
        response = self.call(session, body, path=path)
        self.assertEqual(response.status_code, 400, (body, response.text))
        self.assertEqual(response.json()["code"], code, body)
        return response.json()

    def test_nested_unknown_keys_are_refused_and_never_reach_the_upstream(self):
        session = self.open()
        msg = [{"role": "user", "content": "hi"}]
        fn = {"name": "move", "parameters": {"type": "object"}}
        tool = {"type": "function", "function": fn}
        call = {"id": "c1", "type": "function", "function": {"name": "move", "arguments": "{}"}}
        # The rereview's example: a limit smuggled inside tool_choice.
        body = self.attack(session, {"messages": msg, "tools": [tool],
                                     "tool_choice": {"type": "function", "function": {"name": "move"},
                                                     "max_completion_tokens": 100000}})
        self.assertEqual(body["parameters"], ["tool_choice.max_completion_tokens"])
        chat_attacks = [
            {"tools": [tool], "tool_choice": {"type": "function", "function": {"name": "move", "n": 9}}},
            {"tools": [{**tool, "max_tokens": 1}]},
            {"tools": [{"type": "function", "function": {**fn, "max_completion_tokens": 10 ** 6}}]},
            {"tools": [{"type": "function", "function": {**fn, "cache_control": {"type": "ephemeral"}}}]},
            {"response_format": {"type": "json_object", "max_tokens": 10 ** 6}},
            {"response_format": {"type": "json_schema", "json_schema": {"name": "s", "schema": {}, "n": 3}}},
            {"response_format": {"type": "json_schema", "json_schema": {"name": "s"}, "max_tokens": 10 ** 6}},
        ]
        for extra in chat_attacks:
            self.attack(session, {"messages": msg, **extra})
        message_attacks = [
            [{"role": "assistant", "content": None, "tool_calls": [{**call, "max_tokens": 5}]}],
            [{"role": "assistant", "content": None,
              "tool_calls": [{**call, "function": {**call["function"], "n": 50}}]}],
            [{"role": "user", "content": [{"type": "text", "text": "hi", "image_url": {"url": "https://x"}}]}],
            [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
        ]
        for messages in message_attacks:
            self.attack(session, {"messages": messages})
        responses_attacks = [
            {"tools": [{"type": "function", "name": "move", "parameters": {}, "max_output_tokens": 10 ** 6}]},
            {"tools": [{"type": "function", "name": "move"}], "tool_choice": {"type": "function", "name": "move",
                                                                              "max_output_tokens": 5}},
            {"text": {"format": {"type": "text"}, "max_output_tokens": 9}},
            {"text": {"format": {"type": "json_schema", "name": "s", "schema": {}, "store": True}}},
            {"input": [{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "hi", "file_id": "f"}]}]},
        ]
        for extra in responses_attacks:
            self.attack(session, {"input": "hi", **extra}, path="responses")
        # Wrong types in nested fields.
        invalid = [
            ({"messages": msg, "tools": [tool], "tool_choice": {"type": "function", "function": {"name": 7}}},
             "chat/completions"),
            ({"messages": [{"role": "assistant", "content": None,
                            "tool_calls": [{**call, "function": {"name": "move", "arguments": {"x": 1}}}]}]},
             "chat/completions"),
            ({"messages": [{"role": "assistant", "content": None, "tool_calls": [{"type": "function",
                                                                                 "function": call["function"]}]}]},
             "chat/completions"),
            ({"input": [{"type": "function_call", "call_id": "c", "name": "bad name!", "arguments": "{}"}]},
             "responses"),
            ({"input": [{"role": "assistant", "content": [{"type": "output_text", "text": "x",
                                                           "annotations": [{"type": "url_citation"}]}]}]},
             "responses"),
            ({"input": "hi", "text": {"verbosity": "max"}}, "responses"),
        ]
        for body, path in invalid:
            self.attack(session, body, path=path, code="invalid_parameter")
        self.assertEqual(self.seen, [])

    def test_well_formed_nested_structures_still_pass(self):
        session = self.open(max_tokens=5000)
        tool = {"type": "function", "function": {"name": "move", "description": "drive",
                                                 "parameters": {"type": "object", "properties": {}}, "strict": True}}
        history = [{"role": "user", "content": [{"type": "text", "text": "go"}]},
                   {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "move", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
        self.ok(self.call(session, {"messages": history, "tools": [tool],
                                    "tool_choice": {"type": "function", "function": {"name": "move"}},
                                    "response_format": {"type": "json_schema",
                                                        "json_schema": {"name": "out", "schema": {"type": "object"},
                                                                        "strict": True}}}))
        self.assertEqual(self.seen[-1][1]["tool_choice"], {"type": "function", "function": {"name": "move"}})
        self.ok(self.call(session, {"input": [
            {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c1", "name": "move", "arguments": "{}", "status": "completed"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "done", "annotations": []}]}],
            "tools": [{"type": "function", "name": "move", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "move"},
            "text": {"format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}},
                     "verbosity": "low"}}, path="responses"))
        self.assertEqual(len(self.seen), 2)
