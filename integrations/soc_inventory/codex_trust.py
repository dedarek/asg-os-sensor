"""Read-only native-trust audit for Codex managed hook blocks.

Codex fires a configured hook only when the config layer has a matching
[hooks.state."<key_source>:<event>:<group>:<index>"] entry whose trusted_hash
equals the hook's current normalized hash and whose enabled is not false.
The hash algorithm mirrors Codex (codex-rs hooks/src/engine/discovery.rs):
sha256 over canonical JSON (keys sorted, compact separators, UTF-8) of
{"event_name": <snake_case>, "matcher": <optional>, "hooks": [<normalized handler>]}
where a normalized command handler is
{"type":"command","command":...,"timeout":<normalized int>,"async":<bool>}
plus optional statusMessage/commandWindows when present. Timeouts are
normalized exactly like Codex: default 600 (max(1)); SessionEnd/Interrupt
default 1 clamped to [1,3]. Validated against a live Codex config: every
existing trusted_hash entry reproduces bit-exactly with this formula.

Presence of trusted_hash alone is NOT trusted: a wrong, stale or
post-modification hash must report "modified", never "trusted". Only the
target's own /hooks approval flow can produce trust; ASG never writes
hooks.state entries.
"""
import hashlib
import json
from pathlib import Path

MARKER = "security-hooks-codex"

EVENT_KEY = {"UserPromptSubmit": "user_prompt_submit", "PreToolUse": "pre_tool_use",
           "PostToolUse": "post_tool_use", "Stop": "stop",
           "SessionStart": "session_start", "SessionEnd": "session_end",
           "PermissionRequest": "permission_request", "PreCompact": "pre_compact",
           "PostCompact": "post_compact", "SubagentStart": "subagent_start",
           "SubagentStop": "subagent_stop", "Interrupt": "interrupt"}

# serde(tag="type") renames and defaults reproduced from hook_config.rs.
_DEFAULT_TIMEOUT = 600
_SESSION_END_DEFAULT_TIMEOUT = 1
_SESSION_END_MAX_TIMEOUT = 3


def _normalize_timeout(event_label, timeout):
    if event_label in ("session_end", "interrupt"):
        value = _SESSION_END_DEFAULT_TIMEOUT if timeout is None else timeout
        return max(_SESSION_END_DEFAULT_TIMEOUT, min(_SESSION_END_MAX_TIMEOUT, value))
    value = _DEFAULT_TIMEOUT if timeout is None else timeout
    return max(1, value)


def handler_hash(event_label, matcher, handler):
    """Codex hook_hash for one normalized command handler."""
    normalized = {"type": str(handler.get("type", "command"))}
    if "command" in handler:
        normalized["command"] = str(handler["command"])
    if handler.get("commandWindows") is not None:
        normalized["commandWindows"] = str(handler["commandWindows"])
    if handler.get("command_windows") is not None:
        normalized["commandWindows"] = str(handler["command_windows"])
    normalized["timeout"] = _normalize_timeout(event_label, handler.get("timeout"))
    normalized["async"] = bool(handler.get("async", False))
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = str(handler["statusMessage"])
    if handler.get("additionalContextLimit") is not None:
        normalized["additionalContextLimit"] = int(handler["additionalContextLimit"])
    identity = {"event_name": event_label, "hooks": [normalized]}
    if matcher is not None:
        identity["matcher"] = str(matcher)
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _parse_toml(src):
    import tomlkit
    document = tomlkit.parse(src)
    hooks = document.get("hooks")
    events = {}
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if event == "state" or not isinstance(groups, list):
                continue
            events[event] = [{"matcher": group.get("matcher"),
                              "handlers": [dict(h) for h in group.get("hooks", []) if isinstance(h, dict)]}
                             for group in groups if isinstance(group, dict)]
    state = {}
    if isinstance(hooks, dict) and isinstance(hooks.get("state"), dict):
        for key, entry in hooks["state"].items():
            if isinstance(entry, dict):
                state[str(key)] = {"enabled": entry.get("enabled"),
                                   "trusted_hash": entry.get("trusted_hash")}
    return events, state


def _parse_json(src):
    document = json.loads(src)
    hooks = document.get("hooks")
    events = {}
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if event == "state" or not isinstance(groups, list):
                continue
            events[event] = [{"matcher": group.get("matcher") if isinstance(group, dict) else None,
                              "handlers": [dict(h) for h in (group.get("hooks", []) if isinstance(group, dict) else []) if isinstance(h, dict)]}
                             for group in groups]
    state = {}
    raw_state = hooks.get("state") if isinstance(hooks, dict) else None
    if isinstance(raw_state, dict):
        for key, entry in raw_state.items():
            if isinstance(entry, dict):
                state[str(key)] = {"enabled": entry.get("enabled"),
                                   "trusted_hash": entry.get("trusted_hash")}
    return events, state


def audit(config_path):
    path = Path(config_path).expanduser()
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"status": "unreadable", "reason": type(exc).__name__, "blocks": []}
    try:
        if path.suffix == ".json":
            events, state = _parse_json(src)
        else:
            events, state = _parse_toml(src)
    except Exception as exc:
        return {"status": "unreadable", "reason": type(exc).__name__, "blocks": []}
    blocks = []
    counts = {"trusted": 0, "untrusted": 0, "disabled": 0, "modified": 0}
    for event, groups in events.items():
        label = EVENT_KEY.get(event, event.lower())
        for group_index, group in enumerate(groups):
            for handler_index, handler in enumerate(group["handlers"]):
                if MARKER not in str(handler.get("command", "")):
                    continue
                key = "%s:%s:%d:%d" % (path, label, group_index, handler_index)
                current_hash = handler_hash(label, group.get("matcher"), handler)
                entry = state.get(key)
                if entry is None or not entry.get("trusted_hash"):
                    verdict = "untrusted"
                elif entry.get("enabled") is False:
                    verdict = "disabled"
                elif str(entry.get("trusted_hash")) != current_hash:
                    verdict = "modified"
                else:
                    verdict = "trusted"
                counts[verdict] += 1
                blocks.append({"event": event, "group": group_index, "handler": handler_index,
                               "verdict": verdict, "state_key": key,
                               "enabled": (entry or {}).get("enabled"),
                               "current_hash": current_hash,
                               "trusted_hash_present": bool(entry and entry.get("trusted_hash"))})
    if not blocks:
        return {"status": "not_installed", "blocks": []}
    pending = counts["untrusted"] + counts["disabled"] + counts["modified"]
    if counts["trusted"] and not pending:
        status = "trusted"
    elif counts["trusted"]:
        status = "partially_trusted"
    else:
        status = "awaiting_user_trust"
    instructions = ""
    if status != "trusted":
        instructions = ("在 Codex 里运行 /hooks，审阅并信任 security-hooks-codex 的条目；"
                        "配置被修改过的条目需要重新批准。信任对新会话生效。"
                        "ASG 不代替用户批准，也不伪造信任。")
    return {"status": status, "hash_verified": True, **counts, "blocks": blocks,
            "action_required": status != "trusted", "instructions": instructions}


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
        adapter = agent.get("adapter") or {}
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
                        "detail": "已写入 %d 个回调，其中 %d 个尚未获得有效原生信任；"
                                  "信任后新开会话生效。安装写入不等于挂接成功。"
                                  % (len(report["blocks"]),
                                     report["untrusted"] + report["disabled"] + report["modified"]),
                    }
            elif report["status"] == "trusted":
                adapter["native_trust"] = report
    return snapshot
