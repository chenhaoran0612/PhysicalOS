"""Deterministic stand-in for the Codex 0.146.1 app-server, for browser fixtures (ux03).

Why: the round-03/04 browser acceptances (cloud03, adaptation) were written
against the real pinned Codex binary. On machines without it
(``runtime/bin/codex``, ``.build/agent-runtime/bin/codex`` or
``NEUXMIND_TEST_AGENT_BINARY``) the fixtures either refused to start the
engineering engine ("工程引擎未安装", adaptation) or fell back to a replay that
only re-posted recorded requests and never ran the model's tool calls, so no
workspace file -- and no session artifact -- was ever produced (cloud03).

This stand-in replaces ``agent_runtime.AgentProcess`` in-process and speaks the
subset of the app-server protocol that ``brainos.engineering`` uses, with the
same tool loop Codex runs against a Responses endpoint:

* ``thread/start`` / ``thread/resume`` / ``turn/start`` / ``turn/interrupt``;
* each turn POSTs ``/responses`` (stream, Bearer key, Codex's recorded tool
  list plus the session's dynamic tools) to ``profile['base_url']`` and reads the
  SSE stream to its end;
* ``exec_command`` calls run locally with ``bash -c`` in the session workspace
  (bounded by a timeout, minimal environment), reported as ``commandExecution``
  items whose id is the call id, as Codex does;
* every other function call is a dynamic tool: sent to the product as an
  ``item/tool/call`` request and answered through :meth:`respond`;
* usage → ``thread/tokenUsage/updated``; final text → ``agentMessage``;
  HTTP errors use Codex's wording (``unexpected status 402 Payment Required: …``).

It is a test double: it trusts the fixture model's commands, which are written
by the tests themselves. It is never used by the product.
"""
import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_RECORDED = json.loads((ROOT / 'cloud/tests/fixtures/codex-0.146.1-responses.json').read_text())
# Codex's own (non-device) tools, as recorded from the real 0.146.1 runtime.
CODEX_TOOLS = [t for t in _RECORDED['requests'][0]['body']['tools'] if not str(t.get('name', '')).startswith(('device_', 'save_inventory'))]
CODEX_INSTRUCTIONS = _RECORDED['requests'][1]['body']['instructions']
# Codex's own developer text (sandbox / permission instructions) that precedes the product's instructions,
# and its environment-context user message; recorded from the engineering (workspace-write) request.
_PREAMBLE = _RECORDED['requests'][1]['body']['input']
CODEX_PERMISSIONS = [p for p in _PREAMBLE[0]['content'] if p.get('text', '').startswith('<permissions')]
CODEX_ENVIRONMENT = _PREAMBLE[1]['content'][0]['text']


def _exec_output(code, output):
    return f'Chunk ID: standin\nWall time: 0.0000 seconds\nProcess exited with code {code}\nOriginal token count: 0\nOutput:\n{output}'


