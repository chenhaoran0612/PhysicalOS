"""Contract §4.3: streamed session calls (SSE), metering, truncation, interruption,
client disconnect, and rereview N-05 (one call in flight per session).

Upstreams are test doubles: the Echo provider's streaming mode, a scripted
stream with gates (to hold a stream open at a chosen point), and a local fake
OpenAI-compatible HTTP server that speaks SSE. No real model is called.
Tests that need a real socket (disconnect, concurrency) run the app on uvicorn.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import support  # noqa: F401
from support import API, ECHO_LLM, CloudTestCase, LiveServer, sse_events

from neuxmind_cloud.upstream import UpstreamResult, UpstreamStream, UpstreamUnknown


def echo_card(model_id: str, behavior: str = "ok", **upstream) -> dict:
    return {**ECHO_LLM, "id": model_id, "upstream": {"provider": "echo", "behavior": behavior, **upstream}}


MSG = [{"role": "user", "content": "hello robot world"}]


class _Base(CloudTestCase):
    def open(self, model="echo-llm", max_tokens=1000, request_id=None, user=None):
        self._n = getattr(self, "_n", 0) + 1
        return self.ok(self.post("/gateway/sessions", {"request_id": request_id or f"s{self._n}", "model": model,
                                                       "max_tokens": max_tokens}, user or self.user), 201)

    def url(self, session, path="chat/completions"):
        return f"{API}/gateway/sessions/{session['session_id']}/v1/{path}"

    def auth(self, session):
        return {"Authorization": f"Bearer {session['access_token']}"}

    def stream(self, session, body, path="chat/completions"):
        response = self.client.post(self.url(session, path), json={**body, "stream": True}, headers=self.auth(session))
        return response, sse_events(response.text)

    def state(self, session):
        return self.ok(self.get(f"/gateway/sessions/{session['session_id']}", self.user))

    def close(self, session, request_id="close"):
        return self.ok(self.post(f"/gateway/sessions/{session['session_id']}/close", {"request_id": request_id},
                                 self.user))

    def terminal(self, events):
        name, data = events[-1]
        return name, json.loads(data)


# ------------------------------------------------------------- Echo streaming
class EchoStreamTest(_Base):
    def setUp(self):
        super().setUp()
        self.user = self.register("stream@example.com")
        self.publish(ECHO_LLM)
        for model, behavior in (("over-llm", "overuse"), ("cut-llm", "stream_cut"), ("nousage-llm", "no_usage"),
                                ("overrun-llm", "usage_overrun"), ("down-llm", "timeout"), ("bad-llm", "error")):
            self.publish(echo_card(model, behavior))
            self.entitle(self.user, model)
        self.entitle(self.user, "echo-llm")
        self.fund(self.user, 1000)

    def test_chat_stream_passes_events_through_and_settles_on_upstream_usage(self):
        session = self.open()
        response, events = self.stream(session, {"messages": MSG})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["x-neuxmind-simulated"], "true")
        chunks = [json.loads(d) for n, d in events[:-1] if d != "[DONE]"]
        self.assertTrue(all(c["simulated"] for c in chunks))           # the test double says so on every event
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
        self.assertEqual(text, "echo: hello robot world")
        self.assertIn((None, "[DONE]"), events)
        self.assertEqual(chunks[-1]["usage"], {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
        name, metering = self.terminal(events)
        self.assertEqual(name, "neuxmind.metering")
        self.assertEqual((metering["status"], metering["call_tokens"], metering["used_tokens"], metering["simulated"]),
                         ("succeeded", 7, 7, True))
        self.assertEqual(metering["output_lower_bound"], 4)
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["inflight_tokens"], state["calls"]), (7, 0, 1))
        closed = self.close(session)
        self.assertEqual((closed["status"], closed["credits_charged"]), ("closed", 1))
        usage = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertEqual((usage["status"], usage["upstream_ref"], usage["simulated"]), ("succeeded", "echo:stream-1", True))

    def test_responses_stream(self):
        session = self.open()
        response, events = self.stream(session, {"input": "status report"}, path="responses")
        self.assertEqual(response.status_code, 200)
        kinds = [n for n, _ in events]
        self.assertEqual(kinds[0], "response.created")
        self.assertIn("response.completed", kinds)
        self.assertEqual(kinds.count("response.output_text.delta"), 3)
        name, metering = self.terminal(events)
        self.assertEqual((name, metering["status"], metering["call_tokens"]), ("neuxmind.metering", "succeeded", 5))
        self.assertEqual(self.state(session)["used_tokens"], 5)

    def test_truncation_when_the_upstream_ignores_the_output_limit(self):
        session = self.open(model="over-llm")
        response, events = self.stream(session, {"messages": MSG, "max_tokens": 5})
        self.assertEqual(response.status_code, 200)
        deltas = [d for n, d in events if n is None and d != "[DONE]" and json.loads(d)["choices"]
                  and json.loads(d)["choices"][0]["delta"].get("content")]
        self.assertEqual(len(deltas), 5)                    # exactly the output limit, never more
        self.assertNotIn((None, "[DONE]"), events)
        name, err = self.terminal(events)
        self.assertEqual((name, err["code"], err["error"]["code"]), ("error", "output_limit_reached", "output_limit_reached"))
        metering = err["neuxmind"]
        self.assertEqual(metering["output_limit"], 5)
        self.assertEqual((metering["status"], metering["call_tokens"]), ("reconciliation_required", metering["hold_tokens"]))
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["unknown_calls"], state["inflight_tokens"]),
                         (metering["hold_tokens"], 1, 0))
        closed = self.close(session)
        self.assertEqual((closed["status"], closed["credits_charged"]), ("reconciliation_required", None))
        self.assertEqual(self.wallet(self.user)["reserved"], 10)   # the hold stays for the operator (ADR-012)

    def test_upstream_cut_before_usage_counts_the_hold_and_goes_to_reconciliation(self):
        session = self.open(model="cut-llm")
        response, events = self.stream(session, {"messages": MSG})
        self.assertEqual(response.status_code, 200)
        name, err = self.terminal(events)
        self.assertEqual((name, err["code"]), ("error", "upstream_interrupted"))
        hold = err["neuxmind"]["hold_tokens"]
        self.assertEqual(self.state(session)["used_tokens"], hold)
        # The remaining budget shrank by the hold: the next call cannot use it again.
        response, events = self.stream(session, {"messages": MSG})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.close(session)["status"], "reconciliation_required")
        usage = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertEqual(usage["status"], "reconciliation_required")

    def test_usage_missing(self):
        for path, body in (("chat/completions", {"messages": MSG}), ("responses", {"input": "hi"})):
            session = self.open(model="nousage-llm")
            response, events = self.stream(session, body, path=path)
            name, err = self.terminal(events)
            self.assertEqual((name, err["code"]), ("error", "usage_missing"), path)
            self.assertEqual(self.state(session)["used_tokens"], err["neuxmind"]["hold_tokens"])
            self.assertEqual(self.state(session)["unknown_calls"], 1)

    def test_usage_above_the_hold_charges_the_hold_records_overage_and_locks(self):
        session = self.open(model="overrun-llm")
        response, events = self.stream(session, {"messages": MSG})
        name, err = self.terminal(events)
        self.assertEqual((name, err["code"]), ("error", "usage_exceeds_reservation"))
        hold = err["charged_tokens"]
        self.assertEqual(err["reported_tokens"], 3 + 4000)
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["overage_calls"], state["overage_tokens"]), (hold, 1, 4003 - hold))
        response = self.client.post(self.url(session), json={"messages": MSG, "stream": True}, headers=self.auth(session))
        self.error(response, 409, "session_overage_locked")
        closed = self.close(session)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["credits_charged"], -(-hold * 10 // 1000))       # never more than the hold
        overages = self.ok(self.get("/admin/overages", self.operator()))["overages"]
        self.assertEqual([(o["session_id"], o["charged_tokens"]) for o in overages], [(session["session_id"], hold)])

    def test_failure_before_the_first_byte_is_a_plain_json_error(self):
        session = self.open(model="bad-llm")
        response = self.client.post(self.url(session), json={"messages": MSG, "stream": True}, headers=self.auth(session))
        self.error(response, 502, "upstream_error")
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["inflight_tokens"], state["unknown_calls"]), (0, 0, 0))
        session = self.open(model="down-llm")
        response = self.client.post(self.url(session), json={"messages": MSG, "stream": True}, headers=self.auth(session))
        self.error(response, 502, "upstream_unknown")
        self.assertEqual(self.state(session)["unknown_calls"], 1)

    def test_stream_parameters_still_allow_listed(self):
        session = self.open()
        for extra in ({"stream_options": {"include_usage": False}}, {"stream": "true"}, {"n": 2}):
            response = self.client.post(self.url(session), json={"messages": MSG, "stream": True, **extra},
                                        headers=self.auth(session))
            self.assertEqual(response.status_code, 400, (extra, response.text))
        self.assertEqual(self.state(session)["inflight_tokens"], 0)


class StreamDeadlineTest(_Base):
    settings_overrides = {"stream_max_seconds": 1}

    def test_stream_longer_than_the_limit_is_ended_and_counted_at_the_hold(self):
        self.user = self.register("slow@example.com")
        self.publish(echo_card("slow-llm", "ok", stream_delay=0.4))
        self.entitle(self.user, "slow-llm")
        self.fund(self.user, 100)
        session = self.open(model="slow-llm")
        started = time.monotonic()
        response, events = self.stream(session, {"messages": MSG})
        self.assertLess(time.monotonic() - started, 3)
        name, err = self.terminal(events)
        self.assertEqual((name, err["code"]), ("error", "stream_duration_exceeded"))
        self.assertEqual(self.state(session)["used_tokens"], err["neuxmind"]["hold_tokens"])

    def test_derived_limit_ends_before_the_sweeper_could_declare_the_call_lost(self):
        s = self.make_settings(stream_max_seconds=0)
        self.assertLess(s.effective_stream_max_seconds + s.upstream_timeout_seconds, s.stale_call_seconds)
        self.assertEqual(self.make_settings(stream_max_seconds=10_000).effective_stream_max_seconds,
                         s.effective_stream_max_seconds)


# ------------------------------------------------------------- scripted upstream
def chat_sse(deltas, prompt=20, completion=None, done=True, rid="up-1") -> list[bytes]:
    lines: list[bytes] = []

    def ev(obj):
        lines.extend([f"data: {json.dumps(obj)}\n".encode(), b"\n"])

    ev({"id": rid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    for d in deltas:
        ev({"id": rid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": d}}]})
    if completion is not None:
        ev({"id": rid, "object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}})
    if done:
        lines.extend([b"data: [DONE]\n", b"\n"])
    return lines


class Script:
    def __init__(self, lines, gate_at=None, cut_at=None):
        self.lines, self.gate_at, self.cut_at = lines, gate_at, cut_at
        self.gate, self.reached = threading.Event(), threading.Event()
        self.closed = False


class ScriptedStream(UpstreamStream):
    simulated = False

    def __init__(self, script: Script):
        self.script = script

    def lines(self):
        for i, line in enumerate(self.script.lines):
            if i == self.script.gate_at:
                self.script.reached.set()
                self.script.gate.wait(15)
            if i == self.script.cut_at:
                raise UpstreamUnknown("scripted cut", "scripted cut")
            yield line

    def close(self):
        self.script.closed = True


class ScriptedProvider:
    """Streams ``owner.script``; non-streamed calls block on ``owner.call_gate``
    and report ``owner.reported`` tokens (default: more than any hold)."""

    simulated = False

    def __init__(self, owner):
        self.owner = owner

    def open_stream(self, path, body):
        self.owner.stream_bodies.append(body)
        return ScriptedStream(self.owner.script)

    def passthrough(self, path, body):
        with self.owner.lock:
            self.owner.entered += 1
            n = self.owner.entered
        self.owner.call_gate.wait(15)
        reported = self.owner.reported
        return ({"id": f"call-{n}", "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": reported - 10}},
                UpstreamResult("", 10, reported - 10, reported, f"openai_compatible:call-{n}"))


class LiveStreamTest(_Base):
    """Real uvicorn server: disconnects, concurrency, no lock held while streaming."""

    def provider_factory(self):
        self.stream_bodies, self.lock, self.entered = [], threading.Lock(), 0
        self.call_gate = threading.Event()
        self.reported = 50_000
        self.script = Script(chat_sse(["a"], completion=1))
        return lambda upstream: ScriptedProvider(self)

    def setUp(self):
        super().setUp()
        self.user = self.register("live@example.com")
        self.publish({**ECHO_LLM, "id": "scripted-llm"})
        self.entitle(self.user, "scripted-llm")
        self.fund(self.user, 1000)
        self.live = LiveServer(self.app)
        self.http = httpx.Client(base_url=self.live.url, trust_env=False, timeout=20)

    def tearDown(self):
        self.call_gate.set()
        self.script.gate.set()
        self.http.close()
        self.live.stop()
        super().tearDown()

    def open(self, **kw):
        return super().open(model="scripted-llm", **kw)

    def wait_idle(self, session, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state(session)
            if state["inflight_tokens"] == 0:
                return state
            time.sleep(0.05)
        self.fail(f"session still busy: {state}")

    def test_stream_body_sent_upstream(self):
        session = self.open()
        with self.http.stream("POST", self.url(session), json={"messages": MSG, "stream": True},
                              headers=self.auth(session)) as r:
            body = r.read().decode()
        name, metering = sse_events(body)[-1]
        metering = json.loads(metering)
        self.assertEqual(name, "neuxmind.metering")
        sent = self.stream_bodies[-1]
        self.assertEqual((sent["stream"], sent["stream_options"], sent["n"]), (True, {"include_usage": True}, 1))
        self.assertEqual(sent["max_tokens"], metering["output_limit"])
        self.assertTrue(self.script.closed)

    def test_lower_bound_of_forwarded_output_is_billed_when_usage_under_reports(self):
        self.script = Script(chat_sse(list("abcdefghij"), prompt=20, completion=2))
        session = self.open()
        with self.http.stream("POST", self.url(session), json={"messages": MSG, "stream": True},
                              headers=self.auth(session)) as r:
            name, metering = sse_events(r.read().decode())[-1]
            metering = json.loads(metering)
        self.assertEqual((name, metering["output_lower_bound"], metering["call_tokens"]), ("neuxmind.metering", 10, 30))
        self.assertEqual(self.state(session)["used_tokens"], 30)

    def _start_stream(self, session, collected: list):
        def run():
            with self.http.stream("POST", self.url(session), json={"messages": MSG, "stream": True},
                                  headers=self.auth(session)) as r:
                collected.append(r.read().decode())
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_no_transaction_or_wallet_lock_is_held_while_streaming(self):
        self.script = Script(chat_sse(["a", "b", "c"], completion=3), gate_at=4)
        session = self.open()
        collected: list = []
        thread = self._start_stream(session, collected)
        self.assertTrue(self.script.reached.wait(10))
        # While the upstream is stalled mid-stream:
        t0 = time.monotonic()
        busy = self.http.post(self.url(session), json={"messages": MSG}, headers=self.auth(session))
        self.assertEqual((busy.status_code, busy.json()["code"]), (409, "session_busy"))
        busy_stream = self.http.post(self.url(session), json={"messages": MSG, "stream": True},
                                     headers=self.auth(session))
        self.assertEqual(busy_stream.json()["code"], "session_busy")
        close = self.http.post(f"{API}/gateway/sessions/{session['session_id']}/close", json={"request_id": "c"},
                               headers=self.user["headers"])
        self.assertEqual(close.json()["code"], "session_busy")
        # Money-moving work for the same org is not blocked (SQLite would serialise
        # on BEGIN IMMEDIATE, Postgres on the wallet row, if the stream held either).
        adjust = self.http.post(f"{API}/admin/wallets/{self.user['org_id']}/adjust",
                                json={"delta": 5, "reason": "during stream"}, headers=self.operator()["headers"])
        self.assertEqual(adjust.status_code, 200, adjust.text)
        other = self.http.post(f"{API}/gateway/sessions", json={"request_id": "other", "model": "scripted-llm",
                                                               "max_tokens": 500}, headers=self.user["headers"])
        self.assertEqual(other.status_code, 201, other.text)
        self.assertLess(time.monotonic() - t0, 3)
        self.script.gate.set()
        thread.join(10)
        name, metering = sse_events(collected[0])[-1]
        metering = json.loads(metering)
        self.assertEqual((name, metering["status"], metering["call_tokens"]), ("neuxmind.metering", "succeeded", 23))
        self.assertEqual(self.wait_idle(session)["used_tokens"], 23)

    def _disconnect_midway(self, session):
        with self.http.stream("POST", self.url(session), json={"messages": MSG, "stream": True},
                              headers=self.auth(session)) as r:
            self.assertEqual(r.status_code, 200)
            seen = 0
            for line in r.iter_lines():
                if line.startswith("data:"):
                    seen += 1
                if seen >= 3:
                    break
        # leaving the block drops the connection while the upstream is still stalled
        self.assertTrue(self.script.reached.wait(10))
        time.sleep(0.5)

    def test_client_disconnect_drains_the_upstream_and_settles_exactly(self):
        self.script = Script(chat_sse(["a", "b", "c", "d", "e"], completion=5), gate_at=6)
        session = self.open()
        self._disconnect_midway(session)
        self.assertEqual(self.state(session)["inflight_tokens"] > 0, True)   # still metering
        self.script.gate.set()
        state = self.wait_idle(session)
        self.assertEqual((state["used_tokens"], state["unknown_calls"], state["calls"]), (25, 0, 1))
        self.assertTrue(self.script.closed)
        audit = self.ok(self.get("/admin/audit?action=gateway.stream_client_disconnected", self.operator()))["audit"]
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["detail"]["client_disconnected"])
        closed = self.close(session)
        self.assertEqual((closed["status"], closed["credits_charged"]), ("closed", 1))

    def test_client_disconnect_then_upstream_cut_counts_the_hold(self):
        self.script = Script(chat_sse(["a", "b", "c", "d"], completion=4), gate_at=6, cut_at=7)
        session = self.open()
        self._disconnect_midway(session)
        self.script.gate.set()
        state = self.wait_idle(session)
        self.assertEqual(state["unknown_calls"], 1)
        hold = self.ok(self.get("/usage", self.user))["usage"][0]
        self.assertEqual(hold["status"], "reconciliation_required")
        self.assertGreater(state["used_tokens"], 900)            # the whole hold (budget 1000)
        self.assertLessEqual(state["used_tokens"], 1000)
        self.assertEqual(self.close(session)["status"], "reconciliation_required")

    def test_stream_ends_at_the_terminal_event_even_if_the_upstream_keeps_the_connection_open(self):
        """The gateway must not wait for EOF after [DONE] / response.completed."""
        chat = chat_sse(["a", "b"], completion=2)
        self.script = Script(chat + [b": still here\n"], gate_at=len(chat))
        session = self.open()
        t0 = time.monotonic()
        with self.http.stream("POST", self.url(session), json={"messages": MSG, "stream": True},
                              headers=self.auth(session)) as r:
            name, metering = sse_events(r.read().decode())[-1]
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((name, json.loads(metering)["call_tokens"]), ("neuxmind.metering", 22))
        events = []
        for kind, data in (("response.created", {"response": {"id": "r1"}}),
                           ("response.output_text.delta", {"delta": "hi"}),
                           ("response.completed", {"response": {"id": "r1", "usage": {"input_tokens": 7,
                                                                                    "output_tokens": 1}}})):
            events += [f"event: {kind}\n".encode(), f"data: {json.dumps({'type': kind, **data})}\n".encode(), b"\n"]
        self.script = Script(events + [b": still here\n"], gate_at=len(events))
        t0 = time.monotonic()
        with self.http.stream("POST", self.url(session, "responses"), json={"input": "x", "stream": True},
                              headers=self.auth(session)) as r:
            name, metering = sse_events(r.read().decode())[-1]
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((name, json.loads(metering)["call_tokens"]), ("neuxmind.metering", 8))

    def test_stream_declared_lost_by_the_sweeper_is_not_metered_twice(self):
        from neuxmind_cloud import maintenance
        self.script = Script(chat_sse(["a", "b"], completion=2), gate_at=4)
        session = self.open()
        collected: list = []
        thread = self._start_stream(session, collected)
        self.assertTrue(self.script.reached.wait(10))
        swept = maintenance.sweep(self.db, self.settings, at=time.time() + self.settings.stale_call_seconds + 1)
        self.assertEqual(swept["lost_calls"], 1)
        self.script.gate.set()
        thread.join(10)
        name, err = sse_events(collected[0])[-1]
        err = json.loads(err)
        self.assertEqual((name, err["code"], err["neuxmind"]["call_tokens"]), ("error", "call_marked_lost", None))
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["inflight_tokens"], state["unknown_calls"]), (0, 0, 1))

    def test_concurrent_calls_in_one_session_cannot_exceed_the_budget(self):
        """Rereview N-05: 6 concurrent calls, the upstream over-reports every one.
        Only one may be in flight; the others are refused before reaching the upstream."""
        session = self.open(max_tokens=1000)

        def call(_):
            return self.http.post(self.url(session), json={"messages": MSG}, headers=self.auth(session))

        with ThreadPoolExecutor(6) as pool:
            futures = [pool.submit(call, i) for i in range(6)]
            deadline = time.time() + 5
            while self.entered < 1 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.5)          # every other request has had time to reach the gateway
            self.call_gate.set()
            results = [f.result() for f in futures]
        codes = sorted(r.json().get("code", "ok") for r in results)
        self.assertEqual(self.entered, 1, codes)
        self.assertEqual(codes, sorted(["usage_exceeds_reservation"] + ["session_busy"] * 5))
        state = self.state(session)
        self.assertEqual(state["overage_calls"], 1)
        self.assertLessEqual(state["used_tokens"], 1000)
        overages = self.ok(self.get("/admin/overages", self.operator()))["overages"]
        self.assertEqual(len(overages), 1)
        closed = self.close(session)
        self.assertLessEqual(closed["credits_charged"], 10)          # ≤ the session's reservation

    def test_concurrent_honest_calls_serialise_and_stay_within_budget(self):
        self.reported = 100          # each call uses 100 tokens (always ≤ its hold)
        session = self.open(max_tokens=1000)
        self.call_gate.set()
        ok = 0
        for _round in range(4):
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda _: self.http.post(self.url(session), json={"messages": MSG},
                                                                 headers=self.auth(session)), range(4)))
            ok += sum(r.status_code == 200 for r in results)
            for r in results:
                self.assertIn(r.status_code, (200, 402, 409), r.text)
                if r.status_code == 409:
                    self.assertEqual(r.json()["code"], "session_busy")
        state = self.state(session)
        self.assertLessEqual(state["used_tokens"], 1000)
        self.assertEqual(state["used_tokens"], 100 * ok)
        self.assertGreaterEqual(ok, 1)


# ------------------------------------------------------------- real HTTP SSE upstream
class _FakeSSE(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"
    seen: list = []

    def log_message(self, *args):
        pass

    def _chunk(self, data: bytes):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, dict(self.headers), body))
        if self.mode.startswith("status:"):
            self.send_response(int(self.mode[7:]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.path.endswith("/responses"):
            events = [("response.created", {"type": "response.created", "response": {"id": "resp_9"}}),
                      ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "fake"}),
                      ("response.output_text.delta", {"type": "response.output_text.delta", "delta": " answer"}),
                      ("response.completed", {"type": "response.completed", "response": {
                          "id": "resp_9", "usage": {"input_tokens": 11, "output_tokens": 2, "total_tokens": 13}}})]
            frames = [f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events]
        else:
            frames = [f"data: {json.dumps(d)}\n\n".encode() for d in (
                {"id": "chatcmpl_9", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"id": "chatcmpl_9", "choices": [{"index": 0, "delta": {"content": "fake"}}]},
                {"id": "chatcmpl_9", "choices": [{"index": 0, "delta": {"content": " answer"}}]},
                {"id": "chatcmpl_9", "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2,
                                                              "total_tokens": 13}})] + [b"data: [DONE]\n\n"]
        if self.mode == "cut":
            self._chunk(frames[0] + frames[1])
            self.wfile.flush()
            self.connection.shutdown(2)           # drop without the terminating chunk
            self.close_connection = True
            return
        for frame in frames:
            self._chunk(frame)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class OpenAICompatibleStreamTest(_Base):
    """Protocol test against a local fake SSE server (not a real model provider)."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSSE)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        _FakeSSE.mode, _FakeSSE.seen = "ok", []
        self.user = self.register("sse@example.com")
        self.publish({"id": "remote-llm", "name": "Remote", "source": "closed", "kind": "llm", "license": "api",
                      "pricing": {"type": "per_1k_tokens", "credits": 10},
                      "upstream": {"provider": "openai_compatible", "base_url": base, "model": "gpt-test"}})
        self.entitle(self.user, "remote-llm")
        self.fund(self.user, 100)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_chat_and_responses_streams_over_http(self):
        session = self.open(model="remote-llm")
        response, events = self.stream(session, {"messages": MSG, "max_tokens": 50})
        self.assertEqual(response.headers["x-neuxmind-simulated"], "false")
        path, headers, body = _FakeSSE.seen[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual((body["model"], body["stream"], body["stream_options"], body["max_tokens"], body["n"]),
                         ("gpt-test", True, {"include_usage": True}, 50, 1))
        name, metering = self.terminal(events)
        self.assertEqual((name, metering["call_tokens"], metering["upstream_ref"], metering["simulated"]),
                         ("neuxmind.metering", 13, "openai_compatible:chatcmpl_9", False))
        response, events = self.stream(session, {"input": "hi"}, path="responses")
        self.assertEqual(_FakeSSE.seen[-1][2]["stream"], True)
        self.assertNotIn("stream_options", _FakeSSE.seen[-1][2])
        name, metering = self.terminal(events)
        self.assertEqual((name, metering["call_tokens"], metering["used_tokens"]), ("neuxmind.metering", 13, 26))

    def test_connection_dropped_mid_stream(self):
        session = self.open(model="remote-llm")
        _FakeSSE.mode = "cut"
        response, events = self.stream(session, {"messages": MSG})
        self.assertEqual(response.status_code, 200)
        name, err = self.terminal(events)
        self.assertEqual((name, err["code"]), ("error", "upstream_interrupted"))
        self.assertEqual(self.state(session)["unknown_calls"], 1)

    def test_http_errors_before_streaming(self):
        session = self.open(model="remote-llm")
        _FakeSSE.mode = "status:429"
        self.error(self.client.post(self.url(session), json={"messages": MSG, "stream": True},
                                    headers=self.auth(session)), 502, "upstream_error")
        _FakeSSE.mode = "status:503"
        self.error(self.client.post(self.url(session), json={"messages": MSG, "stream": True},
                                    headers=self.auth(session)), 502, "upstream_unknown")
        state = self.state(session)
        self.assertEqual((state["used_tokens"], state["unknown_calls"], state["inflight_tokens"]), (0, 1, 0))


# ------------------------------------------------------------- meter unit tests
class StreamMeterTest(CloudTestCase):
    def test_lower_bound_counts_every_non_empty_output_delta(self):
        from neuxmind_cloud.streaming import SSEEvent, StreamMeter
        meter = StreamMeter("/chat/completions", output_limit=3)
        chunk = lambda delta: SSEEvent(None, json.dumps({"id": "x", "choices": [{"index": 0, "delta": delta}]}))
        self.assertEqual(meter.feed(chunk({"role": "assistant", "content": ""})), "forward")
        self.assertEqual(meter.feed(chunk({"content": "a"})), "forward")
        self.assertEqual(meter.feed(chunk({"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": ""}}]})),
                         "forward")
        self.assertEqual(meter.feed(chunk({"reasoning_content": "r"})), "forward")
        self.assertEqual(meter.lower_bound, 3)
        self.assertEqual(meter.feed(chunk({"refusal": "no"})), "truncate")       # would be the 4th unit
        self.assertEqual(meter.lower_bound, 3)
        responses = StreamMeter("/responses", output_limit=1)
        ev = lambda kind, **kw: SSEEvent(kind, json.dumps({"type": kind, **kw}))
        self.assertEqual(responses.feed(ev("response.function_call_arguments.delta", delta="{")), "forward")
        self.assertEqual(responses.feed(ev("response.output_text.delta", delta="x")), "truncate")
        self.assertEqual(responses.feed(ev("response.completed", response={"id": "r", "usage": {
            "input_tokens": 1, "output_tokens": 1}})), "forward")
        self.assertTrue(responses.done)
        self.assertEqual(responses.usage.total_tokens, 2)

    def test_usage_reported_mid_stream_also_triggers_truncation(self):
        from neuxmind_cloud.streaming import SSEEvent, StreamMeter
        meter = StreamMeter("/chat/completions", output_limit=10)
        ev = SSEEvent(None, json.dumps({"choices": [{"delta": {"content": "x"}}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 11}}))
        self.assertEqual(meter.feed(ev), "truncate")

    def test_sse_parser(self):
        from neuxmind_cloud.streaming import parse_sse
        lines = [b": keepalive\n", b"event: a\n", b"data: 1\n", b"data: 2\n", b"id: 7\n", b"\n", b"data:x\r\n", b"\r\n",
                 b"data: incomplete\n"]
        self.assertEqual([(e.event, e.data) for e in parse_sse(lines)], [("a", "1\n2"), (None, "x")])


