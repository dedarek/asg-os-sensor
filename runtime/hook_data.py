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
from runtime import event_vocabulary, observation_source


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

_NETWORK_REQUEST_TYPES = {
    "network.request", "network.request.sent", "http.request",
    "http.request.sent", "model.network.request", "model.http.request",
}
_NETWORK_RESPONSE_TYPES = {
    "network.response", "network.response.received", "http.response",
    "http.response.received", "model.network.response", "model.http.response",
}
_NETWORK_TRANSPORT_KEYS = (
    "capture_layer", "transport_layer", "transport", "network_transport",
)
_NETWORK_CORRELATION_KEYS = (
    "correlation_id", "correlationId", "trace_id", "traceId",
    "request_id", "requestId", "call_id", "callId",
)
_NETWORK_BODY_KEYS = ("body", "request_body", "response_body")
_NETWORK_COMPLETE_KEYS = ("body_complete", "bodyComplete", "complete_body", "completeBody")
_NETWORK_TRUNCATION_KEYS = (
    "truncated", "body_truncated", "bodyTruncated", "is_truncated", "isTruncated",
    "partial", "body_partial", "bodyPartial",
)
_MODEL_CANDIDATE_TYPES = {
    "model.request", "model.response", "assistant.output",
    *_NETWORK_REQUEST_TYPES, *_NETWORK_RESPONSE_TYPES,
}


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


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _network_transport(event: dict[str, Any]) -> str | None:
    """Read an explicitly named transport layer; never infer it from event kind."""
    for key in _NETWORK_TRANSPORT_KEYS:
        if key not in event:
            continue
        value = event.get(key)
        if key == "capture_layer":
            # The generated Hook contract uses capture_layer="transport".
            # Do not treat capture_layer="model" (or an arbitrary label) as
            # proof that a model summary came from the network.
            text = _scalar_text(value)
            if text and text.lower() in {"transport", "network", "http", "https", "wire"}:
                return text
            continue
        if isinstance(value, dict):
            for nested in ("layer", "transport_layer", "protocol", "name"):
                text = _scalar_text(value.get(nested))
                if text:
                    return text
            continue
        text = _scalar_text(value)
        if text:
            return text
    return None


def _network_correlation_id(event: dict[str, Any]) -> str | None:
    for key in _NETWORK_CORRELATION_KEYS:
        text = _scalar_text(event.get(key)) if key in event else None
        if text:
            return text
    return None


def _network_body(event: dict[str, Any]) -> tuple[str | None, bool]:
    for key in _NETWORK_BODY_KEYS:
        if key in event and event.get(key) is not None:
            return key, True
    return None, False


def _network_body_flags(event: dict[str, Any]) -> tuple[bool, bool]:
    """Return (body_complete, explicitly_not_truncated).

    A complete-body marker is required, together with either an explicit false
    truncation/partial marker or no truncation marker.  A body-looking
    ``input``/``output`` value is deliberately not enough evidence for a
    network capture.
    """
    complete = False
    for key in _NETWORK_COMPLETE_KEYS:
        if key in event:
            complete = event.get(key) is True
            break
    markers = [event.get(key) for key in _NETWORK_TRUNCATION_KEYS if key in event]
    no_truncation = bool(markers) and all(value is False for value in markers)
    # An explicit body_complete=true is itself an assertion that the body was
    # not truncated when no truncation marker was emitted.  Keep an explicit
    # false truncation flag authoritative, but still require body_complete=true
    # so an arbitrary body plus truncated=false cannot be promoted to a full
    # transport capture.
    if complete and not markers:
        no_truncation = True
    return complete, no_truncation


def _network_direction(event: dict[str, Any], event_type: Any) -> str | None:
    for key in ("direction", "network_direction", "message_direction"):
        if key in event:
            value = _scalar_text(event.get(key))
            if not value:
                continue
            value = value.lower().replace("-", "_")
            if value in {"request", "req", "outbound", "egress", "sent"}:
                return "request"
            if value in {"response", "resp", "inbound", "ingress", "received", "recv"}:
                return "response"
    normalized = _scalar_text(event_type)
    if normalized:
        normalized = normalized.lower()
        if normalized in _NETWORK_REQUEST_TYPES:
            return "request"
        if normalized in _NETWORK_RESPONSE_TYPES:
            return "response"
        # Canonical model events carry parameters/results by default.  They
        # only become request/response transport evidence when an explicit
        # transport capture marker accompanies the event.
        if normalized == "model.request" and _network_transport(event):
            return "request"
        if normalized == "model.response" and _network_transport(event):
            return "response"
        if ("network" in normalized or normalized.startswith("http.")):
            if normalized.endswith("request") or ".request." in normalized:
                return "request"
            if normalized.endswith("response") or ".response." in normalized:
                return "response"
    return None


