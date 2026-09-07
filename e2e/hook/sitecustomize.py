"""Generic runtime observation hook.

Only public SDK surfaces are wrapped. No target harness or vendor identity is
used for discovery. Hook failures are swallowed so the original call continues.
Enable with PYTHONPATH plus ASG_ADAPTER_EVENTS.
"""
import functools
import json
import os
from datetime import datetime, timezone

_EVENTS = os.environ.get("ASG_ADAPTER_EVENTS", "")
_SOURCE = "auto-runtime-v2"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _emit(ev):
    if not _EVENTS:
        return
    try:
        with open(_EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _safe(o):
    try:
        json.dumps(o)
        return o
    except Exception:
        return str(o)[:2000]


def _wrap(fn, kind):
    @functools.wraps(fn)
    def inner(*a, **k):
        try:
            _emit({"ts": _now(), "event_type": "llm.request",
                   "adapter_source": _SOURCE, "sdk": kind,
                   "model": k.get("model"), "messages": _safe(k.get("messages")),
                   "tools": _safe(k.get("tools"))})
        except Exception:
            pass
        try:
            resp = fn(*a, **k)
        except Exception as e:
            try:
                _emit({"ts": _now(), "event_type": "llm.error",
                       "adapter_source": _SOURCE, "sdk": kind,
                       "error": str(e)[:300]})
            except Exception:
                pass
            raise
        try:
            content, tcs = None, None
            if hasattr(resp, "choices") and resp.choices:
                m = resp.choices[0].message
                content = getattr(m, "content", None)
                tcs = getattr(m, "tool_calls", None)
                if tcs is not None:
                    tcs = [{"name": t.function.name,
                            "args": t.function.arguments} for t in tcs]
            _emit({"ts": _now(), "event_type": "llm.response",
                   "adapter_source": _SOURCE, "sdk": kind,
                   "content": content, "tool_calls": _safe(tcs)})
        except Exception:
            pass
        return resp
    return inner


# --- openai (生产 agent 最常用的公共面; 包资源类, 影响全部实例, 无需构造 client) ---
try:
    from openai.resources.chat import completions as _cc
    _cc.Completions.create = _wrap(_cc.Completions.create, "openai-chat")
except Exception:
    pass
try:
    from openai.resources import responses as _rs
    _rs.Responses.create = _wrap(_rs.Responses.create, "openai-responses")
except Exception:
    pass
# Optional generic adapter surfaces (installed modules are detected at runtime).
try:
    import litellm as _llm
    _llm.completion = _wrap(_llm.completion, "completion-api")
except Exception:
    pass