class ChannelBackpressureTest(CloudTestCase):
    def test_bounded_channel_blocks_the_producer_and_gives_up_on_an_idle_client(self):
        import asyncio
        from neuxmind_cloud.streaming import _Channel
        loop = asyncio.new_event_loop()
        try:
            channel = _Channel(loop, capacity=2, idle_seconds=0.5)
            self.assertTrue(channel.put(b"1"))
            self.assertTrue(channel.put(b"2"))
            t0 = time.monotonic()
            self.assertFalse(channel.put(b"3"))          # nobody reads: blocks, then treats the client as gone
            self.assertGreaterEqual(time.monotonic() - t0, 0.4)
            self.assertTrue(channel.gone.is_set())
            loop.run_until_complete(asyncio.sleep(0))    # run the scheduled puts
            self.assertEqual(channel.queue.qsize(), 2)
            self.assertEqual(loop.run_until_complete(channel.get()), b"1")
        finally:
            loop.close()


# ------------------------------------------------------------- review Q2-02: wall-clock deadline
class _KeepAliveForever(UpstreamStream):
    simulated = True

    def __init__(self, interval=0.05):
        self.interval, self.aborted = interval, threading.Event()

    def lines(self):
        while not self.aborted.is_set():
            time.sleep(self.interval)
            yield b": keepalive\n"

    def close(self):
        self.aborted.set()


