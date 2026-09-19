"""Translate producer-native Hook events into the shared ASG vocabulary.

Producers do not share an event contract.  The Codex hooks engine emits
``UserPromptSubmit`` / ``SessionStart`` / ``Stop``, the ASG observer emits
``user.prompt.submitted``, and opencode emits ``user.input`` with nested
``detail.parts``.  Without one translation point the dashboard and the
input/output acceptance check simply do not see the user's own words.

This module never invents data.  A native name it cannot map keeps its original
label and is reported as unmapped, so a new producer is visible rather than
silently dropped or reclassified into a category it does not belong to.
"""
from __future__ import annotations

import json
from typing import Any

# Canonical vocabulary, mirroring runtime/observation_source.CANONICAL_EVENTS.
CANONICAL_EVENTS = ("hook.loaded", "tool.execute.before", "tool.execute.after",
                    "user.input", "assistant.output", "model.request", "model.response",
                    "session.start", "session.end", "tool.error", "control.applied", "io.turn.end")

# The two conversation roles a person actually reads.
CONVERSATION_EVENTS = ("user.input", "assistant.output")

# Producer-native names observed in real payloads plus documented generic
# aliases.  Keys are the lowercase, separator-stripped form of the native name.
_ALIASES = {
    # Codex hooks engine event names and the ASG observer's emitted names.
    "sessionstart": "hook.loaded",
    "userpromptsubmit": "user.input",
    "user.prompt.submitted": "user.input",
    "pretooluse": "tool.execute.before",
    "posttooluse": "tool.execute.after",
    "stop": "assistant.output",
    "permissionrequest": "permission.request",
    "precompact": "compact.before",
    "postcompact": "compact.after",
    "subagentstart": "subagent.start",
    "subagentstop": "subagent.stop",
    "interrupt": "turn.interrupt",
    # Generic aliases other runtimes use for the same two conversation stages.
    "chat.message": "user.input",
    "userinput": "user.input",
    "message.completed": "assistant.output",
    "completion": "assistant.output",
}


def _key(name: Any) -> str:
    """Fold a native event name for comparison: lowercase, drop separators."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum() or ch == ".")


_CANONICAL_BY_KEY = {_key(name): name for name in CANONICAL_EVENTS}


def canonical(name: Any) -> str | None:
    """Return the canonical kind for a native event name, or ``None``.

    ``None`` means "not translated", which callers must surface.  It never
    means "no event": the raw record still exists and keeps its own label.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    return _CANONICAL_BY_KEY.get(_key(name)) or _ALIASES.get(_key(name))


def _payload(record: Any) -> dict:
    payload = record.get("payload") if isinstance(record, dict) else None
    return payload if isinstance(payload, dict) else {}


def native_name(record: Any) -> str | None:
    """The producer's own label for a record, before translation."""
    payload = _payload(record)
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    for value in (payload.get("event"), payload.get("event_raw"), payload.get("hook_event_name"), detail.get("hook_event_name"),
                  record.get("event_type") if isinstance(record, dict) else None):
        if isinstance(value, str) and value.strip():
            return value
    return None


def record_kind(record: Any) -> str | None:
    """Canonical kind of a Hook record, without raising on unknown producers."""
    return canonical(native_name(record))


def unmapped(records: Any) -> list[str]:
    """Native names present in the window that have no canonical translation."""
    names = set()
    for record in records or []:
        name = native_name(record)
        if name and canonical(name) is None:
            names.add(name)
    return sorted(names)


def vocabulary_summary(records: Any) -> dict:
    total = mapped = 0
    for record in records or []:
        if native_name(record) is None:
            continue
        total += 1
        if record_kind(record) is not None:
            mapped += 1
    return {"records": total, "mapped": mapped, "unmapped_native_names": unmapped(records)}


