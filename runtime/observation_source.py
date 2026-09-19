# -*- coding: utf-8 -*-
"""Generic file-backed observation source.

Consumes a JSONL file that a hook appends to, using an *external* binding
config that declares the target instance (pid + create_time), the log path and
the field mapping. Nothing here knows a product or a specific runtime: the raw
event names are configuration, so the same adapter serves any producer.

Truth rules, mirroring the rest of Stage1:
- A bound pid is not proof of health. Liveness is re-checked against the live
  process create_time on every read, and a dead/replaced process is reported as
  not healthy instead of inheriting a historical snapshot.
- Structurally broken lines and events bound to another instance count as
  invalid; unrelated hook output is ignored, never inflated into an event.
- Blocking is declared unsupported. This source records observation only.
"""
from __future__ import annotations

import json
import math
import hashlib
import os
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from runtime import event_vocabulary

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a runtime dependency
    psutil = None

MAX_READ_CHUNK_BYTES = 4 * 1024 * 1024
MAX_PARTIAL_BYTES = 64 * 1024
MAX_EVENTS = 40
MAX_PAIRED = 40
CREATE_TIME_TOLERANCE = 1e-3
# An event may not predate the bound instance's own create time (with a small
# tolerance for sub-second rounding between the producer clock and psutil).
EVENT_TIME_TOLERANCE_S = 1.0
SCHEMA_VERSION = 1

# Canonical vocabulary shared with the existing observation contract.
CANONICAL_EVENTS = ("hook.loaded", "tool.execute.before", "tool.execute.after",
                    "user.input", "assistant.output", "model.request", "model.response",
                    "session.start", "session.end", "tool.error", "control.applied")
DEFAULT_FIELDS = {"event": "event", "pid": "pid", "timestamp": "ts",
                  "tool": "tool", "call_id": "callID"}
BLOCKING_UNSUPPORTED = {"status": "unsupported", "label": "未支持"}

# A candidate may describe how its own Hook writes events, but it may never
# decide which instance it is bound to: pid/create_time come from the executor.
DECLARATION_KEYS = ("log_path", "fields", "event_names")
REQUIRED_FIELD_ROLES = ("event", "pid", "timestamp")
FORBIDDEN_DECLARATION_KEYS = ("target", "pid", "create_time")
# Legacy configs may omit roles and inherit the built-in names. A config written
# from a candidate declaration is ``explicit``: only the roles it declared exist,
# so saving and re-reading never resurrects a role the candidate never named.
DEFAULT_MAPPING_MODE = "defaults"
EXPLICIT_MAPPING_MODE = "explicit"
MAPPING_MODES = (DEFAULT_MAPPING_MODE, EXPLICIT_MAPPING_MODE)

_STATE: dict[str, dict[str, Any]] = {}
_STATE_LOCK = threading.RLock()