def _network_event_evidence(event: dict[str, Any], event_type: Any) -> dict[str, Any]:
    """Assess explicit network evidence without promoting model IO summaries."""
    normalized_type = _scalar_text(event_type)
    transport = _network_transport(event)
    correlation_id = _network_correlation_id(event)
    body_key, has_body = _network_body(event)
    body_complete, no_truncation = _network_body_flags(event)
    direction = _network_direction(event, event_type)
    explicit_transport = bool(transport) or bool(
        normalized_type and normalized_type.lower() in (_NETWORK_REQUEST_TYPES | _NETWORK_RESPONSE_TYPES)
    )
    candidate = bool(
        normalized_type and normalized_type.lower() in _MODEL_CANDIDATE_TYPES
    ) or bool(transport)
    missing: list[str] = []
    if transport is None:
        missing.append("transport_layer")
    if correlation_id is None:
        missing.append("correlation_id")
    if not has_body:
        missing.append("body")
    elif not body_complete:
        missing.append("body_complete")
    if has_body and not no_truncation:
        missing.append("not_truncated")
    if direction is None:
        missing.append("direction")
    qualified = not missing
    return {
        "candidate": candidate,
        "transport_candidate": explicit_transport,
        "qualified": qualified,
        "event_type": normalized_type,
        "direction": direction,
        "correlation_id": correlation_id,
        "body_key": body_key,
        "missing_requirements": missing,
    }


