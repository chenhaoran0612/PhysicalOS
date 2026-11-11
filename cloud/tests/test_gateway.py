"""Inference gateway: infer flow, upstream failures, entitlement gate, metered sessions.

Upstreams here are test doubles: the Echo provider and a local fake
OpenAI-compatible HTTP server. No real model is called.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase

from neuxmind_cloud.upstream import OpenAICompatibleProvider, UpstreamRejected, UpstreamUnknown


def echo_card(model_id: str, behavior: str = "ok", **extra) -> dict:
    return {**ECHO_LLM, "id": model_id, "upstream": {"provider": "echo", "behavior": behavior}, **extra}


class GatewayInferTest(CloudTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@example.com")
        self.publish(ECHO_LLM)
        self.entitle(self.user, "echo-llm")
        self.fund(self.user, 1000)

    def infer(self, body: dict, user=None, **kwargs):
        return self.post("/gateway/infer", body, user or self.user, **kwargs)

    def test_full_flow_reserve_call_settle(self):
        out = self.ok(self.infer({"model": "echo-llm", "input": "hello robot world", "max_tokens": 100,
                                  "request_id": "r1"}))
        self.assertTrue(out["simulated"])
        self.assertEqual(out["output"]["text"], "echo: hello robot world")
        # Echo usage: 3 prompt words + 4 output words = 7 tokens; 10 credits / 1k → ceil(0.07) = 1.
        self.assertEqual(out["usage"]["total_tokens"], 7)
        self.assertEqual(out["credits_charged"], 1)
        # Reservation covered max_tokens + input bytes + overhead: (100 + 17 + 64) * 10 / 1000 → 2.
        self.assertEqual(out["credits_reserved"], 2)
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits", "balance": 999,
                                                  "reserved": 0, "available": 999})
        usage = self.ok(self.get("/usage", self.user))
        self.assertEqual(usage["usage"][0]["status"], "succeeded")
        self.assertEqual(usage["usage"][0]["upstream_ref"], "echo:1")
        self.assertTrue(usage["usage"][0]["simulated"])
        reservation = self.ok(self.get("/wallet/reservations", self.user))["reservations"][0]
        self.assertEqual((reservation["status"], reservation["settled_amount"], reservation["evidence_ref"]),
                         ("settled", 1, "echo:1"))

    def test_request_id_idempotency(self):
        body = {"model": "echo-llm", "input": "same", "max_tokens": 10, "request_id": "dup"}
        first = self.ok(self.infer(body))
        second = self.ok(self.infer(body))
        self.assertTrue(second["replayed"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.wallet(self.user)["balance"], 999)
        self.error(self.infer({**body, "input": "different"}), 409, "request_id_reused")

    def test_entitlement_gate(self):
        self.publish(echo_card("locked-llm"))
        self.error(self.infer({"model": "locked-llm", "input": "x", "max_tokens": 10}), 403, "entitlement_required")
        self.error(self.infer({"model": "nope", "input": "x", "max_tokens": 10}), 404, "model_not_found")
        self.assertEqual(self.wallet(self.user)["reserved"], 0)

    def test_unlisted_model_refused(self):
        self.ok(self.post("/admin/models/echo-llm/status", {"status": "unlisted"}, self.operator()))
        self.error(self.infer({"model": "echo-llm", "input": "x", "max_tokens": 10}), 404, "model_not_found")

    def test_insufficient_credits_refused_before_call(self):
        poor = self.register("poor@example.com")
        self.entitle(poor, "echo-llm")
        self.error(self.infer({"model": "echo-llm", "input": "x", "max_tokens": 10}, poor), 402, "insufficient_credits")
        self.assertEqual(self.ok(self.get("/usage", poor))["usage"], [])

    def test_api_key_scope(self):
        key = self.api_key(self.user, ["gateway", "usage:read"])
        self.ok(self.infer({"model": "echo-llm", "input": "via key", "max_tokens": 10}, headers=key["headers"]))
        nokey = self.api_key(self.user, ["catalog:read"])
        self.error(self.infer({"model": "echo-llm", "input": "x", "max_tokens": 10}, headers=nokey["headers"]), 403,
                   "scope_missing")

    def test_validation(self):
        self.error(self.infer({"model": "echo-llm", "input": "x", "max_tokens": 0}), 400)
        self.error(self.infer({"model": "echo-llm", "input": "x", "max_tokens": True}), 400)
        self.error(self.infer({"model": "echo-llm", "input": [{"role": "robot", "content": "x"}], "max_tokens": 5}), 400)

    def test_upstream_timeout_keeps_reservation(self):
        self.publish(echo_card("slow-llm", "timeout"))
        self.entitle(self.user, "slow-llm")
        body = self.error(self.infer({"model": "slow-llm", "input": "x", "max_tokens": 1000, "request_id": "t1"}), 502,
                          "upstream_unknown")
        wallet = self.wallet(self.user)
        self.assertEqual(wallet["balance"], 1000)
        self.assertGreater(wallet["reserved"], 0)
        self.assertEqual(wallet["available"], 1000 - wallet["reserved"])
        held = self.ok(self.get("/admin/reservations", self.operator()))["reservations"]
        self.assertEqual([r["id"] for r in held], [body["reservation_id"]])
        # Not replayed automatically.
        self.error(self.infer({"model": "slow-llm", "input": "x", "max_tokens": 1000, "request_id": "t1"}), 409,
                   "request_not_replayable")
        # Operator reconciles: provider confirms nothing was billed.
        self.ok(self.post(f"/admin/reservations/{body['reservation_id']}/resolve",
                          {"action": "release", "reason": "provider log shows no completion"}, self.operator()))
        self.assertEqual(self.wallet(self.user)["reserved"], 0)

    def test_upstream_error_releases(self):
        self.publish(echo_card("broken-llm", "error"))
        self.entitle(self.user, "broken-llm")
        self.error(self.infer({"model": "broken-llm", "input": "x", "max_tokens": 50}), 502, "upstream_error")
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits", "balance": 1000,
                                                  "reserved": 0, "available": 1000})
        self.assertEqual(self.ok(self.get("/usage", self.user))["usage"][0]["status"], "failed")

    def test_missing_usage_goes_to_reconciliation(self):
        self.publish(echo_card("nousage-llm", "no_usage"))
        self.entitle(self.user, "nousage-llm")
        self.error(self.infer({"model": "nousage-llm", "input": "x", "max_tokens": 50}), 502, "upstream_unknown")
        wallet = self.wallet(self.user)
        self.assertEqual(wallet["balance"], 1000)  # nothing auto-charged
        statuses = {r["model_id"]: r["status"] for r in self.ok(self.get("/wallet/reservations", self.user))["reservations"]}
        self.assertEqual(statuses, {"nousage-llm": "reconciliation_required"})

    def test_overuse_charges_the_cap_withholds_output_and_records_excess(self):
        """Iteration-01 fix (replaces ADR-007's "overage is not charged"): the user
        pays exactly the reservation, gets no output, the excess waits for an operator."""
        self.publish(echo_card("greedy-llm", "overuse"))
        self.entitle(self.user, "greedy-llm")
        body = self.error(self.infer({"model": "greedy-llm", "input": "x", "max_tokens": 50, "request_id": "g1"}),
                          502, "usage_exceeds_reservation")
        self.assertNotIn("output", body)
        reserved = catalog_cost = 2   # bound 50 + 1 byte + 64 = 115 tokens; ceil(115 * 10 / 1000) = 2
        self.assertEqual(body["credits_charged"], reserved)
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits",
                                                  "balance": 1000 - catalog_cost, "reserved": 0,
                                                  "available": 1000 - catalog_cost})
        reservation = self.ok(self.get("/wallet/reservations", self.user))["reservations"][0]
        self.assertEqual((reservation["status"], reservation["settled_amount"]), ("settled", reserved))
        usage = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertEqual((usage["status"], usage["credits"]), ("overage", reserved))
        overages = self.ok(self.get("/admin/overages", self.operator()))["overages"]
        self.assertEqual([o["id"] for o in overages], [body["overage_id"]])
        self.assertEqual(overages[0]["charged_tokens"], 115)
        self.assertGreater(overages[0]["overage_tokens"], 1_000_000)
        # Not replayable; the operator may waive (or charge) the excess explicitly.
        self.error(self.infer({"model": "greedy-llm", "input": "x", "max_tokens": 50, "request_id": "g1"}), 409,
                   "request_not_replayable")
        self.error(self.post(f"/admin/overages/{body['overage_id']}/resolve", {"action": "charge", "credits": 10 ** 12,
                                                                               "reason": "x"}, self.operator()), 400)
        charged = self.ok(self.post(f"/admin/overages/{body['overage_id']}/resolve",
                                    {"action": "charge", "credits": 5, "reason": "provider invoice #1"}, self.operator()))
        self.assertEqual(charged["status"], "charged")
        self.assertEqual(self.wallet(self.user)["balance"], 1000 - catalog_cost - 5)
        self.error(self.post(f"/admin/overages/{body['overage_id']}/resolve", {"action": "waive", "reason": "x"},
                             self.operator()), 409, "overage_resolved")

    def test_per_call_and_free_pricing(self):
        self.publish(echo_card("call-llm", pricing={"type": "per_call", "credits": 7}))
        self.publish(echo_card("free-llm", pricing={"type": "free"}))
        self.entitle(self.user, "call-llm")
        self.entitle(self.user, "free-llm")
        self.assertEqual(self.ok(self.infer({"model": "call-llm", "input": "x", "max_tokens": 5}))["credits_charged"], 7)
        self.assertEqual(self.ok(self.infer({"model": "free-llm", "input": "x", "max_tokens": 5}))["credits_charged"], 0)
        self.assertEqual(self.wallet(self.user)["balance"], 993)


class EchoInProdTest(CloudTestCase):
    env = "prod"

    def test_echo_upstream_refused_in_prod(self):
        user = self.register("p@example.com")
        # Refused at publish time (review C-09) ...
        self.error(self.post("/admin/models", ECHO_LLM, self.operator()), 400, "upstream_not_allowed")
        # ... and, defence in depth, at call time for a card that got into the DB anyway.
        from neuxmind_cloud.models import ModelCard
        with self.db.transaction() as s:
            s.add(ModelCard(id="echo-llm", name="Echo", source="closed", kind="llm", license="t",
                            pricing={"type": "per_1k_tokens", "credits": 10}, upstream={"provider": "echo"},
                            requires_capabilities=[], families=[], status="published"))
        self.entitle(user, "echo-llm")
        self.fund(user, 100)
        self.error(self.post("/gateway/infer", {"model": "echo-llm", "input": "x", "max_tokens": 5}, user), 502,
                   "upstream_error")
        self.assertEqual(self.wallet(user)["reserved"], 0)
        self.assertEqual(self.wallet(user)["balance"], 100)


class GatewaySessionTest(CloudTestCase):
    """Compatibility with the desktop /v1/agent-sessions protocol semantics."""

    def setUp(self):
        super().setUp()
        self.user = self.register("desk@example.com")
        self.publish(ECHO_LLM)
        self.entitle(self.user, "echo-llm")
        self.fund(self.user, 1000)
        self.key = self.api_key(self.user, ["gateway"])

    def open(self, request_id="sess-1", max_tokens=2000, **extra):
        return self.post("/gateway/sessions", {"request_id": request_id, "model": "echo-llm", "max_tokens": max_tokens,
                                               "max_units": -(-max_tokens // 1000), "max_minutes": 15, **extra},
                         headers=self.key["headers"])

    def call(self, session, body, path="chat/completions"):
        return self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/{path}", json=body,
                                headers={"Authorization": f"Bearer {session['access_token']}"})

    def test_open_call_close_settles(self):
        session = self.ok(self.open(), 201)
        self.assertTrue(session["access_token"].startswith("nxgw_"))
        self.assertTrue(session["base_url"].endswith(f"/api/v1/gateway/sessions/{session['session_id']}/v1"))
        self.assertEqual(session["reserved_credits"], 20)  # 2000 tokens * 10 / 1000
        self.assertEqual(self.wallet(self.user)["reserved"], 20)
        reply = self.ok(self.call(session, {"model": "ignored", "messages": [{"role": "user", "content": "hi there"}]}))
        self.assertTrue(reply["simulated"])
        self.assertEqual(reply["neuxmind"]["used_tokens"], reply["usage"]["total_tokens"])
        self.ok(self.call(session, {"input": "status report"}, path="responses"))
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "close-1"},
                                   headers=self.key["headers"]))
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["metering_status"], "settled")
        self.assertEqual(closed["usage_units"], 1)
        self.assertTrue(closed["usage_evidence_ref"].startswith("gateway_session:"))
        self.assertEqual(self.wallet(self.user), {"org_id": self.user["org_id"], "unit": "credits",
                                                  "balance": 1000 - closed["credits_charged"], "reserved": 0,
                                                  "available": 1000 - closed["credits_charged"]})
        # Idempotent close; token no longer usable.
        again = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "close-1"},
                                  headers=self.key["headers"]))
        self.assertTrue(again["replayed"])
        self.assertEqual(self.wallet(self.user)["balance"], 1000 - closed["credits_charged"])
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x"}]}), 409, "session_closed")

    def test_open_is_idempotent_and_token_not_reissued(self):
        first = self.ok(self.open(), 201)
        replay = self.ok(self.open(), 201)
        self.assertEqual(replay["session_id"], first["session_id"])
        self.assertIsNone(replay["access_token"])
        self.assertEqual(self.wallet(self.user)["reserved"], 20)
        self.error(self.open(max_tokens=3000), 409, "request_id_reused")

    def test_budget_is_a_hard_ceiling(self):
        session = self.ok(self.open(request_id="tiny", max_tokens=80), 201)
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x" * 100}]}), 402,
                   "session_budget_exhausted")
        # Iteration 02 (§4.3): streaming is allowed, but the same budget ceiling applies before
        # anything is sent upstream; a non-boolean ``stream`` is refused.
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x" * 100}], "stream": True}), 402,
                   "session_budget_exhausted")
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x"}], "stream": "yes"}), 400,
                   "invalid_parameter")
        self.error(self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/chat/completions",
                                    json={"messages": [{"role": "user", "content": "x"}]},
                                    headers={"Authorization": "Bearer nxgw_forged"}), 401, "invalid_session_token")

    def test_unknown_call_forces_reconciliation(self):
        self.publish(echo_card("slow-llm", "timeout"))
        self.entitle(self.user, "slow-llm")
        session = self.ok(self.post("/gateway/sessions", {"request_id": "s-slow", "model": "slow-llm", "max_tokens": 1000},
                                    headers=self.key["headers"]), 201)
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x"}]}), 502, "upstream_unknown")
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"},
                                   headers=self.key["headers"]))
        self.assertEqual(closed["status"], "reconciliation_required")
        self.assertIsNone(closed["credits_charged"])
        self.assertEqual(self.wallet(self.user)["reserved"], 10)

    def test_expired_session_rejects_calls(self):
        session = self.ok(self.open(), 201)
        from neuxmind_cloud.models import GatewaySession
        with self.db.transaction() as s:
            s.get(GatewaySession, session["session_id"]).expires_at = time.time() - 1
        self.error(self.call(session, {"messages": [{"role": "user", "content": "x"}]}), 403, "session_expired")
        closed = self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": "c"},
                                   headers=self.key["headers"]))
        self.assertEqual((closed["status"], closed["credits_charged"]), ("closed", 0))
        self.assertEqual(self.wallet(self.user)["reserved"], 0)


# ------------------------------------------------- OpenAI-compatible provider
class _FakeOpenAI(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers.get("Authorization"), body))
        if self.mode == "sleep":
            time.sleep(2)
        if self.mode.startswith("status:"):
            self.send_response(int(self.mode[7:]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/responses"):
            data = {"id": "resp_1", "output_text": "fake answer", "usage": {"input_tokens": 11, "output_tokens": 5}}
        else:
            data = {"id": "chatcmpl_1", "choices": [{"message": {"content": "fake answer"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}
        if self.mode == "no_usage":
            data.pop("usage")
        raw = json.dumps(data).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except BrokenPipeError:  # client already timed out (the "sleep" mode)
            pass


class OpenAICompatibleProviderTest(CloudTestCase):
    """Protocol test against a local fake server (not a real model provider)."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        _FakeOpenAI.mode, _FakeOpenAI.seen = "ok", []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_chat_and_responses_parse_usage_and_send_operator_credential(self):
        import os
        os.environ["NX_TEST_UPSTREAM_KEY"] = "sk-operator-only"
        try:
            chat = OpenAICompatibleProvider(self.base, "gpt-test", credential_ref="NX_TEST_UPSTREAM_KEY", timeout=5)
            result = chat.complete([{"role": "user", "content": "hi"}], 20)
            self.assertEqual((result.text, result.total_tokens, result.upstream_ref),
                             ("fake answer", 16, "openai_compatible:chatcmpl_1"))
            path, auth, body = _FakeOpenAI.seen[-1]
            self.assertEqual((path, auth, body["max_tokens"], body["model"]),
                             ("/v1/chat/completions", "Bearer sk-operator-only", 20, "gpt-test"))
            responses = OpenAICompatibleProvider(self.base, "gpt-test", api="responses", timeout=5)
            self.assertEqual(responses.complete([{"role": "user", "content": "hi"}], 20).total_tokens, 16)
        finally:
            os.environ.pop("NX_TEST_UPSTREAM_KEY")
        with self.assertRaises(UpstreamRejected):
            OpenAICompatibleProvider(self.base, "m", credential_ref="NX_MISSING_KEY").complete([], 1)

    def test_error_classification(self):
        """Review C-04: 4xx (except 408) = rejected/no charge; 408 and 5xx = unknown (may have been billed)."""
        provider = OpenAICompatibleProvider(self.base, "m", timeout=0.5)
        for status, expected in ((400, UpstreamRejected), (401, UpstreamRejected), (429, UpstreamRejected),
                                 (408, UpstreamUnknown), (500, UpstreamUnknown), (502, UpstreamUnknown),
                                 (504, UpstreamUnknown)):
            _FakeOpenAI.mode = f"status:{status}"
            with self.assertRaises(expected, msg=f"HTTP {status}"):
                provider.complete([{"role": "user", "content": "x"}], 5)
        # Errors before the request leaves are rejected (nothing can have been billed).
        import socket
        import ssl
        from neuxmind_cloud.upstream import classify_connection_error
        self.assertIsInstance(classify_connection_error(socket.gaierror(-2, "Name or service not known")), UpstreamRejected)
        self.assertIsInstance(classify_connection_error(ssl.SSLCertVerificationError("bad cert")), UpstreamRejected)
        self.assertIsInstance(classify_connection_error(ConnectionResetError()), UpstreamUnknown)
        _FakeOpenAI.mode = "no_usage"
        with self.assertRaises(UpstreamUnknown):
            provider.complete([{"role": "user", "content": "x"}], 5)
        _FakeOpenAI.mode = "sleep"
        with self.assertRaises(UpstreamUnknown):
            provider.complete([{"role": "user", "content": "x"}], 5)
        with self.assertRaises(UpstreamRejected):
            OpenAICompatibleProvider("http://127.0.0.1:1/v1", "m", timeout=1).complete([], 1)

    def test_gateway_end_to_end_with_http_upstream(self):
        user = self.register("http@example.com")
        self.publish({"id": "remote-llm", "name": "Remote", "source": "closed", "kind": "llm", "license": "api",
                      "pricing": {"type": "per_1k_tokens", "credits": 100},
                      "upstream": {"provider": "openai_compatible", "base_url": self.base, "model": "gpt-test"}})
        self.entitle(user, "remote-llm")
        self.fund(user, 100)
        out = self.ok(self.post("/gateway/infer", {"model": "remote-llm", "input": "hello", "max_tokens": 20}, user))
        self.assertFalse(out["simulated"])
        self.assertEqual((out["usage"]["total_tokens"], out["credits_charged"]), (16, 2))
        self.assertEqual(self.wallet(user)["balance"], 98)
        public = self.ok(self.get("/catalog/models/remote-llm", user))
        self.assertNotIn("upstream", public)
        self.assertNotIn(self.base, json.dumps(public))
