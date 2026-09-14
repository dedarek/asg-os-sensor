"""Bounded, generic raw Hook event data for the runtime dashboard.

The normal observation source deliberately projects a Hook log into a small
health snapshot.  The dashboard also needs a way to inspect the real event
payloads that produced that snapshot.  This module reads only bindings already
persisted by :class:`runtime.observation_registry.Registry`; it never guesses a
log path or binds an event to a process from the event itself.

The reader is intentionally bounded.  A live Hook can keep appending to its
JSONL file, so each read takes a bounded tail of the file and reports when the
tail is incomplete.  Every returned payload is the original JSON object after
credential-like values have been redacted.  Non-sensitive fields are retained
so request/response details remain useful for diagnosis.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable

from runtime.observation_registry import Registry
from runtime import observation_source


SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_LIMIT = 2_000
CREATE_TIME_TOLERANCE = 1e-3
EVENT_TIME_TOLERANCE_S = 1.0

# Keep this intentionally aligned with the redaction rules used by the
# read-only investigation tools, while also catching credential names commonly
# used by arbitrary Hooks.  The key is retained; only its value is replaced.
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"proxy-authorization|cookie|set-cookie|password|passwd|secret|"
    r"credential|private[_-]?key|bearer|session[_-]?token|client[_-]?secret|"
    r"(?:^|[_-])nonce$|(?:^|[_-])key$)", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"proxy-authorization|cookie|set-cookie|password|passwd|secret|"
    r"credential|private[_-]?key|bearer|session[_-]?token|client[_-]?secret)"
    r"(\s*[:=]\s*)[^\s,;]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.S,
)


def _default_run_dir() -> Path:
    return Path(os.environ.get(
        "ASG_RUN_DIR",
        str(Path(__file__).resolve().parents[1] / "artifacts" / "stage1" / "dashboard"),
    ))


def _run_dir(value: Path | str | None) -> Path:
    return Path(value).expanduser() if value is not None else _default_run_dir()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_limit(value: int | str | None) -> int:
    if value is None:
        return DEFAULT_MAX_RECORDS
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RECORDS
    return max(1, min(number, MAX_LIMIT))


def _safe_max_bytes(value: int | str | None) -> int:
    if value is None:
        value = os.environ.get("ASG_HOOK_DATA_MAX_BYTES", DEFAULT_MAX_BYTES)
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_MAX_BYTES
    return max(64 * 1024, min(number, 64 * 1024 * 1024))


def _target(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or isinstance(value.get("pid"), bool):
        return None
    try:
        pid = int(value["pid"])
        create_time = float(value["create_time"])
    except (KeyError, TypeError, ValueError):
        return None
    if pid <= 0 or not math.isfinite(create_time):
        return None
    return {"pid": pid, "create_time": create_time}


def _same_target(left: Any, right: Any) -> bool:
    one, two = _target(left), _target(right)
    return bool(one and two and one["pid"] == two["pid"] and
                abs(one["create_time"] - two["create_time"]) <= CREATE_TIME_TOLERANCE)


def _instance_id(target: dict[str, Any]) -> str:
    return f"{int(target['pid'])}:{float(target['create_time'])}"


def _parse_time(value: Any) -> float | None:
    """Parse the same timestamp shapes accepted by the file observer."""
    return observation_source._timestamp(value)


def _time_label(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def redact(value: Any) -> Any:
    """Redact credential-like values while retaining the payload shape."""
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _PRIVATE_KEY.sub("[REDACTED]", value)
        value = _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        return _TOKEN.sub("[REDACTED]", value)
    return value


def _registry_bindings(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Load and validate the persisted Registry mapping.

    Registry writes immutable copies below ``observation-bindings``.  Reject a
    mapping that points elsewhere or whose copied target no longer agrees with
    its record; returning an unavailable entry keeps the UI truthful without
    inventing a replacement binding.
    """
    try:
        records = Registry(root)._read()
    except (OSError, ValueError) as exc:
        return [], [], f"registry_unavailable:{type(exc).__name__}"
    if records == {}:
        return [], [], None
    if not isinstance(records, dict):
        return [], [{"status": "unavailable", "reason": "registry_not_an_object"}], None

    binding_root = (root / "observation-bindings").resolve(strict=False)
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for record_id, record in records.items():
        if not isinstance(record, dict):
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": "registry_record_not_an_object"})
            continue
        config_name = record.get("config_path")
        if not isinstance(config_name, str) or not config_name.strip():
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": "binding_config_missing"})
            continue
        config_path = Path(config_name).expanduser().resolve(strict=False)
        try:
            config_path.relative_to(binding_root)
        except ValueError:
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": "binding_config_outside_registry"})
            continue
        try:
            config = observation_source.load_config(config_path)
        except (OSError, ValueError) as exc:
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": f"binding_config_invalid:{type(exc).__name__}"})
            continue
        stored_target = _target(record.get("target"))
        config_target = _target(config.get("target"))
        if not stored_target or not config_target or not _same_target(stored_target, config_target):
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": "binding_target_mismatch"})
            continue
        try:
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append({"status": "unavailable", "instance_id": str(record_id),
                           "reason": f"binding_config_invalid:{type(exc).__name__}"})
            continue
        raw_event_names = raw_config.get("event_names") if isinstance(raw_config, dict) else None
        # ``load_config`` supplies defaults for compatibility.  The raw viewer
        # must not turn those defaults into claims that an older Hook declared
        # every newly supported event; retain only names present in the saved
        # binding and expose the remaining supported vocabulary separately.
        declared_event_names = list(dict.fromkeys(
            value for value in (raw_event_names or {}).values()
            if isinstance(value, str) and value
        )) if isinstance(raw_event_names, dict) else []
        declared_event_keys = list(dict.fromkeys(
            key for key in (raw_event_names or {}).keys()
            if isinstance(key, str) and key
        )) if isinstance(raw_event_names, dict) else []
        valid.append({
            "record_id": str(record_id),
            "instance_id": _instance_id(config_target),
            "target": config_target,
            "config": config,
            "config_path": str(config_path),
            "declared_event_types": declared_event_names,
            "declared_event_keys": declared_event_keys,
            "supported_event_types": list(observation_source.CANONICAL_EVENTS),
        })
    return valid, errors, None