def _network_capture_coverage(
    candidates: list[dict[str, Any]],
    *,
    source: dict[str, Any] | None = None,
    records_truncated: bool = False,
    malformed: int = 0,
) -> dict[str, Any]:
    """Summarize explicit network captures separately from model IO summaries."""
    candidates = list(candidates)
    # Model parameter/message summaries are useful evidence, but they are not
    # transport records.  Once an explicit transport stream is present, do not
    # let those summaries make an otherwise complete request/response pair look
    # partial merely because they lack wire-level fields.
    transport_candidates = [item for item in candidates if item.get("transport_candidate")]
    assessed = transport_candidates or candidates
    qualified = [item for item in assessed if item.get("qualified")]
    requests = [item for item in qualified if item.get("direction") == "request"]
    responses = [item for item in qualified if item.get("direction") == "response"]
    request_ids = {item.get("correlation_id") for item in requests if item.get("correlation_id")}
    response_ids = {item.get("correlation_id") for item in responses if item.get("correlation_id")}
    pairs = request_ids & response_ids
    missing: set[str] = set()
    for item in assessed:
        missing.update(item.get("missing_requirements") or [])
    source_incomplete = bool(source and (
        source.get("truncated") or source.get("partial_lines") or source.get("read_error")
    ))
    if source_incomplete or records_truncated:
        missing.add("not_truncated")
    if malformed:
        missing.add("complete_source")
    if not candidates:
        status = "not_observed"
        reason = "没有模型或网络候选事件；未据模型摘要推断网络捕获"
    elif not qualified:
        status = "partial"
        reason = "候选事件缺少显式网络传输、关联、完整 body 或方向证据"
    elif not pairs:
        status = "partial"
        reason = "存在带显式网络字段的单向事件，但未形成 request/response 关联对"
    elif source_incomplete or records_truncated or malformed:
        status = "partial"
        reason = "已形成关联对，但当前有界来源存在截断、未完成行、解析错误或记录上限"
    else:
        status = "complete"
        reason = "request/response 均有显式传输层、关联 ID、完整 body 和无截断证据"
    complete = status == "complete"
    return {
        "scope": "explicit_network_payload",
        "status": status,
        "complete": complete,
        "captured": bool(qualified),
        "candidate_events": len(candidates),
        "qualified_events": len(qualified),
        "request_events": len(requests),
        "response_events": len(responses),
        "complete_pairs": len(pairs),
        "requirements": {
            "transport_layer": bool(qualified),
            "correlation_id": bool(qualified),
            "complete_body": bool(qualified),
            "not_truncated": bool(qualified and not source_incomplete and not records_truncated),
        },
        "missing_requirements": sorted(missing),
        "reason": reason,
        "limitations": [
            "model.request 参数和 assistant.output 摘要本身不代表模型网络请求或响应",
            "complete 仅表示当前登记 binding 的有界日志窗口内存在显式关联对，不证明全局网络覆盖",
        ],
    }


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
    network_candidates: list[dict[str, Any]] | None = None,
    records_truncated: bool = False,
) -> dict[str, Any]:
    declared = list(dict.fromkeys(str(item) for item in (declared_event_types or [])))
    declared_keys = list(dict.fromkeys(str(item) for item in (declared_event_keys or [])))
    supported = list(dict.fromkeys(str(item) for item in
                                   (supported_event_types or observation_source.CANONICAL_EVENTS)))
    counts: dict[str, int] = {}
    for item in records:
        raw_name = item.get("event_type")
        name = event_vocabulary.canonical(raw_name) or raw_name
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
    network_capture = _network_capture_coverage(
        network_candidates or [], source=source,
        records_truncated=records_truncated,
        malformed=malformed,
    )
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
        "network_capture": network_capture,
        "network_capture_complete": bool(network_capture.get("complete")),
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
    network_candidates: list[dict[str, Any]] = []
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
        network_evidence = _network_event_evidence(event, event_type)
        if network_evidence.get("candidate"):
            network_candidates.append({
                **network_evidence,
                "line": line_number,
                "timestamp": timestamp,
            })
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
        kept_lines = {record.get("line") for record in records}
        network_candidates = [item for item in network_candidates if item.get("line") in kept_lines]
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
                         supported_event_types=binding.get("supported_event_types"),
                         network_candidates=network_candidates,
                         records_truncated=truncated_by_records)
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
    network_items: list[dict[str, Any]] = []
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
        if isinstance(coverage.get("network_capture"), dict):
            network_items.append(coverage["network_capture"])
    if errors:
        complete = False
        missing_inputs.append("valid_binding_config")
    if no_match:
        complete = False
        missing_inputs.append("binding_for_requested_target")
    network_candidates = sum(int(item.get("candidate_events") or 0) for item in network_items)
    network_qualified = sum(int(item.get("qualified_events") or 0) for item in network_items)
    network_requests = sum(int(item.get("request_events") or 0) for item in network_items)
    network_responses = sum(int(item.get("response_events") or 0) for item in network_items)
    network_pairs = sum(int(item.get("complete_pairs") or 0) for item in network_items)
    network_missing: set[str] = set()
    # A binding can legitimately expose model summaries without any transport
    # capture.  Those bindings must remain visible, but they must not downgrade
    # a separate explicit transport binding that has a complete pair.
    transport_items = [item for item in network_items if int(item.get("qualified_events") or 0) > 0]
    assessed_network_items = transport_items or network_items
    for item in assessed_network_items:
        network_missing.update(item.get("missing_requirements") or [])
    if errors or no_match:
        network_missing.add("complete_binding_set")
    # An aggregate cannot claim complete network coverage while any registered
    # binding is unavailable or the requested instance had no matching
    # binding.  The valid binding may still expose a qualified pair, so keep
    # that evidence in the counts and report the aggregate as partial.
    network_complete = (
        bool(transport_items) and not errors and not no_match and
        all(item.get("complete") for item in transport_items)
    )
    if not network_items or network_candidates == 0:
        network_status = "not_observed"
        network_reason = "没有模型或网络候选事件；未据模型摘要推断网络捕获"
    elif network_complete:
        network_status = "complete"
        network_reason = "request/response 均有显式网络字段和完整关联对"
    else:
        network_status = "partial"
        network_reason = "网络候选事件存在，但尚不足以证明完整模型网络请求响应"
    network_capture = {
        "scope": "explicit_network_payload",
        "status": network_status,
        "complete": network_complete,
        "captured": network_qualified > 0,
        "candidate_events": network_candidates,
        "qualified_events": network_qualified,
        "request_events": network_requests,
        "response_events": network_responses,
        "complete_pairs": network_pairs,
        "requirements": {
            "transport_layer": network_qualified > 0,
            "correlation_id": network_qualified > 0,
            "complete_body": network_qualified > 0,
            "not_truncated": bool(network_complete),
        },
        "missing_requirements": sorted(network_missing),
        "reason": network_reason,
        "limitations": [
            "模型摘要事件不代表网络捕获；仅显式网络 payload 可进入此计数",
            "complete 仅表示当前登记 binding 的有界日志窗口内存在完整关联对",
        ],
    }
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
        "network_capture": network_capture,
        "network_capture_complete": bool(network_capture.get("complete")),
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
    from runtime.io_acceptance import assess
    from runtime import event_vocabulary
    acceptance_coverage = dict(coverage)
    if any((item.get('coverage') or {}).get('truncated_records') for item in results):
        acceptance_coverage['missing_inputs'] = [*coverage.get('missing_inputs', []), 'full_record_window']
    from runtime.protocol_events import normalize
    from runtime.integration_selfcheck import assess as selfcheck
    from runtime.hook_acceptance import snapshots as acceptance_snapshots
    normalized = [normalize(r) for r in flat_records]
    coverage['io_acceptance'] = assess(normalized, acceptance_coverage)
    proofs = acceptance_snapshots()
    coverage['integration_selfchecks'] = []
    for iid in dict.fromkeys(item['instance_id'] for item in filtered_entries):
        local_results = [item for item in results if item['instance_id'] == iid]
        local_coverage = _aggregate(local_results, [], no_match=False)
        if any((item.get('coverage') or {}).get('truncated_records') for item in local_results):
            local_coverage['missing_inputs'] = [*local_coverage.get('missing_inputs', []), 'full_record_window']
        coverage['integration_selfchecks'].append(selfcheck(normalized, local_coverage, iid, proofs))
    coverage['capture_matrix'] = capture_matrix(flat_records)
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
        "normalized_records": normalized,
        # ``events`` is a convenient direct raw-payload view for API clients;
        # ``records`` carries line/time/instance metadata used by the drawer.
        "events": [record["payload"] for record in flat_records],
        # Normalized conversation and translation coverage: producers name the
        # same stage differently, so the raw labels alone cannot show whether a
        # user's own words were captured.
        "conversation": event_vocabulary.conversation(normalized, limit=200),
        "vocabulary": event_vocabulary.vocabulary_summary(flat_records),
        "coverage": coverage,
    }