def load_config(path: Path | str) -> dict[str, Any]:
    """Read and validate an external observation binding config."""
    config_path = Path(path).expanduser()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("observation config unreadable: %s" % type(exc).__name__) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("observation config is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("observation config must be an object")
    target = value.get("target")
    if not isinstance(target, dict) or target.get("pid") is None or target.get("create_time") is None:
        raise ValueError("observation config requires target.pid and target.create_time")
    try:
        pid = int(target["pid"])
        create_time = float(target["create_time"])
    except (TypeError, ValueError) as exc:
        raise ValueError("observation config target pid/create_time must be numeric") from exc
    if pid <= 0:
        raise ValueError("observation config target.pid must be positive")
    log_path = value.get("log_path")
    if not isinstance(log_path, str) or not log_path.strip():
        raise ValueError("observation config requires log_path")
    resolved = Path(log_path).expanduser()
    if not resolved.is_absolute():
        raise ValueError("observation config log_path must be absolute")
    mode = value.get("mapping_mode", DEFAULT_MAPPING_MODE)
    if mode not in MAPPING_MODES:
        raise ValueError("observation config mapping_mode must be one of: " + ", ".join(MAPPING_MODES))
    configured = value.get("fields")
    if mode == EXPLICIT_MAPPING_MODE:
        # Explicit mode never inherits a name the declaration did not state.
        fields = _validate_mapping(configured, tuple(DEFAULT_FIELDS), "observation config fields")
    else:
        fields = dict(DEFAULT_FIELDS)
        if configured is not None:
            if not isinstance(configured, dict):
                raise ValueError("observation config fields must be an object")
            for key, name in configured.items():
                if key not in DEFAULT_FIELDS:
                    raise ValueError("unknown observation field mapping: " + str(key))
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("observation field mapping values must be non-empty strings")
                fields[key] = name.strip()
    missing = [role for role in REQUIRED_FIELD_ROLES if role not in fields]
    if missing:
        raise ValueError("observation config fields must declare: " + ", ".join(missing))
    event_names = {name: name for name in CANONICAL_EVENTS}
    declared = value.get("event_names")
    if declared is not None:
        if not isinstance(declared, dict):
            raise ValueError("observation config event_names must be an object")
        for key, name in declared.items():
            if key not in CANONICAL_EVENTS:
                raise ValueError("unknown canonical event name: " + str(key))
            if not isinstance(name, str) or not name.strip():
                raise ValueError("observation event_names values must be non-empty strings")
            event_names[key] = name.strip()
    return {
        "version": SCHEMA_VERSION,
        "mapping_mode": mode,
        "path": str(config_path),
        "target": {"pid": pid, "create_time": create_time},
        "log_path": str(resolved),
        "fields": fields,
        "event_names": event_names,
    }


def _validate_mapping(value: Any, allowed_keys, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % label)
    result: dict[str, str] = {}
    for key, name in value.items():
        if key not in allowed_keys:
            raise ValueError("unknown %s key: %s (allowed: %s)"
                             % (label, key, ", ".join(allowed_keys)))
        if not isinstance(name, str) or not name.strip():
            raise ValueError("%s values must be non-empty strings" % label)
        result[key] = name.strip()
    if len(set(result.values())) != len(result):
        raise ValueError("%s must not map two roles to the same field" % label)
    return result


def validate_declaration(declaration: Any) -> dict[str, Any]:
    """Validate a candidate's observation-source declaration in isolation.

    The declaration describes how the candidate's own Hook writes its log:
    a workspace-relative ``log_path`` plus the field/event names it really
    emits. It must never carry an instance binding; ``pid``/``create_time`` are
    bound by the executor at install time.
    """
    if not isinstance(declaration, dict):
        raise ValueError("observation_source must be an object")
    for key in FORBIDDEN_DECLARATION_KEYS:
        if key in declaration:
            raise ValueError("observation_source must not declare %s; the executor binds the target instance"
                             % key)
    unknown = sorted(set(declaration) - set(DECLARATION_KEYS))
    if unknown:
        raise ValueError("unknown observation_source keys: " + ", ".join(unknown))
    log_path = declaration.get("log_path")
    if not isinstance(log_path, str) or not log_path.strip():
        raise ValueError("observation_source.log_path is required")
    log_path = log_path.strip()
    if "\\" in log_path or "\x00" in log_path or log_path.startswith("/"):
        raise ValueError("observation_source.log_path must be workspace-relative")
    parts = log_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("observation_source.log_path must stay inside the workspace")
    if PurePosixPath(log_path).is_absolute():
        raise ValueError("observation_source.log_path must be workspace-relative")
    fields = _validate_mapping(declaration.get("fields"), tuple(DEFAULT_FIELDS), "observation_source.fields")
    missing = [role for role in REQUIRED_FIELD_ROLES if role not in fields]
    if missing:
        raise ValueError("observation_source.fields must declare: " + ", ".join(missing))
    event_names = {name: name for name in CANONICAL_EVENTS}
    if declaration.get("event_names") is not None:
        event_names.update(_validate_mapping(declaration["event_names"], CANONICAL_EVENTS,
                                             "observation_source.event_names"))
    return {"log_path": log_path, "fields": fields, "event_names": event_names}


def candidate_to_config(declaration: Any, workspace: Path | str, target: dict[str, Any]) -> dict[str, Any]:
    """Pure conversion: candidate declaration + executor binding -> source config.

    ``workspace`` and ``target`` are supplied by the caller (the executor), never
    read from the candidate. The produced ``log_path`` is proven to resolve
    inside the approved workspace before it is returned.
    """
    normalized = validate_declaration(declaration)
    root = Path(workspace).expanduser()
    if not root.is_absolute():
        raise ValueError("approved workspace must be an absolute path")
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise ValueError("approved workspace must exist")
    # Only the workspace and its own components are in scope. Ancestors may be
    # platform aliases (e.g. macOS /var -> /private/var) that abspath keeps
    # verbatim; the caller passes the approved root as the supervisor sees it.
    if root.is_symlink():
        raise ValueError("approved workspace must not be a symlink")
    if not isinstance(target, dict) or target.get("pid") is None or target.get("create_time") is None:
        raise ValueError("target binding requires pid and create_time")
    try:
        pid = int(target["pid"])
        create_time = float(target["create_time"])
    except (TypeError, ValueError) as exc:
        raise ValueError("target binding pid/create_time must be numeric") from exc
    if pid <= 0:
        raise ValueError("target binding pid must be positive")

    candidate_path = root.joinpath(*PurePosixPath(normalized["log_path"]).parts)
    resolved = Path(os.path.abspath(candidate_path))
    if resolved != root and root not in resolved.parents:
        raise ValueError("observation_source.log_path escapes the approved workspace")
    current = resolved.parent
    while current != root and root in current.parents:
        if current.is_symlink():
            raise ValueError("observation_source.log_path crosses a symlink")
        current = current.parent
    return {
        "version": SCHEMA_VERSION,
        "mapping_mode": EXPLICIT_MAPPING_MODE,
        "path": str(root / normalized["log_path"]),
        "target": {"pid": pid, "create_time": create_time},
        "log_path": str(resolved),
        "fields": dict(normalized["fields"]),
        "event_names": dict(normalized["event_names"]),
    }


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    parsed: float | None = None
    if isinstance(value, (int, float)):
        parsed = float(value) / 1000.0 if value > 1e11 else float(value)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        parsed = moment.timestamp()
    # NaN/Infinity survive json.loads and are truthy; they are not timestamps.
    if parsed is None or not math.isfinite(parsed):
        return None
    return parsed


def target_liveness(target: dict[str, Any]) -> bool | None:
    """True/False for a live pid whose create_time still matches, None if unknown."""
    if psutil is None:
        return None
    pid, create_time = int(target["pid"]), float(target["create_time"])
    try:
        observed = psutil.Process(pid).create_time()
    except Exception:  # psutil.Error and OS-level lookup failures
        return False
    return abs(observed - create_time) < CREATE_TIME_TOLERANCE


def _blank_state() -> dict[str, Any]:
    return {"offset": 0, "partial": "", "valid": 0, "invalid": 0, "ignored": 0,
            "loaded": False, "pending": {}, "paired": [], "events": [],
            "last_event_time": None, "inode": None}


def _mapping_fingerprint(config: dict[str, Any]) -> str:
    """Identity of the field/event mapping so a mapping change never reuses state."""
    payload = json.dumps({"fields": config["fields"], "event_names": config["event_names"]},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def reset_state(log_path: str | None = None) -> None:
    """Drop cached read state (tests, or an explicit operator reset)."""
    if log_path is None:
        _STATE.clear()
        return
    for key in [key for key in _STATE if key.startswith(str(log_path) + "|")]:
        _STATE.pop(key, None)


def _state_for(config: dict[str, Any]) -> dict[str, Any]:
    target = config["target"]
    # The mapping is part of the identity: changing how fields/events are named
    # must start a fresh read, never blend results from the previous mapping.
    key = "%s|%s|%s|%s" % (config["log_path"], target["pid"], target["create_time"],
                            _mapping_fingerprint(config))
    state = _STATE.get(key)
    if state is None:
        state = _blank_state()
        _STATE[key] = state
    return state


def _apply_line(line: str, state: dict[str, Any], config: dict[str, Any]) -> None:
    fields = config["fields"]
    canonical_for: dict[str, str] = {}
    for canonical, raw in config["event_names"].items():
        canonical_for.setdefault(raw, canonical)
    text = line.strip()
    if not text:
        return
    try:
        event = json.loads(text)
    except ValueError:
        state["invalid"] += 1
        return
    if not isinstance(event, dict):
        state["invalid"] += 1
        return
    if event.get(fields["pid"]) != config["target"]["pid"]:
        state["invalid"] += 1
        return
    raw_event = event.get(fields["event"])
    canonical = canonical_for.get(raw_event) or event_vocabulary.canonical(raw_event)
    if canonical is None:
        # Unrelated hook output for the same instance: not an observation event.
        state["ignored"] += 1
        return
    timestamp = _timestamp(event.get(fields["timestamp"]))
    if timestamp is None:
        state["invalid"] += 1
        return
    if timestamp < float(config["target"]["create_time"]) - EVENT_TIME_TOLERANCE_S:
        # A hook cannot report activity from before the instance it is bound to
        # came into existence; such lines are treated the same as unparsable.
        state["invalid"] += 1
        return
    if canonical == "hook.loaded":
        state["loaded"] = True
    elif canonical in ('tool.execute.before', 'tool.execute.after'):
        # ``tool``/``call_id`` are optional in a candidate declaration: a Hook
        # that never writes them cannot be correlated. A missing role means the
        # event cannot be paired, which is invalid, not a reason to invent a name.
        call_field, tool_field = fields.get("call_id"), fields.get("tool")
        call_id = event.get(call_field) if call_field else None
        tool = event.get(tool_field) if tool_field else None
        if not state["loaded"] or not isinstance(call_id, str) or not call_id \
                or not isinstance(tool, str) or not tool:
            state["invalid"] += 1
            return
        if canonical == "tool.execute.before":
            state["pending"][call_id] = (tool, timestamp)
        else:
            before = state["pending"].pop(call_id, None)
            if before is None or before[0] != tool or timestamp < before[1]:
                state["invalid"] += 1
                return
            state["paired"].append({"call_id": call_id[:120], "tool_name": tool[:120]})
            state["paired"] = state["paired"][-MAX_PAIRED:]
    state["valid"] += 1
    if state["last_event_time"] is None or timestamp > state["last_event_time"]:
        state["last_event_time"] = timestamp
    state["events"].append({"event_type": canonical, "timestamp": timestamp,
                            "pid": config["target"]["pid"]})
    state["events"] = state["events"][-MAX_EVENTS:]


def _ingest(config: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Consume only appended bytes; returns an error message when unreadable."""
    path = Path(config["log_path"])
    if not path.exists():
        return None
    if path.is_symlink():
        return "观测日志不得为符号链接"
    try:
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        replaced = state["inode"] is not None and state["inode"] != identity
        truncated = info.st_size < state["offset"]
        if replaced or truncated:
            # A same-path file that was replaced (copy/rename/rotate) or truncated
            # is a different reader target: restart instead of trusting an offset.
            state.update(_blank_state())
        state["inode"] = identity
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(state["offset"])
            chunk = stream.read(MAX_READ_CHUNK_BYTES)
            state["offset"] = stream.tell()
    except OSError as exc:
        return "观测日志读取失败: %s" % type(exc).__name__
    text = state["partial"] + chunk
    lines = text.split("\n")
    state["partial"] = lines.pop()
    if len(state["partial"]) > MAX_PARTIAL_BYTES:
        state["partial"] = ""
        state["invalid"] += 1
    for line in lines:
        _apply_line(line, state, config)
    return None


def _snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Read appended events and project the shared observation snapshot shape."""
    state = _state_for(config)
    read_error = _ingest(config, state)
    alive = target_liveness(config["target"])
    bound = "%s:%s" % (config["target"]["pid"], config["target"]["create_time"])
    result: dict[str, Any] = {
        "status": "connected",
        "source": config["path"],
        "binding": {"target": dict(config["target"]), "log_path": config["log_path"],
                    "field_mapping": dict(config["fields"]),
                    "event_names": dict(config["event_names"]),
                    "configured_by": "external_config"},
        "instance_pid": config["target"]["pid"],
        "instance_create_time": config["target"]["create_time"],
        "instance_id": bound,
        "last_event_time": state["last_event_time"],
        "target_alive": alive,
        "blocking": dict(BLOCKING_UNSUPPORTED),
        "events": {"valid": state["valid"], "invalid": state["invalid"],
                   "ignored": state["ignored"]},
        "paired_calls": list(state["paired"][-MAX_PAIRED:]),
        "recent_events": list(state["events"]),
        "capabilities": {
            "observation": {"status": "supported", "label": "文件事件观测"},
            "blocking": dict(BLOCKING_UNSUPPORTED),
        },
    }
    if read_error is not None:
        result.update(status="unavailable", message=read_error,
                      health={"status": "unknown", "healthy": False,
                              "reason": read_error, "loaded_observed": False})
        return result
    loaded_observed = bool(state["loaded"]) and alive is True
    observing = bool(state["paired"]) and alive is True
    if alive is False:
        reason = "目标进程已退出或被替换（create_time 不再匹配）；不继承历史健康状态"
    elif alive is None:
        reason = "无法确认目标进程活性"
    elif observing:
        reason = "已观察到绑定实例的工具事件配对"
    elif loaded_observed:
        reason = "已观察到加载事件，尚无工具事件配对"
    else:
        reason = "尚无该绑定实例的加载事件"
    result.update(
        status="connected",
        health={"status": "observing" if observing else ("loaded" if loaded_observed else "awaiting_events"),
                "healthy": bool(observing), "reason": reason,
                "loaded_observed": loaded_observed},
        message=reason,
    )
    return result


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    with _STATE_LOCK:
        return _snapshot(config)
