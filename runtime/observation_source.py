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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
CANONICAL_EVENTS = ("hook.loaded", "tool.execute.before", "tool.execute.after")
DEFAULT_FIELDS = {"event": "event", "pid": "pid", "timestamp": "ts",
                  "tool": "tool", "call_id": "callID"}
BLOCKING_UNSUPPORTED = {"status": "unsupported", "label": "未支持"}

_STATE: dict[str, dict[str, Any]] = {}


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
    fields = dict(DEFAULT_FIELDS)
    configured = value.get("fields")
    if configured is not None:
        if not isinstance(configured, dict):
            raise ValueError("observation config fields must be an object")
        for key, name in configured.items():
            if key not in DEFAULT_FIELDS:
                raise ValueError("unknown observation field mapping: " + str(key))
            if not isinstance(name, str) or not name.strip():
                raise ValueError("observation field mapping values must be non-empty strings")
            fields[key] = name.strip()
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
        "path": str(config_path),
        "target": {"pid": pid, "create_time": create_time},
        "log_path": str(resolved),
        "fields": fields,
        "event_names": event_names,
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
    canonical = canonical_for.get(event.get(fields["event"]))
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
    else:
        call_id = event.get(fields["call_id"])
        tool = event.get(fields["tool"])
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


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
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
