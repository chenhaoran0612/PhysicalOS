"""Round 04, G-2: a user stop has a product-side upper bound.

Before this round a stop was only seen by the session loop between events, so
a session blocked in a synchronous runtime RPC (``thread/start``,
``turn/start``, ``turn/interrupt``) or in a tool call kept running until that
call returned or timed out (25 s per RPC, more for SSH), and an unresponsive
runtime could hold a stop indefinitely. Now:

* every synchronous RPC wait is cancelled by the stop (``StopRequested``);
* ``turn/interrupt`` that gets no answer kills the runtime process group;
* a watchdog started by ``stop()`` kills the runtime after
  ``STOP_DEADLINE_SECONDS`` and, if the run thread is still blocked 3 s later,
  releases the session and marks it ``stop_forced``.

These tests use the real Codex 0.146.1 app-server (``NEUXMIND_TEST_AGENT_BINARY``)
against a local Responses double. "Unresponsive" is produced with SIGSTOP on
the real runtime process, not with a mock of the runtime.
"""
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_binary  # noqa: F401
from brainos import agent_runtime, engineering
from brainos.service import Platform
from test_engineering import ResponsesFixture
from test_product import Server


class SlowModel(ResponsesFixture):
    requests=[];engineering=True;delay=False
    gate=threading.Event()
    def do_POST(self):
        if type(self).delay:type(self).gate.wait(20)
        return super().do_POST()


@unittest.skipUnless(agent_runtime.binary_path(),'Packaged agent binary required')
class StopUpperBoundTests(unittest.TestCase):
    def setUp(self):
        SlowModel.requests=[];SlowModel.delay=False;SlowModel.gate=threading.Event()
        self.tmp=tempfile.TemporaryDirectory();self.platform=Platform(self.tmp.name);self.engine=self.platform.engineering
        self.server=Server(SlowModel);self.release=threading.Event()
        self.pid=self.platform.add_project({'name':'Stop bound','objective':'G-2'})['id']
        self.model=self.engine.model_add({'project_id':self.pid,'name':'fixture','model':'fixture','base_url':self.server.url+'/v1','api_key':'fixture-key'})

    def tearDown(self):
        self.release.set();SlowModel.gate.set()
        self.platform.close();self.server.close();self.tmp.cleanup()

    def start(self):
        return self.engine.start_work({'project_id':self.pid,'model_id':self.model['id'],'name':'Stop bound','objective':'Create adapter.py',
                                       'data_authorized':True,'max_tokens':5000,'max_minutes':2})

    def wait(self,predicate,seconds=20,label='condition'):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.02)
        self.fail('timeout waiting for '+label)

    def released(self,sid):
        with self.engine.lock:return sid not in self.engine.live

    def stop_and_measure(self,sid):
        started=time.monotonic();self.engine.stop({'session_id':sid})
        self.wait(lambda:self.released(sid),40,'session release')
        return time.monotonic()-started,self.platform.store.get('agent_sessions',sid)

    def test_stop_cancels_a_blocked_thread_start_rpc(self):
        """The real runtime is frozen right when thread/start is sent."""
        original=agent_runtime.AgentProcess.request;frozen=threading.Event()
        def request(process,method,params,**kw):
            if method=='thread/start':os.killpg(process.process.pid,signal.SIGSTOP);frozen.set()
            return original(process,method,params,**kw)
        with patch.object(agent_runtime.AgentProcess,'request',request):
            s=self.start();self.wait(frozen.is_set,20,'thread/start')
            elapsed,s=self.stop_and_measure(s['id'])
        self.assertEqual(s['status'],'interrupted',s.get('error'))
        self.assertNotIn('stop_forced',s)                     # cancelled, not killed by the watchdog
        self.assertLess(elapsed,engineering.STOP_DEADLINE_SECONDS,elapsed)
        self.assertIn('stop_latency_ms',s)
        self.assertTrue(any('thread/start' in e['message'] for e in s['events'] if e['kind']=='stopped'))

    def test_unresponsive_runtime_during_a_turn_is_killed_within_the_bound(self):
        """Mid-turn the real runtime stops answering: turn/interrupt gets no
        reply. Before G-2 this ended as 'failed' after the RPC timeout."""
        SlowModel.delay=True
        s=self.start();sid=s['id']
        self.wait(lambda:self.platform.store.get('agent_sessions',sid).get('turn_id'),20,'turn/start')
        with self.engine.lock:process=self.engine.live[sid]['process']
        os.killpg(process.process.pid,signal.SIGSTOP)
        elapsed,s=self.stop_and_measure(sid)
        self.assertEqual(s['status'],'interrupted',s.get('error'))
        self.assertTrue(s.get('stop_forced'),s)
        self.assertLess(elapsed,engineering.STOP_DEADLINE_SECONDS,elapsed)
        self.assertIsNotNone(process.process.poll(),'runtime process must be gone')
        self.assertTrue(any(e['kind']=='stop_forced' for e in s['events']))

    def test_watchdog_releases_a_run_thread_that_ignores_the_stop(self):
        """The run thread is stuck in a call that never looks at the stop
        (here: a blocking product call after turn/start). The watchdog kills
        the runtime at the deadline, then releases and marks the session."""
        original=engineering.Engineering.event;stuck=threading.Event()
        def event(engine,sid,kind,message,**details):
            original(engine,sid,kind,message,**details)
            if kind=='started':stuck.set();self.release.wait(60)
        with patch.object(engineering,'STOP_DEADLINE_SECONDS',1),patch.object(engineering.Engineering,'event',event):
            s=self.start();sid=s['id'];self.wait(stuck.is_set,20,'turn started')
            with self.engine.lock:handle=self.engine.live[sid]
            elapsed,s=self.stop_and_measure(sid)
        self.assertLess(elapsed,1+3+2,elapsed)
        self.assertEqual(s['status'],'interrupted');self.assertTrue(s['stop_forced'])
        self.assertIn('强制终止',s['error'])
        self.assertIsNotNone(handle['process'].process.poll(),'runtime process must be gone')
        # A new session can start: the stuck one no longer holds a slot.
        self.release.set();handle['thread'].join(10)
        later=self.platform.store.get('agent_sessions',sid)
        self.assertEqual(later['status'],'interrupted',later)      # the late finally keeps the stop
        self.assertTrue(later['stop_forced'])

    def test_repeated_stop_requests_start_one_watchdog(self):
        SlowModel.delay=True
        s=self.start();sid=s['id']
        self.wait(lambda:self.platform.store.get('agent_sessions',sid).get('turn_id'),20,'turn/start')
        before=threading.active_count()
        for _ in range(5):
            try:self.engine.stop({'session_id':sid})
            except ValueError:pass
        self.assertLessEqual(len([t for t in threading.enumerate() if t.name=='nx-stop-watchdog']),1)
        SlowModel.gate.set()
        self.wait(lambda:self.released(sid),20,'release')
        s=self.platform.store.get('agent_sessions',sid)
        self.assertEqual(s['status'],'interrupted');self.assertNotIn('stop_forced',s)
        del before


if __name__=='__main__':unittest.main()
