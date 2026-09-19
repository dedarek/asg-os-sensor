"""Transparent ACP v1 stdio bridge. Run between an ACP client and its Agent.

Owns the child transport; never pretends to attach to an existing desktop
session. No model, tool or filesystem operations are synthesized by the bridge.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psutil
from runtime.file_lock import _FileLock
from runtime.learned_install import _atomic
from runtime.observation_registry import Registry


class Recorder:
    def __init__(self, target, log_path):
        self.target, self.log_path = target, Path(log_path)
        self.pending, self.turns = {}, {}
        self.lock = threading.RLock()

    def emit(self, event, **fields):
        row = {'event': event, **self.target, 'timestamp': time.time(), 'event_id': uuid.uuid4().hex, **fields}
        with _FileLock(self.log_path):
            with self.log_path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(row, ensure_ascii=False) + '\n')

    def observe(self, message, direction):
        if not isinstance(message, dict): return
        with self.lock:
            method, params = message.get('method'), message.get('params') or {}
            if direction == 'client' and method in ('initialize', 'session/prompt'):
                key = json.dumps(message.get('id'))
                self.pending[key] = {'method': method, 'session': params.get('sessionId')}
                if method == 'session/prompt':
                    self.turns[key] = []
            if direction == 'agent' and method == 'session/update':
                update = params.get('update') or {}
                if update.get('sessionUpdate') == 'agent_message_chunk':
                    content = update.get('content') or {}
                    if content.get('type') == 'text':
                        for key, value in self.pending.items():
                            if value['method'] == 'session/prompt' and value['session'] == params.get('sessionId'):
                                self.turns[key].append(content.get('text', ''))
            if direction == 'agent' and 'id' in message and not method:
                key = json.dumps(message['id'])
                call = self.pending.pop(key, None)
                if call and call['method'] == 'initialize':
                    result = message.get('result') or {}
                    if result.get('protocolVersion') == 1 and isinstance(result.get('agentCapabilities'), dict):
                        self.emit('hook.loaded', protocol='acp', handshake=result)
                if call and call['method'] == 'session/prompt':
                    text = ''.join(self.turns.pop(key, []))
                    self.emit('assistant.output', session_id=call['session'], content=text or None,
                              content_complete=bool(text) and (message.get('result') or {}).get('stopReason') == 'end_turn',
                              capture_layer='acp_reassembled', rpc_id=message['id'])
            self.emit('acp.message', protocol_message=message, direction=direction)


def permission_response(message, decision):
    options = (message.get('params') or {}).get('options') or []
    kind = 'allow_once' if decision == 'allow' else 'reject_once'
    option = next((o.get('optionId') for o in options if o.get('kind') == kind), None)
    outcome = {'outcome': 'selected', 'optionId': option} if option else {'outcome': 'cancelled'}
    return {'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': outcome}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--control-config', help='Optional ASG synchronous decision client configuration')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command: parser.error('Agent ACP command required after --')
    run = Path(args.run_dir).resolve(); run.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
    target = {'pid': child.pid, 'create_time': psutil.Process(child.pid).create_time()}
    folder = run / 'acp' / str(child.pid); folder.mkdir(parents=True, exist_ok=True)
    log = folder / 'events.jsonl'
    binding = folder / 'binding.json'
    _atomic(binding, json.dumps({'target': target, 'log_path': str(log),
        'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp', 'call_id': 'tool_call_id'}}).encode())
    Registry(run).register(binding, target, source_name='acp', make_primary=True)
    recorder = Recorder(target, log)
    writer = threading.Lock()
    def send(raw):
        with writer:
            child.stdin.write(raw); child.stdin.flush()
    def from_client():
        try:
            for raw in sys.stdin.buffer:
                try: recorder.observe(json.loads(raw), 'client')
                except (ValueError, TypeError): pass
                send(raw)
        except (BrokenPipeError, OSError): pass
        finally:
            try: child.stdin.close()
            except OSError: pass
    threading.Thread(target=from_client, daemon=True).start()
    try:
        for raw in child.stdout:
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    recorder.observe(message, 'agent')
                    if message.get('method') == 'session/request_permission' and args.control_config:
                        params = message.get('params') or {}; call = params.get('toolCall') or {}
                        request = {**target, 'call_id': call.get('toolCallId') or str(message['id']),
                                   'tool': call.get('title') or call.get('kind') or 'acp.tool', 'input': call.get('rawInput')}
                        try:
                            result = subprocess.run([sys.executable, str(Path(__file__).with_name('hook_control_client.py')),
                                args.control_config, 'decision'], input=json.dumps(request), text=True, capture_output=True, timeout=65, check=True)
                            decision = json.loads(result.stdout).get('decision')
                        except (ValueError, OSError, subprocess.SubprocessError): decision = 'deny'
                        recorder.emit('permission.decision', session_id=params.get('sessionId'),
                                      tool_call_id=call.get('toolCallId'), decision=decision,
                                      enforcement='acp_permission_response', effect_verified=False)
                        if decision != 'allow':
                            send((json.dumps(permission_response(message, 'deny')) + '\n').encode())
                            continue
                        # ASG allow preserves the original client's permission UI.
            except (ValueError, TypeError): pass
            sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == '__main__': sys.exit(main())