def _event_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_create_time(event: dict[str, Any]) -> float | None | str:
    """Return an optional event identity, preserving absence as ``None``."""
    for key in ("create_time", "createTime", "process_create_time", "instance_create_time"):
        if key in event:
            try:
                value = float(event[key])
            except (TypeError, ValueError):
                return "invalid"
            return value if math.isfinite(value) else "invalid"
    return None


def _read_jsonl(path: Path, max_bytes: int) -> tuple[list[tuple[int, str]], dict[str, Any]]:
    """Read a bounded, complete-line tail from a JSONL source."""
    meta: dict[str, Any] = {
        "source_exists": False, "source_readable": False, "source_size": 0,
        "bytes_read": 0, "truncated": False, "partial_lines": 0,
        "read_error": None,
    }
    if not path.exists():
        return [], meta
    if path.is_symlink():
        meta["read_error"] = "观测日志不得为符号链接"
        return [], meta
    try:
        size = path.stat().st_size
        meta.update(source_exists=True, source_size=size)
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                data = handle.read(max_bytes)
                meta["truncated"] = True
            else:
                data = handle.read(max_bytes)
        meta["bytes_read"] = len(data)
    except OSError as exc:
        meta["read_error"] = f"观测日志读取失败: {type(exc).__name__}"
        return [], meta
    text = data.decode("utf-8", errors="replace")
    if meta["truncated"] and not data.startswith(b"\n"):
        # The first line begins before our bounded tail. It is incomplete and
        # cannot be presented as a fabricated JSON event.
        first_newline = text.find("\n")
        if first_newline < 0:
            return [], meta
        text = text[first_newline + 1:]
    pieces = text.splitlines(keepends=True)
    lines: list[tuple[int, str]] = []
    line_number = 1
    for piece in pieces:
        complete = piece.endswith("\n") or piece.endswith("\r")
        if not complete:
            meta["partial_lines"] += 1
            continue
        lines.append((line_number, piece.rstrip("\r\n")))
        line_number += 1
    meta["source_readable"] = True
    return lines, meta


