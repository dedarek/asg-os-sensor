"""Standalone command-Hook runtime installed by an SOC deployment package.

The file is copied into the target workspace.  It talks to SOC through the
package's durable client and has no dependency on a running ASG service.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
import uuid

ASG_SOC_DIRECT_PROTOCOL = True
EVENTS = {'SessionStart': 'hook.loaded', 'UserPromptSubmit': 'user.input',
          'PreToolUse': 'tool.execute.before', 'PostToolUse': 'tool.execute.after',
          'Stop': 'assistant.output', 'BeforeAgent': 'user.input',
          'BeforeTool': 'tool.execute.before', 'AfterTool': 'tool.execute.after',
          'AfterAgent': 'assistant.output'}
SECRET = re.compile(r'token|secret|password|api[_-]?key|authorization|cookie', re.I)
ROOT = Path(__file__).resolve().parents[1]
LOG = Path(__file__).with_name('events.jsonl')
CLIENT = ROOT / '.soc-hook/runtime/hook_control_client.py'
CONFIG = ROOT / '.soc-hook/artifacts/autonomous-service/hook-control-client.json'


def clean(value, key='', depth=0):
    if depth > 20:
        return '[DEPTH_LIMIT]'
    if SECRET.search(key):
        return '[REDACTED]'
    if isinstance(value, str):
        value = re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', value, flags=re.I)
        raw = value.encode()
        return raw[:1024 * 1024].decode(errors='ignore') if len(raw) > 1024 * 1024 else value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [clean(item, key, depth + 1) for item in value[:1000]]
    if isinstance(value, dict):
        return {str(k): clean(v, str(k), depth + 1) for k, v in list(value.items())[:1000]}
    return str(value)


def call(action, body, timeout):
    result = subprocess.run([sys.executable, str(CLIENT), str(CONFIG), action],
                            input=json.dumps(body, ensure_ascii=False), text=True,
                            capture_output=True, timeout=timeout)
    try:
        payload = json.loads(result.stdout or '{}')
    except ValueError:
        payload = {}
    if result.returncode != 0:
        raise OSError(payload.get('reason') or 'SOC client failed')
    return payload


def emit(event):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    call('event', event, 5)


def main():
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return 2
    payload = json.loads(raw or b'{}')
    native = str(payload.get('hook_event_name') or '')
    kind = EVENTS.get(native)
    if not kind:
        return 0
    call_id = payload.get('tool_use_id') or payload.get('tool_call_id')
    content = (payload.get('prompt') if kind == 'user.input' else
               payload.get('last_assistant_message') or payload.get('prompt_response')
               if kind == 'assistant.output' else payload.get('tool_input')
               if kind == 'tool.execute.before' else payload.get('tool_response')
               if kind == 'tool.execute.after' else None)
    event = {'event': kind, 'pid': os.getppid(),
             'timestamp': datetime.now(timezone.utc).isoformat(),
             'event_id': uuid.uuid4().hex, 'session_id': payload.get('session_id'),
             'tool_call_id': call_id, 'call_id': call_id,
             'tool': payload.get('tool_name'), 'content': clean(content),
             'content_complete': content is not None, 'capture_layer': 'command_hooks'}
    emit(event)
    if kind != 'tool.execute.before':
        return 0
    try:
        decision = call('decision', {'pid': event['pid'], 'session_id': event['session_id'],
                        'call_id': call_id or event['event_id'], 'tool': event['tool'],
                        'input': event['content']}, 65)
        allowed = decision.get('decision') == 'allow'
        call('ack', {'request_id': decision.get('request_id'),
                     'decision': 'allow' if allowed else 'deny', 'tool': event['tool'],
                     'call_id': call_id, 'outcome': 'decision_returned'}, 5)
    except (OSError, ValueError, subprocess.SubprocessError):
        allowed = False
    response = {'hookSpecificOutput': {'hookEventName': native,
                'permissionDecision': 'allow' if allowed else 'deny',
                'permissionDecisionReason': 'SOC execution policy'}}
    if not allowed:
        response.update({'decision': 'block', 'reason': 'SOC execution policy unavailable or denied'})
    print(json.dumps(response, ensure_ascii=False))
    return 0 if allowed else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit(2)
