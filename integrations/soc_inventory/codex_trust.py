"""Read-only native-trust audit for Codex managed hook blocks.

Codex fires a configured hook only when config.toml has a matching
`[hooks.state."<config>:<event>:<group>:<index>"]` entry with enabled=true and
a trusted_hash written by the user through /hooks (or a per-invocation bypass
flag).  Writing managed hook blocks into config.toml therefore does NOT mean
callbacks will fire, so onboarding status must reflect trust, not file writes.

Group numbers in state keys are positional per event type: the Nth
`[[hooks.Event]]` block in file order, 0-based.
"""
import re
from pathlib import Path

MARKER = "security-hooks-codex"

# Codex writes hooks.state keys with snake_case event names.
EVENT_KEY = {"UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
           "PostToolUse": "post_tool_use", "Stop": "stop",
           "SessionStart": "session_start", "SessionEnd": "session_end",
           "PermissionRequest": "permission_request"}


def parse_hook_blocks(src):
    out, counters, pending = [], {}, None
    for line in src.splitlines():
        s = line.strip()
        m = re.match(r"\[\[hooks\.([A-Za-z]+)\]\]$", s)
        if m:
            event = m.group(1)
            idx = counters.get(event, 0)
            counters[event] = idx + 1
            pending = (event, idx)
            continue
        m = re.match(r'command\s*=\s*"(.*?)"\s*(?:#.*)?$', s)
        if m and pending is not None:
            out.append((pending[0], pending[1], m.group(1).replace(chr(92)+chr(34), chr(34))))
            pending = None
    return out


def parse_state(src):
    state, current = {}, None
    for line in src.splitlines():
        s = line.strip()
        m = re.match(r'\[hooks\.state\."(.+)"\]$', s)
        if m:
            current = m.group(1)
            state[current] = {}
            continue
        if s.startswith("["):
            current = None
            continue
        if current is None:
            continue
        m = re.match(r"(enabled|trusted_hash)\s*=\s*(.+)$", s)
        if m:
            if m.group(1) == "enabled":
                state[current]["enabled"] = m.group(2).strip() == "true"
            else:
                state[current]["trusted_hash"] = m.group(2).strip().strip(chr(34))
    return state


def audit(config_path):
    path = Path(config_path).expanduser()
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"status": "unreadable", "reason": type(exc).__name__, "blocks": []}
    state = parse_state(src)
    blocks = []
    trusted = untrusted = disabled = 0
    for event, idx, command in parse_hook_blocks(src):
        if MARKER not in command:
            continue
        key = "%s:%s:%d:0" % (path, EVENT_KEY.get(event, event.lower()), idx)
        entry = state.get(key)
        if entry is None or not entry.get("trusted_hash"):
            verdict = "untrusted"
            untrusted += 1
        elif entry.get("enabled") is False:
            verdict = "disabled"
            disabled += 1
        else:
            verdict = "trusted"
            trusted += 1
        blocks.append({"event": event, "group": idx, "verdict": verdict,
                       "state_key": key,
                       "enabled": (entry or {}).get("enabled")})
    if not blocks:
        return {"status": "not_installed", "blocks": []}
    if trusted and not untrusted and not disabled:
        status = "trusted"
    elif trusted:
        status = "partially_trusted"
    else:
        status = "awaiting_user_trust"
    return {"status": status, "trusted": trusted, "untrusted": untrusted,
            "disabled": disabled, "blocks": blocks,
            "action_required": status != "trusted",
            "instructions": ("在 Codex 里运行 /hooks，审阅并信任 security-hooks-codex 的条目；"
                             "信任对新会话生效。ASG 不代替用户批准，也不伪造信任。"
                             if status != "trusted" else "")}


def codex_home_configs(agent):
    env = (agent.get("environ") or {})
    home = env.get("CODEX_HOME") or ""
    out = []
    if home:
        p = Path(home) / "config.toml"
        if p.is_file():
            out.append(str(p))
    p = Path.home() / ".codex" / "config.toml"
    if p.is_file() and str(p) not in out:
        out.append(str(p))
    return out


def _is_codex(agent):
    """True only for real Codex runtimes (excludes look-alikes such as OpenCodex)."""
    identity = agent.get("identity") or {}
    name = str(identity.get("name") or "")
    entry = str(identity.get("entrypoint") or "") + str(identity.get("resolved_entrypoint") or "")
    if entry:
        low = entry.lower()
        return low.endswith("/codex") or "/resources/codex" in low
    if name.lower() == "codex":
        return True
    try:
        import psutil
        return Path(psutil.Process(agent["pid"]).exe()).name.lower() == "codex"
    except Exception:
        return False


def attach(snapshot):
    reports = {}
    for agent in snapshot.get("agents", []):
        is_codex = _is_codex(agent)
        adapter = agent.get("adapter")
        if not (is_codex and isinstance(adapter, dict)):
            continue
        has_managed = (adapter.get("hook_state")
                       or ((adapter.get("onboarding") or {}).get("plan") or {}))
        if not has_managed:
            continue
        for cfg in codex_home_configs(agent):
            if cfg not in reports:
                reports[cfg] = audit(cfg)
            report = reports[cfg]
            if report["status"] in ("awaiting_user_trust", "partially_trusted"):
                adapter["native_trust"] = report
                if not adapter.get("user_action"):
                    adapter["user_action"] = {
                        "status": "approval_required",
                        "title": "需要在 Codex 里信任 Hook（/hooks）",
                        "instructions": report["instructions"],
                        "detail": "已写入 %d 个回调，其中 %d 个尚未获得原生信任；"
                                  "信任后新开会话生效。安装写入不等于挂接成功。"
                                  % (len(report["blocks"]),
                                     report["untrusted"] + report["disabled"]),
                    }
            elif report["status"] == "trusted":
                adapter["native_trust"] = report
    return snapshot