def _coverage(
    config: dict[str, Any] | None,
    records: list[dict[str, Any]],
    *,
    malformed: int = 0,
    filtered: int = 0,
    filtered_reasons: dict[str, int] | None = None,
    source: dict[str, Any] | None = None,
    missing_inputs: Iterable[str] = (),
    declared_event_types: Iterable[str] | None = None,
    declared_event_keys: Iterable[str] | None = None,
    supported_event_types: Iterable[str] | None = None,
) -> dict[str, Any]:
    declared = list(dict.fromkeys(str(item) for item in (declared_event_types or [])))
    declared_keys = list(dict.fromkeys(str(item) for item in (declared_event_keys or [])))
    supported = list(dict.fromkeys(str(item) for item in
                                   (supported_event_types or observation_source.CANONICAL_EVENTS)))
    counts: dict[str, int] = {}
    for item in records:
        name = item.get("event_type")
        name = str(name) if name is not None else "<missing>"
        counts[name] = counts.get(name, 0) + 1
    observed = sorted(counts)
    missing = [name for name in declared if name not in counts]
    supported_not_declared = [name for name in supported if name not in declared_keys and name not in declared]
    limitations = [
        "未观察到仅表示当前绑定日志窗口内没有通过实例过滤的该事件，不证明 Hook 全局不存在该事件",
        "覆盖范围由已登记 observation-binding、日志文件和有界读取窗口决定",
    ]
    if source and source.get("truncated"):
        limitations.append("日志超过读取上限，仅展示尾部完整记录")
    if source and source.get("partial_lines"):
        limitations.append("末尾存在未完成 JSONL 行，未将其当作事件")
    if malformed:
        limitations.append("存在无法解析的 JSONL 行，原文未展示")
    if supported_not_declared:
        limitations.append("以下通用事件类型由观测契约支持，但当前 binding 未声明，不能据此判定缺失：" +
                           "、".join(supported_not_declared))
    complete = bool(source and source.get("source_readable") and not source.get("read_error") and
                    not source.get("truncated") and not source.get("partial_lines") and
                    not malformed and not missing and not filtered and not supported_not_declared)
    return {
        "complete": complete,
        "scope": "bound_source_window",
        "declared_event_types": declared,
        "declared_event_keys": declared_keys,
        "supported_event_types": supported,
        "supported_not_declared_event_types": supported_not_declared,
        "observed_event_types": observed,
        "missing_event_types": missing,
        "counts": counts,
        "accepted_records": len(records),
        "malformed_records": malformed,
        "filtered_records": filtered,
        "filtered_reasons": dict(filtered_reasons or {}),
        "source": {
            "exists": bool(source and source.get("source_exists")),
            "readable": bool(source and source.get("source_readable")),
            "size_bytes": int((source or {}).get("source_size") or 0),
            "bytes_read": int((source or {}).get("bytes_read") or 0),
            "truncated": bool(source and source.get("truncated")),
            "partial_lines": int((source or {}).get("partial_lines") or 0),
        },
        "missing_inputs": list(dict.fromkeys(str(item) for item in missing_inputs if item)),
        "limitations": limitations,
    }