class _Silent(UpstreamStream):
    """Sends nothing at all; ``abort()`` (watchdog) unblocks the read."""
    simulated = True

    def __init__(self):
        self.aborted = threading.Event()

    def lines(self):
        self.aborted.wait(30)
        if False:
            yield b""
        raise UpstreamUnknown("connection aborted", "aborted")

    def close(self):
        self.aborted.set()


class WallClockDeadlineTest(CloudTestCase):
    def run_stream(self, upstream, **kw):
        import asyncio
        from neuxmind_cloud import streaming
        seen = {}

        def settle(outcome):
            seen["o"] = outcome
            return "neuxmind.metering", {}

        async def main():
            s = streaming.SessionStream(upstream, streaming.StreamMeter("/chat/completions", 100), settle,
                                        idle_seconds=5, headers={}, **kw)
            s.start(asyncio.get_running_loop())
            while await s.channel.get() is not None:
                pass

        t0 = time.monotonic()
        asyncio.run(main())
        return time.monotonic() - t0, seen["o"]

    def test_keepalive_comments_do_not_extend_the_deadline(self):
        """The rereview's reproduction: keep-alives only; previously the deadline never ran."""
        elapsed, outcome = self.run_stream(_KeepAliveForever(), max_seconds=0.5)
        self.assertEqual(outcome.termination, "deadline")
        self.assertLess(elapsed, 1.5)

    def test_a_silent_upstream_is_ended_by_the_watchdog(self):
        elapsed, outcome = self.run_stream(_Silent(), max_seconds=0.5, upstream_idle_seconds=30)
        self.assertEqual(outcome.termination, "deadline")
        self.assertLess(elapsed, 1.5)
        elapsed, outcome = self.run_stream(_Silent(), max_seconds=30, upstream_idle_seconds=0.4)
        self.assertEqual(outcome.termination, "interrupted")
        self.assertIn("nothing", outcome.detail)
        self.assertLess(elapsed, 1.5)

    def test_line_level_deadline_even_if_abort_has_no_effect(self):
        class Stubborn(_KeepAliveForever):
            def abort(self):
                pass                                  # e.g. a transport that cannot be interrupted
        upstream = Stubborn()
        elapsed, outcome = self.run_stream(upstream, max_seconds=0.5)
        self.assertEqual(outcome.termination, "deadline")
        self.assertLess(elapsed, 1.5)

    def test_keepalives_count_as_activity_for_the_idle_limit(self):
        elapsed, outcome = self.run_stream(_KeepAliveForever(0.05), max_seconds=1.0, upstream_idle_seconds=0.3)
        self.assertEqual(outcome.termination, "deadline")      # not "idle": comments are activity
        self.assertGreaterEqual(elapsed, 0.9)


