"""Reusable command/JSON Hook endpoint, installed without an LLM.

Runs as a child of the target. Shared configs identify each live ancestor from
an explicit binding list; neither PID reuse nor another instance inherits it.
Stdout is reserved for the host Hook protocol. No transcript file is guessed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import psutil

# Structured event-name dialects that share this payload shape. Aliases come
# from hosts whose own migration tooling converts between the two spellings;
# recognition stays structural (exact keys), never product-name inference.
EVENTS = {'SessionStart': 'hook.loaded', 'UserPromptSubmit': 'user.input',
          'PreToolUse': 'tool.execute.before', 'PostToolUse': 'tool.execute.after',
          'Stop': 'assistant.output',
          'BeforeAgent': 'user.input', 'BeforeTool': 'tool.execute.before',
          'AfterTool': 'tool.execute.after', 'AfterAgent': 'assistant.output'}


def handle(payload, config, target, *, decide=None, emit=None):
    """Pure injectable protocol boundary; production identity is resolved below."""
    native = payload.get('hook_event_name')
    kind = EVENTS.get(native)
    if not kind: return {}, 0
    call = payload.get('tool_use_id') or payload.get('tool_call_id')
    content = (payload.get('prompt') if kind == 'user.input' else
               payload.get('last_assistant_message') or payload.get('prompt_response')
               if kind == 'assistant.output' else
               payload.get('tool_input') if kind == 'tool.execute.before' else
               payload.get('tool_response') if kind == 'tool.execute.after' else None)
    event = {'event': kind, **target, 'timestamp': time.time(), 'event_id': uuid.uuid4().hex,
             'session_id': payload.get('session_id'), 'tool_call_id': call, 'call_id': call,
             'tool': payload.get('tool_name'), 'content': content,
             'content_complete': content is not None, 'capture_layer': 'command_hooks',
             'native_payload': payload}
    if kind != 'hook.loaded':
        emit({'event': 'hook.loaded', **target, 'timestamp': time.time(),
              'source': 'command_hook_callback_executed', 'session_id': payload.get('session_id')})
    emit(event)
    if kind != 'tool.execute.before' or not config.get('control_client'): return {}, 0
    try:
        decision = decide({**target, 'call_id': call or event['event_id'],
                           'tool': payload.get('tool_name'), 'input': payload.get('tool_input')})
        allowed = decision.get('decision') == 'allow'
    except (OSError, ValueError, subprocess.SubprocessError):
        allowed = False
    # The response mirrors the dialect the host just used (its own event name).
    # Both structured variants treat a missing decision as allow, so the
    # blocking form is emitted only on refusal; either way this is only our
    # declared answer, not proof the host obeys.
    response = {'hookSpecificOutput': {'hookEventName': native,
                'permissionDecision': 'allow' if allowed else 'deny',
                'permissionDecisionReason': 'ASG execution policy'}}
    if not allowed:
        response.update({'decision': 'block', 'reason': 'ASG execution policy'})
    return response, 0 if allowed else 2


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    ancestors = {p.pid: p for p in psutil.Process().parents()}
    target = None
    for candidate in reversed(config['targets']):
        process = ancestors.get(candidate['pid'])
        if process and abs(process.create_time() - candidate['create_time']) < .001:
            target = candidate
            break
    if target is None: return 0  # A shared settings file can serve unrelated hosts.
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024: return 2
    payload = json.loads(raw)
    def emit(event):
        # One append while locked avoids interleaving concurrent Hook processes.
        fd = os.open(config['log_path'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            # Use a separate lock byte: Windows byte-range locks cannot lock
            # a zero-length event file and must not follow the append cursor.
            with open(config['log_path'] + '.lock', 'a+b') as lock:
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(lock.fileno()).st_size == 0:
                        lock.write(b'0'); lock.flush()
                    lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                    stream.flush()
                finally:
                    if os.name == 'nt':
                        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    def decide(request):
        result = subprocess.run(config['control_client'], input=json.dumps(request),
                                capture_output=True, text=True, timeout=65, check=True)
        return json.loads(result.stdout)
    response, code = handle(payload, config, target, decide=decide, emit=emit)
    if response: print(json.dumps(response))
    return code


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, KeyError, psutil.Error): sys.exit(2)