def _read_binding(binding: dict[str, Any], *, since: float | None, until: float | None,
                  limit: int, max_bytes: int) -> dict[str, Any]:
    config = binding["config"]
    target = binding["target"]
    fields = config.get("fields") or {}
    log_path = Path(config["log_path"])
    lines, source = _read_jsonl(log_path, max_bytes)
    records: list[dict[str, Any]] = []
    malformed = 0
    filtered = 0
    reasons: dict[str, int] = {}

    def reject(reason: str) -> None:
        nonlocal filtered
        filtered += 1
        reasons[reason] = reasons.get(reason, 0) + 1

    for line_number, text in lines:
        if not text.strip():
            continue
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        pid = _event_pid(event.get(fields.get("pid", "pid")))
        if pid != target["pid"]:
            reject("pid_mismatch")
            continue
        event_ct = _optional_create_time(event)
        if event_ct == "invalid" or (event_ct is not None and
                                      abs(float(event_ct) - target["create_time"]) > CREATE_TIME_TOLERANCE):
            reject("create_time_mismatch")
            continue
        timestamp = _parse_time(event.get(fields.get("timestamp", "timestamp")))
        if timestamp is None:
            reject("timestamp_invalid")
            continue
        if timestamp < target["create_time"] - EVENT_TIME_TOLERANCE_S:
            reject("timestamp_before_instance")
            continue
        if since is not None and timestamp < since:
            reject("before_since")
            continue
        if until is not None and timestamp > until:
            reject("after_until")
            continue
        event_type = event.get(fields.get("event", "event"))
        if event_type is not None and not isinstance(event_type, (str, int, float, bool)):
            event_type = str(event_type)
        records.append({
            "line": line_number,
            "timestamp": timestamp,
            "timestamp_iso": _time_label(timestamp),
            "event_type": event_type,
            "payload": redact(event),
        })

    truncated_by_records = len(records) > limit
    if truncated_by_records:
        records = records[-limit:]
    alive = observation_source.target_liveness(target)
    missing_inputs: list[str] = []
    if not source.get("source_exists"):
        missing_inputs.append("log_file")
    if source.get("read_error"):
        missing_inputs.append("readable_log_file")
    missing_inputs.extend(
        f"event_field:{role}" for role in ("event", "pid", "timestamp") if not fields.get(role)
    )
    coverage = _coverage(config, records, malformed=malformed, filtered=filtered,
                         filtered_reasons=reasons, source=source,
                         missing_inputs=missing_inputs,
                         declared_event_types=binding.get("declared_event_types"),
                         declared_event_keys=binding.get("declared_event_keys"),
                         supported_event_types=binding.get("supported_event_types"))
    if truncated_by_records:
        coverage["truncated_records"] = True
        coverage["limitations"].append("记录数量超过单次读取上限，仅展示最新有界记录")
        coverage["complete"] = False
    else:
        coverage["truncated_records"] = False
    status = "ok"
    if source.get("read_error"):
        status = "unavailable"
    elif not source.get("source_exists"):
        status = "missing_source"
    return {
        "status": status,
        "instance_id": binding["instance_id"],
        "registry_id": binding["record_id"],
        "target": dict(target),
        "target_alive": alive,
        "binding": {
            "config_path": binding["config_path"],
            "log_path": str(log_path),
            "field_mapping": dict(fields),
            "event_names": dict(config.get("event_names") or {}),
        },
        "records": records,
        "coverage": coverage,
    }


def _aggregate(bindings: list[dict[str, Any]], errors: list[dict[str, Any]],
               *, no_match: bool = False) -> dict[str, Any]:
    counts: dict[str, int] = {}
    observed: set[str] = set()
    missing: set[str] = set()
    supported_not_declared: set[str] = set()
    missing_inputs: list[str] = []
    accepted = malformed = filtered = 0
    complete = bool(bindings) and not errors
    source_exists = source_readable = False
    source_size = bytes_read = partial_lines = 0
    source_truncated = False
    for item in bindings:
        coverage = item.get("coverage") or {}
        for key, value in (coverage.get("counts") or {}).items():
            counts[key] = counts.get(key, 0) + int(value or 0)
        observed.update(coverage.get("observed_event_types") or [])
        missing.update(coverage.get("missing_event_types") or [])
        supported_not_declared.update(coverage.get("supported_not_declared_event_types") or [])
        missing_inputs.extend(coverage.get("missing_inputs") or [])
        accepted += int(coverage.get("accepted_records") or 0)
        malformed += int(coverage.get("malformed_records") or 0)
        filtered += int(coverage.get("filtered_records") or 0)
        complete = complete and bool(coverage.get("complete"))
        source = coverage.get("source") or {}
        source_exists = source_exists or bool(source.get("exists"))
        source_readable = source_readable or bool(source.get("readable"))
        source_size += int(source.get("size_bytes") or 0)
        bytes_read += int(source.get("bytes_read") or 0)
        partial_lines += int(source.get("partial_lines") or 0)
        source_truncated = source_truncated or bool(source.get("truncated"))
    if errors:
        complete = False
        missing_inputs.append("valid_binding_config")
    if no_match:
        complete = False
        missing_inputs.append("binding_for_requested_target")
    return {
        "complete": complete,
        "scope": "registered_bindings",
        "source": {"exists": source_exists, "readable": source_readable,
                   "size_bytes": source_size, "bytes_read": bytes_read,
                   "truncated": source_truncated, "partial_lines": partial_lines},
        "observed_event_types": sorted(observed),
        "missing_event_types": sorted(missing),
        "supported_not_declared_event_types": sorted(supported_not_declared),
        "counts": counts,
        "accepted_records": accepted,
        "malformed_records": malformed,
        "filtered_records": filtered,
        "binding_count": len(bindings),
        "unavailable_binding_count": len(errors),
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
        "limitations": [
            "这是已登记绑定和当前有界日志窗口的覆盖，不是所有 Agent 行为的全局覆盖证明",
            "过滤掉了 pid、create_time 或时间戳不匹配的输入；这些输入不作为当前实例事件展示",
        ],
    }


