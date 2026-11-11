"""Metered gateway sessions (open_session / chat / responses / streaming / close) against the
real cloud app (uvicorn, random port, SQLite).

The only stand-in is the cloud's own EchoProvider upstream (a test double that
echoes the prompt one word per stream event, every event ``simulated: true``).
A broken network is simulated with :class:`_support.CuttingProxy`, which drops
the TCP connection in the middle of an SSE response.
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _support
from _support import OPERATOR_EMAIL, PASSWORD, SDK_DIR, CliHome, CloudServer, CuttingProxy

import neuxmind.gateway as gateway_module
from neuxmind import NeuxMindError
from neuxmind.cloud import CloudClient
from neuxmind.gateway import GatewaySession, GatewayStream, GatewayStreamError, Metering, StreamInterrupted

DEV_EMAIL = "streamer@sdk.test"


def card(model_id, **upstream):
    return {"id": model_id, "name": f"{model_id} (test double)", "source": "closed", "kind": "llm",
            "license": "test only", "pricing": {"type": "per_1k_tokens", "credits": 10},
            "upstream": {"provider": "echo", **upstream}}


MODELS = [card("echo-chat"), card("echo-slow", stream_delay=0.05), card("echo-crawl", stream_delay=0.4),
          card("echo-overuse", behavior="overuse"), card("echo-cut", behavior="stream_cut"),
          card("echo-overrun", behavior="usage_overrun")]

PROMPT = "one two three four five six seven eight nine ten"
EXPECTED_TEXT = "echo: " + PROMPT
# Echo usage: prompt words + completion words ("echo:" + the prompt words).
EXPECTED_TOKENS = 10 + 11


def user(text=PROMPT):
    return [{"role": "user", "content": text}]


class GatewaySessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = CloudServer()
        cls.ops = CloudClient(cls.cloud.url)
        reg = cls.ops.register(OPERATOR_EMAIL, PASSWORD)
        cls.ops.verify_email(reg["dev_verification_token"])
        for model in MODELS:
            cls.ops._request("POST", "/admin/models", model)
        cls.dev = CloudClient(cls.cloud.url)
        reg = cls.dev.register(DEV_EMAIL, PASSWORD)
        cls.dev.verify_email(reg["dev_verification_token"])
        cls.ops._request("POST", f"/admin/wallets/{reg['org_id']}/adjust", {"delta": 100_000, "reason": "sdk test"})
        for model in MODELS:
            cls.dev.purchase_model(model["id"])

    @classmethod
    def tearDownClass(cls):
        cls.cloud.close()

    def session(self, model="echo-chat", max_tokens=2000, client=None, **kw) -> GatewaySession:
        s = (client or self.dev).gateway.open_session(model, max_tokens, **kw)
        self.addCleanup(lambda: s.close_result is None and s.close(wait=30))
        return s

    def spy_requests(self):
        calls = []
        real_json, real_stream = gateway_module.request_json, gateway_module.open_stream

        def json_spy(method, url, **kw):
            calls.append(("json", method, url, kw.get("body")))
            return real_json(method, url, **kw)

        def stream_spy(method, url, **kw):
            calls.append(("stream", method, url, kw.get("body")))
            return real_stream(method, url, **kw)

        for name, spy in (("request_json", json_spy), ("open_stream", stream_spy)):
            patcher = mock.patch.object(gateway_module, name, spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    # ------------------------------------------------------------ non-streamed + context manager
    def test_open_call_and_close_with_context_manager(self):
        wallet_before = self.dev.wallet()
        with self.dev.gateway.open_session("echo-chat", 2000) as s:
            self.assertTrue(s.access_token)
            self.assertEqual(s.openai_base_url, f"{self.cloud.url}/api/v1/gateway/sessions/{s.session_id}/v1")
            self.assertTrue(s.server_base_url.endswith(f"/gateway/sessions/{s.session_id}/v1"))
            self.assertGreater(self.dev.wallet()["reserved"], wallet_before["reserved"])

            reply = s.chat(user(), temperature=0.2)
            self.assertEqual(reply["choices"][0]["message"]["content"], EXPECTED_TEXT)
            self.assertTrue(reply["neuxmind"]["simulated"])
            m = s.last_metering
            self.assertEqual((m.source, m.status, m.call_tokens, m.used_tokens, m.max_tokens),
                             ("response", "succeeded", EXPECTED_TOKENS, EXPECTED_TOKENS, 2000))
            self.assertEqual(m.remaining_tokens, 2000 - EXPECTED_TOKENS)

            reply = s.responses(PROMPT)
            self.assertEqual(reply["output_text"], EXPECTED_TEXT)
            self.assertEqual((s.last_metering.call_tokens, s.last_metering.used_tokens),
                             (EXPECTED_TOKENS, 2 * EXPECTED_TOKENS))
            view = s.status()
            self.assertEqual((view["status"], view["used_tokens"], view["calls"], view["inflight_tokens"]),
                             ("open", 2 * EXPECTED_TOKENS, 2, 0))
        # Leaving the block closed and settled the session.
        self.assertEqual(s.close_result["status"], "closed")
        self.assertEqual(s.close_result["usage_tokens"], 2 * EXPECTED_TOKENS)
        self.assertEqual(s.close(), s.close_result)                     # idempotent, no new request
        self.assertEqual(self.dev.gateway.session(s.session_id)["status"], "closed")
        self.assertEqual(self.dev.wallet()["reserved"], wallet_before["reserved"])
        with self.assertRaises(NeuxMindError) as cm:
            s.chat(user())
        self.assertEqual(cm.exception.code, "session_closed")

    def test_context_manager_closes_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.dev.gateway.open_session("echo-chat", 500) as s:
                s.chat(user())
                raise RuntimeError("boom")
        self.assertEqual(s.close_result["status"], "closed")
        self.assertIsNone(s.close_error)

    def test_errors_before_the_stream_are_json_errors(self):
        s = self.session(max_tokens=30)
        calls = self.spy_requests()
        for stream in (False, True):
            with self.assertRaises(NeuxMindError) as cm:
                s.chat(user(" ".join(["word"] * 50)), stream=stream)
            self.assertEqual((cm.exception.code, cm.exception.status), ("session_budget_exhausted", 402))
            self.assertIn("开新会话", cm.exception.details["hint"])
            self.assertNotIsInstance(cm.exception, (GatewayStreamError, StreamInterrupted))
        with self.assertRaises(NeuxMindError) as cm:
            s.chat(user(), stream=True, logit_bias={"1": 2})
        self.assertEqual((cm.exception.code, cm.exception.status), ("parameter_not_allowed", 400))
        self.assertEqual(cm.exception.details["session_id"], s.session_id)
        # stream_options is refused locally: nothing is sent.
        sent = len(calls)
        with self.assertRaises(NeuxMindError) as cm:
            s.chat(user(), stream=True, stream_options={"include_usage": False})
        self.assertEqual(cm.exception.code, "parameter_not_allowed")
        self.assertEqual(len(calls), sent)
        with self.assertRaises(NeuxMindError) as cm:
            self.dev.gateway.open_session("not-a-model", 100)
        self.assertEqual(cm.exception.status, 404)

    # ------------------------------------------------------------ streaming
    def test_chat_stream_reads_past_done_to_the_metering_event(self):
        s = self.session()
        calls = self.spy_requests()
        stream = s.chat(user(), stream=True)
        self.assertIsInstance(stream, GatewayStream)
        self.assertTrue(stream.simulated)
        self.assertGreater(stream.output_limit, 0)
        events = list(stream)
        sent = [c for c in calls if c[0] == "stream"]
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0][3]["stream"], True)
        self.assertNotIn("stream_options", sent[0][3])                  # the gateway writes it

        done = [i for i, ev in enumerate(events) if ev.is_done]
        self.assertEqual(len(done), 1)
        self.assertTrue(events[-1].is_metering)
        self.assertGreater(len(events) - 1, done[0])                    # metering comes after [DONE]
        self.assertTrue(all(ev.json.get("simulated") for ev in events if isinstance(ev.json, dict)))
        self.assertEqual(stream.text, EXPECTED_TEXT)
        m = stream.metering
        self.assertIsInstance(m, Metering)
        self.assertEqual((m.source, m.status, m.call_tokens, m.used_tokens, m.client_disconnected, m.simulated),
                         ("stream", "succeeded", EXPECTED_TOKENS, EXPECTED_TOKENS, False, True))
        self.assertEqual(m.usage["total_tokens"], EXPECTED_TOKENS)
        self.assertIs(s.last_metering, m)
        with self.assertRaises(NeuxMindError) as cm:
            list(stream)
        self.assertEqual(cm.exception.code, "stream_consumed")

        # Same session, next call: the server counted it after the stream.
        again = s.chat(user(), stream=True).collect()
        self.assertEqual((again.metering.call_tokens, again.metering.used_tokens),
                         (EXPECTED_TOKENS, 2 * EXPECTED_TOKENS))
        self.assertEqual(s.close()["usage_tokens"], 2 * EXPECTED_TOKENS)

    def test_responses_stream_text_deltas(self):
        s = self.session()
        stream = s.responses(PROMPT, stream=True)
        pieces = list(stream.text_deltas())
        self.assertEqual(len(pieces), 11)                               # one word per event
        self.assertEqual("".join(pieces), EXPECTED_TEXT)
        types = [ev.type for ev in stream.events]
        self.assertEqual(types[0], "response.created")
        self.assertEqual(types[-2:], ["response.completed", "neuxmind.metering"])
        self.assertEqual((stream.metering.status, stream.metering.call_tokens), ("succeeded", EXPECTED_TOKENS))

    def test_truncated_stream_raises_output_limit_reached_with_metering(self):
        s = self.session("echo-overuse", max_tokens=200)
        stream = s.chat(user(), stream=True)
        with self.assertRaises(GatewayStreamError) as cm:
            for _ in stream:
                pass
        exc = cm.exception
        self.assertEqual(exc.code, "output_limit_reached")
        self.assertIn("截断", str(exc))
        self.assertIs(stream.error, exc)
        m = exc.metering
        self.assertEqual(m.status, "reconciliation_required")
        self.assertEqual(m.call_tokens, m.hold_tokens)                  # counted at the hold, never more
        self.assertEqual(m.output_limit, stream.output_limit)
        self.assertLessEqual(m.output_lower_bound, m.output_limit)
        self.assertTrue(stream.text.startswith("echo: one two"))       # what arrived before the cut is kept
        self.assertLessEqual(len(stream.text.split()), stream.output_limit)
        self.assertEqual(s.status()["unknown_calls"], 1)
        self.assertEqual(s.close()["status"], "reconciliation_required")

    def test_other_in_stream_errors(self):
        s = self.session("echo-cut")
        with self.assertRaises(GatewayStreamError) as cm:
            s.chat(user(), stream=True).collect()
        self.assertEqual((cm.exception.code, cm.exception.metering.status),
                         ("upstream_interrupted", "reconciliation_required"))
        s = self.session("echo-overrun")
        stream = s.chat(user(), stream=True)
        with self.assertRaises(GatewayStreamError) as cm:
            stream.collect()
        self.assertEqual((cm.exception.code, cm.exception.metering.status), ("usage_exceeds_reservation", "overage"))
        self.assertEqual(stream.text, EXPECTED_TEXT)                    # output was delivered (cannot be withdrawn)
        with self.assertRaises(NeuxMindError) as cm:
            s.chat(user())
        self.assertEqual(cm.exception.code, "session_overage_locked")
        self.assertIn("只能关闭", cm.exception.details["hint"])

    # ------------------------------------------------------------ one call in flight (ADR-016)
    def test_concurrent_call_in_one_session_is_session_busy(self):
        s = self.session("echo-slow")
        stream = s.chat(user(), stream=True)
        first_event = threading.Event()
        result = {}

        def consume():
            try:
                for _ in stream:
                    first_event.set()
                result["metering"] = stream.metering
            except Exception as exc:  # pragma: no cover - reported below
                result["error"] = exc
            first_event.set()

        worker = threading.Thread(target=consume)
        worker.start()
        self.assertTrue(first_event.wait(20))
        for call in (lambda: s.chat(user()), lambda: s.chat(user(), stream=True),
                     lambda: s.responses(PROMPT, stream=True)):
            with self.assertRaises(NeuxMindError) as cm:
                call()
            self.assertEqual((cm.exception.code, cm.exception.status), ("session_busy", 409))
            self.assertIn("为每一路各开一个会话", cm.exception.details["hint"])
            self.assertIn("不能在同一会话上并发", str(cm.exception))
        # A second session runs in parallel without trouble.
        other = self.session("echo-slow")
        self.assertEqual(other.chat(user())["choices"][0]["message"]["content"], EXPECTED_TEXT)
        worker.join(30)
        self.assertNotIn("error", result)
        self.assertEqual((result["metering"].status, result["metering"].call_tokens), ("succeeded", EXPECTED_TOKENS))
        # Only the first call reached the upstream and was counted.
        self.assertEqual(s.status()["calls"], 1)
        self.assertEqual(s.chat(user())["neuxmind"]["used_tokens"], 2 * EXPECTED_TOKENS)
        # The refused calls counted nothing, so the per-call figure is still known.
        self.assertEqual(s.last_metering.call_tokens, EXPECTED_TOKENS)

    # ------------------------------------------------------------ broken connections
    def proxied_client(self, cut_after=400) -> tuple[CloudClient, CuttingProxy]:
        proxy = CuttingProxy(int(self.cloud.url.rsplit(":", 1)[1]), cut_after=cut_after)
        self.addCleanup(proxy.close)
        return CloudClient(proxy.url, self.dev.token), proxy

    def test_disconnect_mid_stream_recovers_usage_from_the_session(self):
        client, proxy = self.proxied_client()
        s = self.session("echo-slow", client=client)
        s.chat(user())                                                  # non-SSE traffic passes untouched
        proxy.cut_after = 10 ** 9
        s.chat(user(), stream=True).collect()                           # a healthy stream first
        proxy.cut_after = 400
        stream = s.chat(user(), stream=True)
        received = []
        with self.assertRaises(StreamInterrupted) as cm:
            for ev in stream:
                received.append(ev)
        self.assertEqual(proxy.cuts, 1)
        exc = cm.exception
        self.assertEqual(exc.code, "stream_interrupted")
        self.assertIs(stream.interrupted, exc)
        self.assertTrue(received)
        self.assertFalse(any(ev.is_metering for ev in received))
        self.assertLess(len(stream.text), len(EXPECTED_TEXT))           # incomplete output
        m = exc.metering
        # The gateway kept reading the upstream after the client vanished and settled exactly.
        self.assertEqual((m.source, m.status, m.call_tokens, m.used_tokens, m.client_disconnected),
                         ("session_query", "succeeded", EXPECTED_TOKENS, 3 * EXPECTED_TOKENS, True))
        self.assertIs(stream.metering, m)
        self.assertIs(s.last_metering, m)
        self.assertEqual(exc.details["session"]["inflight_tokens"], 0)
        self.assertIn("输出不完整", str(exc))
        # The next streamed call through a healthy connection still meters correctly.
        healthy = GatewaySession(self.dev, {**s.view, "access_token": s.access_token})
        healthy._snapshot = None                                        # baseline unknown → read before the call
        follow = healthy.chat(user(), stream=True).collect()
        self.assertEqual((follow.metering.call_tokens, follow.metering.used_tokens),
                         (EXPECTED_TOKENS, 4 * EXPECTED_TOKENS))
        self.assertEqual(s.close()["usage_tokens"], 4 * EXPECTED_TOKENS)

    def test_disconnect_reports_outcome_unknown_when_not_settled_in_time(self):
        client, _proxy = self.proxied_client(cut_after=300)
        s = self.session("echo-crawl", client=client, recover_timeout=0)
        stream = s.chat(user(), stream=True)
        with self.assertRaises(StreamInterrupted) as cm:
            stream.collect()
        self.assertEqual(cm.exception.code, "outcome_unknown")
        self.assertIsNone(cm.exception.metering)
        self.assertGreater(cm.exception.details["session"]["inflight_tokens"], 0)
        self.assertIn("nx sessions show", str(cm.exception))
        with self.assertRaises(NeuxMindError) as cm:
            s.chat(user())                                              # still draining on the server
        self.assertEqual(cm.exception.code, "session_busy")
        # close() waits for the gateway to finish draining (409 session_busy until then), then settles exactly.
        started = time.monotonic()
        closed = s.close(wait=60)
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertEqual((closed["status"], closed["usage_tokens"]), ("closed", EXPECTED_TOKENS))

    def test_unknown_outcome_drops_the_per_call_baseline(self):
        client, _proxy = self.proxied_client(cut_after=300)
        s = self.session("echo-crawl", client=client, recover_timeout=0)
        with self.assertRaises(StreamInterrupted) as cm:
            s.chat(user(), stream=True).collect()
        self.assertEqual(cm.exception.code, "outcome_unknown")
        deadline = time.monotonic() + 60
        while s.status()["inflight_tokens"] and time.monotonic() < deadline:
            time.sleep(0.1)
        s.chat(user())
        # The SDK no longer knows where the previous call left the session: no guessed figure.
        self.assertIsNone(s.last_metering.call_tokens)
        self.assertEqual(s.last_metering.used_tokens, 2 * EXPECTED_TOKENS)

    def test_abandoned_stream_then_close_on_exit(self):
        with self.dev.gateway.open_session("echo-slow", 2000) as s:
            stream = s.chat(user(), stream=True)
            for ev in stream:
                if ev.text:
                    break                                               # leave early
            self.assertTrue(stream.abandoned)
            self.assertIsNone(stream.metering)
        # The gateway drained the upstream; the context manager waited and closed.
        self.assertEqual((s.close_result["status"], s.close_result["usage_tokens"]), ("closed", EXPECTED_TOKENS))

    def test_replayed_open_has_no_access_token(self):
        s = self.session(request_id="sdk-replay-1")
        again = self.dev.gateway.open_session("echo-chat", 2000, request_id="sdk-replay-1")
        self.assertEqual(again.session_id, s.session_id)
        self.assertTrue(again.replayed)
        with self.assertRaises(NeuxMindError) as cm:
            again.chat(user())
        self.assertEqual(cm.exception.code, "access_token_unavailable")


class GatewayCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = CloudServer()
        ops = CloudClient(cls.cloud.url)
        reg = ops.register(OPERATOR_EMAIL, PASSWORD)
        ops.verify_email(reg["dev_verification_token"])
        for model in MODELS:
            ops._request("POST", "/admin/models", model)
        cls.home = CliHome()
        cls.home.json("config", "set", "cloud_url", cls.cloud.url)
        dev = CloudClient(cls.cloud.url)
        reg = dev.register(DEV_EMAIL, PASSWORD)
        dev.verify_email(reg["dev_verification_token"])
        ops._request("POST", f"/admin/wallets/{reg['org_id']}/adjust", {"delta": 100_000, "reason": "sdk test"})
        for model in MODELS:
            dev.purchase_model(model["id"])
        code, out, err = cls.home.run("login", "--email", DEV_EMAIL, "--password-stdin", stdin=PASSWORD + "\n")
        assert code == 0, (out, err)

    @classmethod
    def tearDownClass(cls):
        cls.home.close()
        cls.cloud.close()

    def test_nx_chat_stream_prints_text_then_metering(self):
        code, out, err = self.home.run("chat", "echo-chat", PROMPT, "--stream", "--max-tokens", "500")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], EXPECTED_TEXT)
        self.assertIn(f"计量：本次 {EXPECTED_TOKENS} token，会话已用 {EXPECTED_TOKENS}/500，已计量", out)
        self.assertIn("已关闭：用量 21 token", out)
        self.assertIn("测试替身", out)
        session_id = out.split("会话 ", 2)[-1].split(" ", 1)[0]
        shown = self.home.json("sessions", "show", session_id)
        self.assertEqual((shown["status"], shown["used_tokens"], shown["calls"]), ("closed", EXPECTED_TOKENS, 1))
        code, text, _ = self.home.run("sessions", "show", session_id)
        self.assertIn(f"已用 {EXPECTED_TOKENS}/500 token", text)
        self.assertIn("关闭结果：用量 21 token", text)

    def test_nx_chat_non_stream_responses_and_json(self):
        data = self.home.json("chat", "echo-chat", PROMPT, "--api", "responses")
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], EXPECTED_TEXT)
        self.assertEqual((data["metering"]["source"], data["metering"]["call_tokens"]), ("response", EXPECTED_TOKENS))
        self.assertEqual(data["close"]["status"], "closed")
        data = self.home.json("chat", "echo-chat", PROMPT, "--stream")
        self.assertEqual((data["text"], data["metering"]["status"], data["close"]["usage_tokens"]),
                         (EXPECTED_TEXT, "succeeded", EXPECTED_TOKENS))

    def test_nx_chat_stream_error_still_closes_and_prints_metering(self):
        code, out, err = self.home.run("chat", "echo-overuse", PROMPT, "--stream", "--max-tokens", "200")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("echo: one"))                     # partial text was printed
        self.assertIn("output_limit_reached", err)
        self.assertIn("结果未知（按预留上限计入，待运营核对）", err)
        self.assertIn("reconciliation_required", err)
        data = self.home.json("chat", "echo-overuse", PROMPT, "--stream", "--max-tokens", "200", expect=1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], "output_limit_reached")
        self.assertEqual(data["close"]["status"], "reconciliation_required")

    def test_nx_chat_stream_prints_incrementally_in_a_real_process(self):
        env = dict(os.environ, NEUXMIND_HOME=str(self.home.path), PYTHONPATH=str(SDK_DIR),
                   PYTHONUNBUFFERED="0")
        for key in ("NEUXMIND_API_KEY", "NEUXMIND_CLOUD_URL"):
            env.pop(key, None)
        proc = subprocess.Popen([sys.executable, "-m", "neuxmind", "chat", "echo-crawl", "a b c d e f", "--stream"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        first = proc.stdout.read(5)                                     # "echo:" arrives before the rest exists
        self.assertEqual(first, b"echo:")
        self.assertIsNone(proc.poll())                                  # still streaming
        rest, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err.decode())
        text = (first + rest).decode()
        self.assertTrue(text.startswith("echo: a b c d e f\n"), text)
        self.assertIn("计量：本次 13 token", text)


if __name__ == "__main__":
    unittest.main()