class _SlowSSE(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "keepalive"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.mode == "keepalive":
                    frame = b": ping\n\n"
                    self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                    self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


class HttpDeadlineTest(_Base):
    settings_overrides = {"stream_max_seconds": 1}

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowSSE)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.user = self.register("slowhttp@example.com")
        self.publish({"id": "remote-llm", "name": "Remote", "source": "closed", "kind": "llm", "license": "api",
                      "pricing": {"type": "per_1k_tokens", "credits": 10},
                      "upstream": {"provider": "openai_compatible",
                                   "base_url": f"http://127.0.0.1:{self.server.server_address[1]}/v1", "model": "m"}})
        self.entitle(self.user, "remote-llm")
        self.fund(self.user, 100)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_keepalive_only_and_silent_upstreams_end_at_the_deadline(self):
        for mode in ("keepalive", "silent"):
            _SlowSSE.mode = mode
            session = self.open(model="remote-llm")
            t0 = time.monotonic()
            response, events = self.stream(session, {"messages": MSG})
            self.assertLess(time.monotonic() - t0, 3, mode)
            name, err = self.terminal(events)
            self.assertEqual((name, err["code"]), ("error", "stream_duration_exceeded"), mode)
            self.assertEqual(self.state(session)["inflight_tokens"], 0)