def _unwrap(value: Any) -> Any:
    """Unwrap the ``{present, value}`` boxes and JSON-in-string payloads."""
    if isinstance(value, dict) and "present" in value and "value" in value:
        value = value["value"]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _parts_text(parts: Any) -> str | None:
    parts = _unwrap(parts)
    if not isinstance(parts, list):
        return None
    texts = [item.get("text") for item in parts
             if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
    text = "\n".join(t for t in texts if t)
    return text or None


def text_for(payload: Any, role: str) -> dict | None:
    """Extract one role's text from a payload, or ``None`` when absent.

    ``role`` is ``user`` or ``assistant``.  A payload that declares a different
    role is refused instead of guessed at, and keyword-only fields are reported
    so a caller can tell "empty" apart from "not captured by this Hook".
    """
    payload = payload if isinstance(payload, dict) else {}
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    content = _unwrap(payload.get("content"))
    content = content if isinstance(content, dict) else {}
    truncation = payload.get("truncation") if isinstance(payload.get("truncation"), dict) else {}
    truncated = bool(truncation.get("truncated")) or bool(payload.get("truncated"))
    if payload.get("assistant_output_missing") and role == "assistant":
        return None
    message = _unwrap(detail.get("message")) if "message" in detail else _unwrap(content.get("message"))
    message_role = message.get("role") if isinstance(message, dict) else None
    if isinstance(message_role, str) and message_role and message_role != role:
        return None
    direct = (("prompt", payload.get("prompt")), ("detail.prompt", detail.get("prompt")),
              ("content.prompt", content.get("prompt"))) \
        if role == "user" else \
        (("assistant_output", payload.get("assistant_output")),
         ("last_assistant_message", payload.get("last_assistant_message")),
         ("detail.assistant_output", detail.get("assistant_output")),
         ("content.assistant_output", content.get("assistant_output")),
         ("content.text", content.get("text")))
    for source, value in direct:
        if isinstance(value, str) and value.strip():
            return {"text": value, "source": source, "truncated": truncated}
    for source, container in (("detail.parts", detail), ("parts", payload), ("content.parts", content)):
        text = _parts_text(container.get("parts"))
        if text:
            return {"text": text, "source": source, "truncated": truncated}
    if isinstance(content.get('text'), str) and content['text'].strip():
        return {'text': content['text'], 'source': 'content.text', 'truncated': truncated}
    text = _parts_text(payload.get('content'))
    if text:
        return {'text': text, 'source': 'content', 'truncated': truncated}
    if role == "user" and isinstance(content.get("messages"), list):
        texts = []
        for item in content["messages"]:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            text = _parts_text(item.get("content"))
            if text:
                texts.append(text)
        if texts:
            return {"text": "\n".join(texts), "source": "content.messages", "truncated": truncated}
    for source, value in (("detail.text", detail.get("text")), ("text", payload.get("text")),
                          ("content", payload.get("content"))):
        if isinstance(value, str) and value.strip():
            return {"text": value, "source": source, "truncated": truncated}
    return None


def conversation(records: Any, *, limit: int | None = None) -> list[dict]:
    """Normalized user/assistant turns from any producer's records.

    Each item keeps its origin field names so the UI can explain where a line
    came from, and duplicate deliveries of the same message are collapsed.
    """
    items: list[dict] = []
    for record in records or []:
        kind = record_kind(record)
        if kind not in CONVERSATION_EVENTS:
            continue
        payload = _payload(record)
        found = text_for(payload, "user" if kind == "user.input" else "assistant")
        if not found:
            continue
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        message = _unwrap(detail.get("message"))
        message = message if isinstance(message, dict) else {}
        items.append({
            "role": "user" if kind == "user.input" else "assistant",
            "kind": kind,
            "text": found["text"],
            "text_source": found["source"],
            "truncated": found["truncated"],
            "timestamp": (record.get("timestamp_iso") if isinstance(record, dict) else None)
                         or payload.get("timestamp"),
            "session_id": payload.get("session_id") or detail.get("session_id") or message.get("sessionID"),
            "agent_id": payload.get("agent_id") or detail.get("agent_id") or message.get("agent"),
            "message_id": payload.get("message_id") or detail.get("message_id") or message.get("id"),
            "instance_id": record.get("instance_id") if isinstance(record, dict) else None,
            "origin_native_event": native_name(record),
        })
    seen = set()
    unique: list[dict] = []
    for item in items:
        key = (item["instance_id"], item["session_id"], item["message_id"], item["role"], item["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[-limit:] if isinstance(limit, int) and limit > 0 else unique


__all__ = ["CANONICAL_EVENTS", "CONVERSATION_EVENTS", "canonical", "conversation",
           "native_name", "record_kind", "text_for", "unmapped", "vocabulary_summary"]
