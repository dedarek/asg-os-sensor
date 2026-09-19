"""Bounded persistence for evidence-backed, partial investigation findings."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ASSET_NAMES = ("model_gateway", "mcp", "skills", "rules")
ASSET_STATUSES = {"collected", "empty", "failed", "unsupported", "unknown", "not_collected"}
IDENTITY_STATUSES = {"identified", "unknown"}
IDENTITY_ROLES = {"agent", "model_gateway", "tool_service", "host", "other", "unknown"}
MAX_FINDING_BYTES = 128 * 1024  # Structured inventories may contain many entry paths/descriptions.
MAX_HISTORY = 64
MAX_OPEN_QUESTIONS = 32
_THREAD_LOCK = threading.Lock()

try:  # POSIX
    import fcntl  # type: ignore
    _POSIX = True
except ImportError:  # pragma: no cover
    _POSIX = False

try:  # Windows
    import msvcrt  # type: ignore
    _WINDOWS = not _POSIX
except ImportError:  # pragma: no cover
    _WINDOWS = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def _bounded_list(value: Any, field: str, limit: int = 32) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return [item[:500] for item in value[:limit]]


def _validate_target(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict) or target.get("pid") is None or target.get("create_time") is None:
        raise ValueError("finding target must contain pid and create_time")
    return {"pid": int(target["pid"]), "create_time": float(target["create_time"])}


def validate_finding(finding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(finding, dict):
        raise ValueError("finding must be an object")
    kind = finding.get("kind")
    if kind == "identity":
        status = finding.get("status")
        if status not in IDENTITY_STATUSES:
            raise ValueError("identity status must be identified or unknown")
        value = finding.get("value")
        if not isinstance(value, (dict, str)):
            raise ValueError("identity value must be an object or string")
        if isinstance(value, dict):
            roles = value.get("roles")
            if roles is not None:
                if (not isinstance(roles, list) or not roles
                        or not all(isinstance(role, str) for role in roles)
                        or any(role not in IDENTITY_ROLES for role in roles)):
                    raise ValueError("identity roles must be a non-empty list of: "
                                     + ", ".join(sorted(IDENTITY_ROLES)))
                if len(roles) != len(set(roles)):
                    raise ValueError("identity roles must not repeat")
                reasoning = value.get("role_reasoning")
                if reasoning is not None and not isinstance(reasoning, (str, list)):
                    raise ValueError("role_reasoning must be a string or list of strings")
    elif kind == "asset":
        asset = finding.get("asset")
        if asset not in ASSET_NAMES:
            raise ValueError("unknown asset group")
        status = finding.get("status")
        if status not in ASSET_STATUSES:
            raise ValueError("invalid asset status")
        value = finding.get("value")
        if value is not None and not isinstance(value, (dict, list, str, int, float, bool)):
            raise ValueError("asset value must be JSON data")
    else:
        raise ValueError("finding kind must be identity or asset")
    display = finding.get('display')
    if display is not None:
        if not isinstance(display, dict) or display.get('version') != 1:
            raise ValueError('display must be {version:1, summary:string, facts:[{label,value}], scope:string}')
        for field in ('summary', 'scope'):
            if not isinstance(display.get(field), str):
                raise ValueError('display.' + field + ' must be a string')
        facts = display.get('facts')
        if not isinstance(facts, list) or len(facts) > 20:
            raise ValueError('display.facts must be a list of at most 20 facts')
        if any(not isinstance(f, dict) or not isinstance(f.get('label'), str)
               or not isinstance(f.get('value'), str) for f in facts):
            raise ValueError('each display fact requires string label and value')
    sources = finding.get("evidence_refs")
    if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
        raise ValueError("finding evidence_refs are required")
    result = deepcopy(finding)
    result["evidence_refs"] = sources[:16]
    result["uncertainty"] = _bounded_list(finding.get("uncertainty"), "uncertainty")
    result["open_questions"] = _bounded_list(finding.get("open_questions"), "open_questions")
    if _json_size(result) > MAX_FINDING_BYTES:
        raise ValueError("finding is too large")
    return result


def load(path: Path, target: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"investigation findings unreadable: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError("investigation findings must be an object")
    if target is not None and value.get("target") != _validate_target(target):
        raise ValueError("investigation findings belong to another target instance")
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


class _FileLock:
    """Serialize read-modify-write across Goose extension processes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(str(path) + ".lock")

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if _POSIX:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        elif _WINDOWS:  # pragma: no cover - exercised on Windows CI
            if self.handle.seek(0, 2) == 0:
                self.handle.write(b'0'); self.handle.flush()
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            if _POSIX:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            elif _WINDOWS:  # pragma: no cover - exercised on Windows CI
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()


def save_finding(path: Path, target: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any]:
    target_value = _validate_target(target)
    clean = validate_finding(finding)
    with _THREAD_LOCK:
        with _FileLock(path):
            current = load(path, target=target_value) or {
                "version": 1,
                "target": target_value,
                "findings": {"identity": None, "assets": {}},
                "history": [],
                "open_questions": [],
            }
            current.setdefault("findings", {"identity": None, "assets": {}})
            current["findings"].setdefault("assets", {})
            if clean["kind"] == "identity":
                key = "identity"
            else:
                key = clean["asset"]
            old = current["findings"].get(key) if key == "identity" else current["findings"]["assets"].get(key)
            clean["submitted_at"] = _now()
            clean["source"] = "goose"
            if key == "identity":
                current["findings"]["identity"] = clean
            else:
                current["findings"]["assets"][key] = clean
            history = current.setdefault("history", [])
            if isinstance(old, dict):
                history.append(deepcopy(old))
            history.append(deepcopy(clean))
            current["history"] = history[-MAX_HISTORY:]
            questions = current.setdefault("open_questions", [])
            for question in clean.get("open_questions", []):
                if question not in questions:
                    questions.append(question)
            current["open_questions"] = questions[-MAX_OPEN_QUESTIONS:]
            current["updated_at"] = _now()
            _atomic_write(path, current)
            return current