def snapshot(run_dir: Path | str | None = None, *, pid: int | str | None = None,
             create_time: float | str | None = None, instance_id: str | None = None,
             since: Any = None, until: Any = None, limit: int | str | None = None,
             max_bytes: int | str | None = None) -> dict[str, Any]:
    """Return redacted raw records and explicit coverage for Registry bindings."""
    root = _run_dir(run_dir)
    try:
        requested_pid = None if pid in (None, "") else int(pid)
    except (TypeError, ValueError):
        return {"status": "invalid_request", "error": "pid must be an integer", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_pid"]}}
    try:
        requested_ct = None if create_time in (None, "") else float(create_time)
    except (TypeError, ValueError):
        return {"status": "invalid_request", "error": "create_time must be numeric", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_create_time"]}}
    if requested_pid is not None and requested_pid <= 0:
        return {"status": "invalid_request", "error": "pid must be positive", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_pid"]}}
    if requested_ct is not None and not math.isfinite(requested_ct):
        return {"status": "invalid_request", "error": "create_time must be finite", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_create_time"]}}
    lower, upper = _parse_time(since), _parse_time(until)
    if since not in (None, "") and lower is None or until not in (None, "") and upper is None:
        return {"status": "invalid_request", "error": "since/until must be timestamps", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_time_range"]}}
    if lower is not None and upper is not None and lower > upper:
        return {"status": "invalid_request", "error": "since must be <= until", "bindings": [],
                "records": [], "coverage": {"complete": False, "missing_inputs": ["valid_time_range"]}}

    entries, errors, registry_error = _registry_bindings(root)
    filtered_entries: list[dict[str, Any]] = []
    for item in entries:
        target = item["target"]
        if requested_pid is not None and target["pid"] != requested_pid:
            continue
        if requested_ct is not None and abs(target["create_time"] - requested_ct) > CREATE_TIME_TOLERANCE:
            continue
        if instance_id and instance_id not in {item["instance_id"], item["record_id"]}:
            continue
        filtered_entries.append(item)
    no_match = bool(entries) and not filtered_entries and bool(requested_pid is not None or requested_ct is not None or instance_id)
    results = [_read_binding(item, since=lower, until=upper, limit=_safe_limit(limit),
                             max_bytes=_safe_max_bytes(max_bytes)) for item in filtered_entries]
    flat_records: list[dict[str, Any]] = []
    for item in results:
        for record in item.get("records", []):
            flat_records.append({**record, "instance_id": item["instance_id"],
                                 "target": dict(item["target"])})
    if registry_error:
        status = "unavailable"
    elif not entries:
        status = "partial" if errors else "not_configured"
    elif not filtered_entries:
        status = "empty"
    elif any(item.get("status") == "unavailable" for item in results) or errors:
        status = "partial"
    else:
        status = "ok"
    coverage = _aggregate(results, errors, no_match=no_match)
    if registry_error:
        coverage["missing_inputs"] = [registry_error]
        coverage["complete"] = False
    if not entries:
        coverage.update(complete=False, binding_count=0)
        coverage["missing_inputs"] = ["observation_binding"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": _now(),
        "run_dir": str(root),
        "filter": {"pid": requested_pid, "create_time": requested_ct,
                   "instance_id": instance_id, "since": _time_label(lower),
                   "until": _time_label(upper), "limit": _safe_limit(limit),
                   "max_bytes": _safe_max_bytes(max_bytes)},
        "bindings": results + errors,
        "records": flat_records,
        # ``events`` is a convenient direct raw-payload view for API clients;
        # ``records`` carries line/time/instance metadata used by the drawer.
        "events": [record["payload"] for record in flat_records],
        "coverage": coverage,
    }


def read_raw_events(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for callers that prefer an event-oriented name."""
    return snapshot(*args, **kwargs)


def collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias used by small integrations and tests."""
    return snapshot(*args, **kwargs)


__all__ = ["SCHEMA_VERSION", "redact", "snapshot", "read_raw_events", "collect"]