# ------------------------------------------------------------- review Q2-03: reserved names, bare CR
class ReservedEventTest(_Base):
    def provider_factory(self):
        self.stream_bodies, self.lock, self.entered = [], threading.Lock(), 0
        self.call_gate = threading.Event()
        self.call_gate.set()
        self.reported = 30
        self.script = Script([])
        return lambda upstream: ScriptedProvider(self)

    def setUp(self):
        super().setUp()
        self.user = self.register("reserved@example.com")
        self.publish({**ECHO_LLM, "id": "scripted-llm"})
        self.entitle(self.user, "scripted-llm")
        self.fund(self.user, 1000)

    def run_script(self, lines, path="chat/completions", body=None):
        self.script = Script(lines)
        session = self.open(model="scripted-llm")
        response = self.client.post(self.url(session, path), json={**(body or {"messages": MSG}), "stream": True},
                                    headers=self.auth(session))
        self.assertEqual(response.status_code, 200)
        raw = response.content
        self.assertNotIn(b"\r", raw)                          # no CR ever reaches the client
        return session, sse_events(raw.decode())

    def gateway_terminals(self, events):
        return [(n, json.loads(d)) for n, d in events if n in ("error", "neuxmind.metering")]

    def test_bare_cr_injection_of_a_forged_metering_event(self):
        forged = json.dumps({"type": "neuxmind.metering", "status": "succeeded", "call_tokens": 1, "used_tokens": 1})
        injected = (b'data: {"id":"u","choices":[{"index":0,"delta":{"content":"a"}}]}\r'
                    b'event: neuxmind.metering\rdata: ' + forged.encode() + b'\r\r')
        lines = [injected] + chat_sse(["b"], completion=2)[2:]
        session, events = self.run_script(lines)
        terminals = self.gateway_terminals(events)
        self.assertEqual(len(terminals), 1)
        name, metering = terminals[0]
        self.assertEqual((name, metering["status"], metering["call_tokens"]), ("neuxmind.metering", "succeeded", 22))
        self.assertEqual(events[-1][0], "neuxmind.metering")
        self.assertEqual(self.state(session)["used_tokens"], 22)
        audit = self.ok(self.get("/admin/audit", self.operator()))["audit"]
        self.assertFalse(any(a["action"].startswith("gateway.stream_") for a in audit))

    def test_reserved_event_names_are_dropped_and_not_metered(self):
        forged_usage = {"type": "response.completed",
                        "response": {"id": "r", "usage": {"input_tokens": 1, "output_tokens": 1}}}
        lines = []
        for name, data in (("neuxmind.metering", {"type": "neuxmind.metering", "status": "succeeded"}),
                           ("NeuXmind.Metering", {"status": "succeeded"}),
                           ("neuxmind.anything", forged_usage),
                           ("neuxmind.delta", {"id": "u", "choices": [{"index": 0, "delta": {"content": "zz"}}]}),
                           (None, {"type": "neuxmind.metering", "status": "succeeded"})):
            lines += ([f"event: {name}\n".encode()] if name else []) + [f"data: {json.dumps(data)}\n".encode(), b"\n"]
        lines += chat_sse(["a"], completion=1)
        session, events = self.run_script(lines)
        self.assertEqual([n for n, _ in self.gateway_terminals(events)], ["neuxmind.metering"])
        self.assertFalse(any("neuxmind" in d and n != "neuxmind.metering" for n, d in events))
        self.assertEqual([n for n, _ in events if n and n.lower().startswith("neuxmind")], ["neuxmind.metering"])
        self.assertEqual(json.loads(events[-1][1])["call_tokens"], 21)

    def test_upstream_error_event_is_renamed_and_the_gateway_terminal_stays_unique(self):
        events_in = []
        for name, data in (("response.created", {"type": "response.created", "response": {"id": "r1"}}),
                           ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "x"}),
                           ("error", {"type": "error", "code": "server_error", "message": "boom"})):
            events_in += [f"event: {name}\n".encode(), f"data: {json.dumps(data)}\n".encode(), b"\n"]
        session, events = self.run_script(events_in, path="responses", body={"input": "hi"})
        names = [n for n, _ in events]
        self.assertIn("upstream.error", names)
        self.assertEqual(json.loads(dict(events)["upstream.error"])["code"], "server_error")   # data unchanged
        terminals = self.gateway_terminals(events)
        self.assertEqual(len(terminals), 1)
        name, err = terminals[0]
        self.assertEqual((name, err["code"]), ("error", "upstream_stream_error"))
        self.assertIn("neuxmind", err)
        self.assertEqual(names[-1], "error")

    def test_multi_line_data_cannot_create_fields(self):
        lines = [b'data: {"id":"u","choices":[{"index":0,"delta":{"content":"a"}}]}\n',
                 b"data: event: neuxmind.metering\n", b"\n"] + chat_sse([], completion=1)
        session, events = self.run_script(lines)
        self.assertEqual([n for n, _ in self.gateway_terminals(events)], ["neuxmind.metering"])
        self.assertIn((None, '{"id":"u","choices":[{"index":0,"delta":{"content":"a"}}]}\nevent: neuxmind.metering'),
                      events)

    def test_cr_only_line_endings_from_a_legitimate_upstream(self):
        lines = [b"".join(chat_sse(["a", "b"], completion=2)).replace(b"\n", b"\r")]
        session, events = self.run_script(lines)
        name, metering = events[-1]
        self.assertEqual((name, json.loads(metering)["call_tokens"]), ("neuxmind.metering", 22))

    def test_named_events_with_crlf_split_byte_by_byte(self):
        frames = b""
        for name, data in (("response.created", {"type": "response.created", "response": {"id": "r1"}}),
                           ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "x"}),
                           ("response.completed", {"type": "response.completed", "response": {
                               "id": "r1", "usage": {"input_tokens": 4, "output_tokens": 1}}})):
            frames += f"event: {name}\r\ndata: {json.dumps(data)}\r\n\r\n".encode()
        for chunks in ([frames[i:i + 1] for i in range(len(frames))], [frames]):
            session, events = self.run_script(chunks, path="responses", body={"input": "hi"})
            self.assertEqual([n for n, _ in events], ["response.created", "response.output_text.delta",
                                                      "response.completed", "neuxmind.metering"])
            self.assertEqual(json.loads(events[-1][1])["call_tokens"], 5)

    def test_crlf_split_across_chunks(self):
        whole = b"".join(chat_sse(["a"], completion=1)).replace(b"\n", b"\r\n")
        lines = [whole[i:i + 1] for i in range(len(whole))]           # one byte per read
        session, events = self.run_script(lines)
        self.assertEqual(json.loads(events[-1][1])["call_tokens"], 21)
        self.assertEqual(len([e for e in events if e == (None, "[DONE]")]), 1)