class CodexStandIn:
    instances = []
    command_timeout = 120

    def __init__(self, home, work, profile, engineering=False, extensions=None, analysis_tools=False, cancel=None):
        self.profile, self.work = profile, Path(work)
        self.local_tools = bool(engineering or analysis_tools)
        self.events = queue.Queue(maxsize=4096)
        self.loaded_skills = []
        self.cancel = cancel
        self.closed = False
        self.interrupted = threading.Event()
        self.lock = threading.Lock()
        self.counter = 1000
        self.answers = {}
        self.dynamic_tools = []
        self.instructions = ''
        self.history = []
        self.usage = 0
        Path(home).mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        type(self).instances.append(self)

    # ------------------------------------------------------------ app-server protocol (subset)
    def request(self, method, params, timeout=25, cancel=None):
        if self.closed:
            raise ValueError('工程会话已关闭')
        if method in ('thread/start', 'thread/resume'):
            self.instructions = params.get('developerInstructions', '')
            if method == 'thread/start':
                self.dynamic_tools = [{'type': 'function', 'name': t['name'], 'description': t.get('description', ''),
                                       'strict': False, 'parameters': t.get('inputSchema', {'type': 'object'})}
                                      for t in params.get('dynamicTools') or []]
            return {'thread': {'id': params.get('threadId', 'thread-standin')}}
        if method == 'turn/start':
            text = '\n'.join(i.get('text', '') for i in params.get('input', []) if i.get('type') == 'text')
            self.history.append({'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]})
            self.interrupted.clear()
            threading.Thread(target=self._turn, daemon=True).start()
            return {'turn': {'id': 'turn-standin'}}
        if method == 'turn/interrupt':
            self.interrupted.set()
            return {}
        raise ValueError('工程引擎：unsupported ' + method)

    def respond(self, request_id, result):
        slot = self.answers.get(request_id)
        if slot is not None:
            slot.put(result)

    def send(self, value):
        if isinstance(value, dict) and 'id' in value and 'error' in value:
            self.respond(value['id'], {'success': False, 'contentItems': [{'type': 'inputText', 'text': str(value['error'])}]})

    def close(self):
        self.closed = True
        self.interrupted.set()

    # ------------------------------------------------------------ the tool loop
    def _emit(self, method, params, request_id=None):
        message = {'method': method, 'params': params}
        if request_id is not None:
            message['id'] = request_id
        self.events.put(message)

    def _post(self, body):
        req = urllib.request.Request(self.profile['base_url'].rstrip('/') + '/responses', data=json.dumps(body).encode(), method='POST',
                                     headers={'Authorization': 'Bearer ' + self.profile['api_key'], 'Content-Type': 'application/json',
                                              'Accept': 'text/event-stream'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as exc:
            raise ValueError(f'unexpected status {exc.code} {exc.reason}: {exc.read().decode()}, '
                             f"url: {self.profile['base_url']}/responses") from None
        events = []
        for block in raw.split('\n\n'):
            data = [line[6:] for line in block.split('\n') if line.startswith('data: ')]
            if not data or data[0] == '[DONE]':
                continue
            try:
                events.append(json.loads('\n'.join(data)))
            except ValueError:
                continue
        return events

    def _turn(self):
        status, error = 'completed', None
        try:
            for _ in range(20):
                if self.interrupted.is_set():
                    status = 'interrupted'
                    break
                # Like Codex: its base prompt as `instructions`, the developer instructions as the first input message.
                developer = {'type': 'message', 'role': 'developer',
                             'content': [{'type': 'input_text', 'text': self.instructions}] + CODEX_PERMISSIONS}
                environment = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': CODEX_ENVIRONMENT}]}
                body = {'model': self.profile['model'], 'instructions': CODEX_INSTRUCTIONS, 'input': [developer, environment] + self.history,
                        'tools': (CODEX_TOOLS if self.local_tools else []) + self.dynamic_tools, 'tool_choice': 'auto',
                        'parallel_tool_calls': False, 'store': False, 'stream': True}
                events = self._post(body)
                items, text = [], ''
                for e in events:
                    kind = e.get('type')
                    if kind == 'error' or e.get('code') and not kind:
                        raise ValueError('stream error: ' + str(e.get('code') or e.get('message')))
                    if kind == 'response.output_item.done':
                        items.append(e['item'])
                    elif kind == 'response.output_text.delta':
                        text += e.get('delta', '')
                        self._emit('item/agentMessage/delta', {'delta': e.get('delta', '')})
                    elif kind in ('response.completed', 'response.incomplete', 'response.failed'):
                        usage = (e.get('response') or {}).get('usage') or {}
                        last = int(usage.get('total_tokens') or 0)
                        self.usage += last
                        self._emit('thread/tokenUsage/updated', {'threadId': 'thread-standin', 'turnId': 'turn-standin',
                                                                 'tokenUsage': {'total': {'totalTokens': self.usage}, 'last': {'totalTokens': last}}})
                        if kind == 'response.failed':
                            raise ValueError('stream error: ' + json.dumps((e.get('response') or {}).get('error'))[:300])
                calls = [i for i in items if i.get('type') == 'function_call']
                for item in items:
                    if item.get('type') == 'message':
                        text = ''.join(p.get('text', '') for p in item.get('content') or [] if isinstance(p, dict)) or text
                if not calls:
                    self.history += [i for i in items if i.get('type') == 'message']
                    self._emit('item/completed', {'item': {'type': 'agentMessage', 'id': 'msg-standin', 'text': text}})
                    break
                for call in calls:
                    self.history.append(call)
                    output = self._call(call)
                    self.history.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': output})
            else:
                status, error = 'failed', 'stand-in: too many tool rounds'
            if self.interrupted.is_set():
                status = 'interrupted'
        except Exception as exc:  # noqa: BLE001 -- reported like the runtime does
            status, error = 'failed', str(exc)
        turn = {'id': 'turn-standin', 'status': status}
        if error:
            turn['error'] = {'message': error, 'codexErrorInfo': 'other'}
        self._emit('turn/completed', {'threadId': 'thread-standin', 'turn': turn})

    def _call(self, call):
        try:
            args = json.loads(call.get('arguments') or '{}')
        except ValueError:
            args = {}
        if call.get('name') == 'exec_command' and self.local_tools:
            command = str(args.get('cmd', ''))
            try:
                run = subprocess.run(['bash', '-c', command], cwd=self.work, capture_output=True, text=True,
                                     timeout=self.command_timeout, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(self.work)})
                code, output = run.returncode, (run.stdout + run.stderr)[-6000:]
            except subprocess.TimeoutExpired:
                code, output = 124, 'timed out'
            self._emit('item/completed', {'item': {'type': 'commandExecution', 'id': call['call_id'], 'command': command,
                                                   'exitCode': code, 'status': 'completed' if code == 0 else 'failed',
                                                   'aggregatedOutput': output}})
            return _exec_output(code, output)
        with self.lock:
            self.counter += 1
            request_id = self.counter
            slot = self.answers[request_id] = queue.Queue(maxsize=1)
        self._emit('item/tool/call', {'threadId': 'thread-standin', 'turnId': 'turn-standin', 'callId': call['call_id'],
                                      'tool': call.get('name'), 'arguments': args}, request_id)
        while True:
            if self.interrupted.is_set():
                return 'interrupted'
            try:
                result = slot.get(timeout=0.2)
                break
            except queue.Empty:
                continue
        self.answers.pop(request_id, None)
        return '\n'.join(c.get('text', '') for c in result.get('contentItems') or [] if isinstance(c, dict))