def capture_matrix(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Evidence pointers for one bounded, instance-filtered Hook window.

    A declaration, model setting, or transcript path never proves use or
    conversation history. The matrix intentionally carries no body text.
    """
    names = ('user_input', 'assistant_output', 'tool_before', 'tool_after',
             'skill_use', 'mcp_use', 'model_used', 'model_request',
             'model_response', 'history_context')
    result: dict[str, Any] = {key: {'status': 'not_observed', 'count': 0,
                                     'evidence': []} for key in names}
    model_refs: dict[str, list[dict[str, Any]]] = {}

    def body(value: Any) -> bool:
        """A substantive value, not an empty envelope or metadata-only ID."""
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(body(item) for item in value)
        if isinstance(value, dict):
            return any(body(value.get(k)) for k in
                       ('text', 'content', 'parts', 'messages', 'input', 'output',
                        'prompt', 'body', 'request_body', 'response_body'))
        return False

    def add(name: str, row: dict[str, Any], *, status: str = 'observed') -> None:
        item = result[name]
        item['count'] += 1
        if item['status'] == 'not_observed' or status == 'observed':
            item['status'] = status
        if len(item['evidence']) < 3:
            payload = row.get('payload') or {}
            item['evidence'].append({'event_id': payload.get('event_id'),
                                     'line': row.get('line'),
                                     'timestamp': row.get('timestamp_iso'),
                                     'event_type': row.get('event_type')})

    for row in records:
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        content = payload.get('content')
        content = content if isinstance(content, dict) else {}
        detail = payload.get('detail') if isinstance(payload.get('detail'), dict) else {}
        kind = event_vocabulary.canonical(row.get('event_type')) or row.get('event_type')
        user_text = event_vocabulary.text_for(payload, 'user') if kind == 'user.input' else None
        assistant_text = event_vocabulary.text_for(payload, 'assistant') if kind == 'assistant.output' else None
        if user_text:
            add('user_input', row)
        if assistant_text:
            add('assistant_output', row)
        tool = str(content.get('tool_name') or payload.get('tool') or detail.get('tool') or '')
        if kind == 'tool.execute.before' and tool and any(
                key in source for source in (content, payload, detail)
                for key in ('tool_input', 'args', 'parameters')):
            add('tool_before', row)
        if kind == 'tool.execute.after' and any(
                key in source for source in (content, payload, detail)
                for key in ('tool_response', 'result', 'output')):
            add('tool_after', row)
        if kind in ('tool.execute.before', 'tool.execute.after'):
            usage_status = 'observed' if kind == 'tool.execute.after' else 'metadata_only'
            if any(source.get('skill_name') for source in (content, payload, detail)) or tool.lower() in ('skill', 'skills'):
                add('skill_use', row, status=usage_status)
            if any(source.get('mcp_server') for source in (content, payload, detail)) or tool.lower().startswith(('mcp__', 'mcp:', 'mcp.')):
                add('mcp_use', row, status=usage_status)
        model = content.get('model') or payload.get('model') or detail.get('model')
        if isinstance(model, dict):
            model = model.get('modelID') or model.get('model_id') or model.get('name')
        if kind in ('user.input', 'model.request', 'model.response',
                    'tool.execute.before', 'tool.execute.after') and isinstance(model, str) and model:
            model_refs.setdefault(model, []).append(row)
        if kind == 'model.request':
            request = content.get('request') if isinstance(content.get('request'), dict) else {}
            add('model_request', row, status='observed' if any(body(value) for value in (
                content.get('messages'), request.get('messages'), payload.get('body'),
                payload.get('request_body'), detail.get('messages'))) else 'metadata_only')
        if kind == 'model.response':
            add('model_response', row, status='observed' if any(body(value) for value in (
                content.get('output'), content.get('response'), payload.get('body'),
                payload.get('response_body'), detail.get('output'))) else 'metadata_only')
        messages = content.get('messages')
        if not messages and isinstance(content.get('request'), dict):
            messages = content['request'].get('messages')
        if not messages:
            messages = detail.get('messages')
        if kind == 'model.request' and isinstance(messages, list) and len(messages) > 1 and any(
                isinstance(m, dict) and m.get('role') in ('user', 'assistant', 'tool') and
                (m.get('content') or m.get('parts')) for m in messages[:-1]):
            add('history_context', row)

    if model_refs:
        latest = max((row for rows in model_refs.values() for row in rows),
                     key=lambda row: row.get('timestamp') or 0)
        latest_payload = latest.get('payload') or {}
        latest_content = latest_payload.get('content') or {}
        latest_model = latest_content.get('model') or latest_payload.get('model')
        if isinstance(latest_model, dict):
            latest_model = latest_model.get('modelID') or latest_model.get('model_id') or latest_model.get('name')
        result['model_used']['model'] = latest_model
        result['model_used']['scope'] = 'latest_hook_metadata_only'
        for rows in model_refs.values():
            for row in rows:
                add('model_used', row, status='metadata_only')
    unsupported = set()
    for row in records:
        payload = row.get('payload') or {}
        if row.get('event_type') == 'io.turn.end' and isinstance(payload, dict):
            unsupported.update(value for value in payload.get('unsupported_capture', [])
                               if isinstance(value, str))
    for key, event in (('model_request', 'model.request'),
                       ('model_response', 'model.response')):
        if event in unsupported and result[key]['status'] == 'not_observed':
            result[key]['reason'] = 'producer_declared_unsupported'
    if result['tool_before']['count'] and not result['tool_after']['count']:
        decisions = [row.get('payload', {}).get('content') for row in records
                     if row.get('event_type') == 'control.applied' and
                     isinstance(row.get('payload'), dict)]
        errors = [value for value in decisions if isinstance(value, dict) and
                  value.get('decision') == 'deny' and value.get('control_error')]
        result['tool_after']['reason'] = ('control_error_before_execution' if errors else
                                          'no_paired_completion_in_window')
    if result['history_context']['status'] == 'not_observed' and any(
            isinstance((row.get('payload') or {}).get('content'), dict) and
            (row['payload']['content'].get('transcript_path')) for row in records):
        result['history_context']['reason'] = 'transcript_path_only'
    return {'scope': 'bound_instance_bounded_window', 'items': result}


def read_raw_events(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for callers that prefer an event-oriented name."""
    return snapshot(*args, **kwargs)


def collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias used by small integrations and tests."""
    return snapshot(*args, **kwargs)


__all__ = ["SCHEMA_VERSION", "redact", "snapshot", "read_raw_events", "collect"]