class ReviewP3Test(CloudTestCase):
    def test_every_responses_delta_counts_towards_the_lower_bound(self):
        """Review Q2-06."""
        from neuxmind_cloud.streaming import SSEEvent, StreamMeter
        meter = StreamMeter("/responses", output_limit=10)
        for kind in ("response.reasoning_summary_text.delta", "response.output_text.delta",
                     "response.custom_tool_call_input.delta"):
            meter.feed(SSEEvent(kind, json.dumps({"type": kind, "delta": "x"})))
        meter.feed(SSEEvent("response.output_text.delta", json.dumps({"type": "response.output_text.delta",
                                                                       "delta": ""})))
        meter.feed(SSEEvent("response.output_text.done", json.dumps({"type": "response.output_text.done",
                                                                      "text": "xyz"})))
        self.assertEqual(meter.lower_bound, 3)

    def test_echo_non_streamed_responses_has_an_output_array(self):
        user = self.register("echo-out@example.com")
        self.publish(ECHO_LLM)
        self.entitle(user, "echo-llm")
        self.fund(user, 100)
        session = self.ok(self.post("/gateway/sessions", {"request_id": "e", "model": "echo-llm",
                                                          "max_tokens": 1000}, user), 201)
        reply = self.ok(self.client.post(f"{API}/gateway/sessions/{session['session_id']}/v1/responses",
                                         json={"input": "status report"},
                                         headers={"Authorization": f"Bearer {session['access_token']}"}))
        self.assertEqual((reply["object"], reply["status"], reply["simulated"]), ("response", "completed", True))
        text = "".join(part["text"] for item in reply["output"] if item["type"] == "message"
                       for part in item["content"] if part["type"] == "output_text")
        self.assertEqual(text, "echo: status report")
        self.assertEqual(reply["usage"]["input_tokens"] + reply["usage"]["output_tokens"],
                         reply["neuxmind"]["used_tokens"])
