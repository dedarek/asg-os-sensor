"""Explicit, bounded model transport capture for the local proxy.

This recorder is deliberately opt-in.  It accepts a target instance file and
the proxy connection that is being handled, then records only that live
instance's client-facing request/response pair.  It is an observation aid for
an explicit model proxy; it does not claim to be a native Hook.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psutil

from runtime.hook_data import redact


MAX_BODY_BYTES = 4 * 1024 * 1024
CREATE_TIME_TOLERANCE = 1e-3
TARGET_FILE_ENV = "ASG_MODEL_CAPTURE_TARGET_FILE"
LOG_ENV = "ASG_MODEL_CAPTURE_LOG"
SOURCE = "explicit_model_proxy"
_ALLOWED_METADATA = frozenset({
    "method", "url", "status", "content_type", "transport_side",
})
_WRITE_LOCK = threading.Lock()


def _read_target_file(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None, "target_unreadable"
    except (TypeError, ValueError):
        return None, "target_invalid_json"
    if isinstance(value, dict) and isinstance(value.get("target"), dict):
        value = value["target"]
    if (not isinstance(value, dict) or isinstance(value.get("pid"), bool) or
            isinstance(value.get("create_time"), bool)):
        return None, "target_invalid_shape"
    try:
        pid = int(value["pid"])
        create_time = float(value["create_time"])
    except (KeyError, TypeError, ValueError):
        return None, "target_invalid_identity"
    if pid <= 0 or not math.isfinite(create_time):
        return None, "target_invalid_identity"
    return {"pid": pid, "create_time": create_time}, "ok"


def _target_from_file(path: Path) -> dict[str, Any] | None:
    """Read a target identity, retaining the old private helper contract."""
    target, _detail = _read_target_file(path)
    return target


def _endpoint(value: Any) -> tuple[str, int] | None:
    if value is None:
        return None
    if hasattr(value, "ip") and hasattr(value, "port"):
        host, port = getattr(value, "ip", None), getattr(value, "port", None)
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        host, port = value[0], value[1]
    else:
        return None
    if host is None:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if port <= 0:
        return None
    return str(host).strip().strip("[]").lower(), port


def _same_host(left: str, right: str) -> bool:
    # Strip an IPv6 zone suffix; psutil and the HTTP server can format it
    # differently while still referring to the same local address.
    return left.split("%", 1)[0] == right.split("%", 1)[0]


def _safe_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def _connection_result(process: Any, peer: Any,
                       server_port: Any) -> tuple[bool, str]:
    expected_peer = _endpoint(peer)
    expected_port = _safe_port(server_port)
    if expected_peer is None or expected_port is None:
        return False, "connection_context_invalid"
    try:
        connections = process.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return False, "connection_permission_error"
    except PermissionError:
        return False, "connection_permission_error"
    except psutil.NoSuchProcess:
        return False, "pid_unavailable"
    except (OSError, psutil.Error):
        return False, "connection_query_error"
    for connection in connections or ():
        local = _endpoint(getattr(connection, "laddr", None))
        remote = _endpoint(getattr(connection, "raddr", None))
        if (local and remote and local[1] == expected_peer[1] and
                _same_host(local[0], expected_peer[0]) and remote[1] == expected_port):
            return True, "capture_started"
    return False, "connection_no_match"


def _connection_belongs(process: Any, peer: Any, server_port: Any) -> bool:
    """Return only the legacy boolean result used by existing callers/tests."""
    return _connection_result(process, peer, server_port)[0]


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if parsed.netloc:
            if not hostname:
                return None
            hostname = hostname.strip("[]")
            if ":" in hostname:
                hostname = "[" + hostname + "]"
            port = parsed.port
            netloc = hostname + ((":" + str(port)) if port is not None else "")
        else:
            netloc = ""
        safe = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return None
    return str(redact(safe))


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return str(redact(value))


def _safe_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _ALLOWED_METADATA:
        if key not in metadata or metadata[key] is None:
            continue
        value = metadata[key]
        if key == "url":
            safe = _safe_url(value)
            if safe is not None:
                result[key] = safe
        elif key == "status":
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                if math.isfinite(float(value)):
                    result[key] = value
            else:
                safe = _safe_text(value)
                if safe is not None:
                    result[key] = safe
        else:
            safe = _safe_text(value)
            if safe is not None:
                result[key] = safe
    return result


def _status_peer(value: Any) -> dict[str, Any] | None:
    endpoint = _endpoint(value)
    if endpoint is None:
        return None
    return {"host": endpoint[0], "port": endpoint[1]}


def _write_status(path: Path, status: dict[str, Any]) -> bool:
    """Best-effort overwrite of the latest capture-attempt status."""
    try:
        payload = (json.dumps(status, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow:
            flags |= no_follow
        with _WRITE_LOCK:
            fd = os.open(str(path), flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        return False
                    offset += written
            finally:
                os.close(fd)
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _note_status(log_file: str, *, reason: str, target: dict[str, Any] | None,
                 peer: Any, server_port: Any, result: str,
                 detail: str | None = None) -> None:
    """Persist only safe identity/connection diagnostics.

    This helper deliberately catches every ordinary exception.  Status
    diagnostics are ancillary and must never interrupt proxy forwarding.
    """
    try:
        target_pid = None
        target_create_time = None
        if isinstance(target, dict):
            candidate_pid = target.get("pid")
            if not isinstance(candidate_pid, bool):
                try:
                    candidate_pid = int(candidate_pid)
                except (TypeError, ValueError):
                    candidate_pid = None
                if candidate_pid is not None and candidate_pid > 0:
                    target_pid = candidate_pid
            candidate_create_time = target.get("create_time")
            if not isinstance(candidate_create_time, bool):
                try:
                    candidate_create_time = float(candidate_create_time)
                except (TypeError, ValueError):
                    candidate_create_time = None
                if (candidate_create_time is not None and
                        math.isfinite(candidate_create_time)):
                    target_create_time = candidate_create_time
        status: dict[str, Any] = {
            "ts": time.time(),
            "reason": str(reason),
            "result": str(result),
            "target_pid": target_pid,
            "peer": _status_peer(peer),
            "server_port": _safe_port(server_port),
        }
        if target_create_time is not None:
            status["target_create_time"] = target_create_time
        if detail:
            status["detail"] = str(detail)
        status_path = Path(str(log_file) + ".status.json").expanduser()
        _write_status(status_path, status)
    except Exception:
        return


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError("model capture body must be bytes-like")


def _bounded(value: bytes) -> tuple[bytes, bool]:
    if len(value) <= MAX_BODY_BYTES:
        return value, False
    return value[:MAX_BODY_BYTES], True


def _body_value(value: bytes) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = value.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return redact(text)
    # Model request/response envelopes are objects.  Keep other valid JSON
    # values as text so the field remains compatible with the documented dict
    # or text contract and no shape is invented.
    if isinstance(parsed, dict):
        return redact(parsed)
    return redact(text)


def _append_record(path: Path, record: dict[str, Any]) -> bool:
    try:
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow:
            flags |= no_follow
        with _WRITE_LOCK:
            fd = os.open(str(path), flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        return False
                    offset += written
            finally:
                os.close(fd)
        return True
    except (OSError, TypeError, ValueError):
        return False


class Capture:
    """One frozen target instance's proxy request/response capture."""

    def __init__(self, target: dict[str, Any], log_path: Path):
        self.pid = int(target["pid"])
        self.create_time = float(target["create_time"])
        self.log_path = Path(log_path).expanduser()
        self.request_id = uuid.uuid4().hex
        self._response = bytearray()
        self._response_truncated = False
        self._request_recorded = False
        self._finished = False

    def _record(self, event: str, body: bytes, *, body_complete: bool,
                truncated: bool, metadata: Any = None, error: Any = None) -> None:
        timestamp = time.time()
        record: dict[str, Any] = {
            "event": event,
            "pid": self.pid,
            "create_time": self.create_time,
            "ts": timestamp,
            "timestamp": timestamp,
            "request_id": self.request_id,
            "capture_layer": "transport",
            "source": SOURCE,
            "body": _body_value(body),
            "body_complete": bool(body_complete),
            "truncated": bool(truncated),
        }
        record.update(_safe_metadata(metadata))
        if error is not None:
            safe_error = _safe_text(str(error))
            if safe_error is not None:
                record["error"] = safe_error
        try:
            _append_record(self.log_path, record)
        except OSError:
            # A recorder failure must never turn into a proxy forwarding
            # failure, including when the low-level writer is replaced by a
            # caller or test double.
            return

    def request(self, body: bytes, metadata: dict[str, Any] | None = None) -> None:
        if self._request_recorded:
            return
        self._request_recorded = True
        try:
            raw = _as_bytes(body)
        except (TypeError, ValueError):
            raw = b""
        retained, truncated = _bounded(raw)
        self._record("model.request", retained, body_complete=not truncated,
                     truncated=truncated, metadata=metadata)

    def feed(self, chunk: bytes) -> bytes:
        """Buffer at most the bounded prefix and return the original chunk."""
        if self._finished:
            return chunk
        try:
            raw = _as_bytes(chunk)
        except (TypeError, ValueError):
            return chunk
        if not raw:
            return chunk
        remaining = MAX_BODY_BYTES - len(self._response)
        if remaining > 0:
            self._response.extend(raw[:remaining])
        if len(raw) > max(remaining, 0):
            self._response_truncated = True
        return chunk

    def finish(self, complete: bool = True, error: Any = None,
               metadata: dict[str, Any] | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        body = bytes(self._response)
        body_complete = bool(complete) and not self._response_truncated and error is None
        self._record("model.response", body, body_complete=body_complete,
                     truncated=self._response_truncated, metadata=metadata, error=error)


def begin_capture(*, peer: Any = None, server_port: Any = None) -> Capture | None:
    """Begin capture only for an explicitly enabled, connected target instance.

    ``peer`` and ``server_port`` are required to prove that the proxy handler's
    client connection belongs to the configured target.  Omitting either value
    intentionally disables capture rather than risking shared-proxy leakage.
    """
    target_file = os.environ.get(TARGET_FILE_ENV, "").strip()
    log_file = os.environ.get(LOG_ENV, "").strip()
    # Without a configured log path there is nowhere safe to put diagnostics;
    # keep the fully disabled path side-effect free.
    if not log_file:
        return None
    if not target_file:
        _note_status(log_file, reason="config_invalid", target=None,
                     peer=peer, server_port=server_port, result="rejected",
                     detail="target_path_missing")
        return None

    try:
        target_path = Path(target_file).expanduser()
    except (OSError, TypeError, ValueError):
        _note_status(log_file, reason="config_invalid", target=None,
                     peer=peer, server_port=server_port, result="rejected",
                     detail="target_path_invalid")
        return None
    target, target_detail = _read_target_file(target_path)
    if not target:
        _note_status(log_file, reason="config_invalid", target=None,
                     peer=peer, server_port=server_port, result="rejected",
                     detail=target_detail)
        return None

    if peer is None or server_port is None:
        _note_status(log_file, reason="connection_context_missing",
                     target=target, peer=peer, server_port=server_port,
                     result="rejected")
        return None
    if _endpoint(peer) is None or _safe_port(server_port) is None:
        _note_status(log_file, reason="connection_context_invalid",
                     target=target, peer=peer, server_port=server_port,
                     result="rejected")
        return None

    try:
        process = psutil.Process(target["pid"])
    except psutil.NoSuchProcess:
        _note_status(log_file, reason="pid_unavailable", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except psutil.AccessDenied:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except PermissionError:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except (OSError, psutil.Error):
        _note_status(log_file, reason="pid_query_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None

    try:
        is_running = getattr(process, "is_running", None)
        if callable(is_running) and not is_running():
            _note_status(log_file, reason="pid_unavailable", target=target,
                         peer=peer, server_port=server_port, result="rejected",
                         detail="process_not_running")
            return None
    except psutil.NoSuchProcess:
        _note_status(log_file, reason="pid_unavailable", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except psutil.AccessDenied:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except PermissionError:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except (OSError, psutil.Error):
        _note_status(log_file, reason="pid_query_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None

    try:
        live_create_time = float(process.create_time())
    except psutil.NoSuchProcess:
        _note_status(log_file, reason="pid_unavailable", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except psutil.AccessDenied:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except PermissionError:
        _note_status(log_file, reason="pid_permission_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    except (OSError, TypeError, ValueError, OverflowError, psutil.Error):
        _note_status(log_file, reason="pid_query_error", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None
    if not math.isfinite(live_create_time):
        _note_status(log_file, reason="pid_query_error", target=target,
                     peer=peer, server_port=server_port, result="rejected",
                     detail="create_time_invalid")
        return None
    if abs(live_create_time - target["create_time"]) > CREATE_TIME_TOLERANCE:
        _note_status(log_file, reason="pid_mismatch", target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None

    connected, connection_reason = _connection_result(process, peer, server_port)
    if not connected:
        _note_status(log_file, reason=connection_reason, target=target,
                     peer=peer, server_port=server_port, result="rejected")
        return None

    _note_status(log_file, reason="capture_started", target=target,
                 peer=peer, server_port=server_port, result="started")
    return Capture(target, Path(log_file).expanduser())


__all__ = ["MAX_BODY_BYTES", "Capture", "begin_capture"]
