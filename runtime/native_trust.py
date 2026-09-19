"""Read-only probe of a target's own Hook trust admission.

Installing a Hook file and having that Hook run are different states.  The Codex
hooks engine skips a non-managed Hook until the current definition hash has been
reviewed and trusted, so "the file is still there after a restart" is not
evidence of activation.  Rather than guessing, this module asks the target's own
binary (``codex app-server`` -> ``hooks/list``) and reports what it says.

The probe is read-only: it starts a throwaway app-server process, sends an
``initialize`` and a ``hooks/list`` request, and exits.  It never approves,
trusts, edits or restarts anything.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CANDIDATE_BINS = ("/Applications/ChatGPT.app/Contents/Resources/codex",)
DEFAULT_NEEDLE = "asg"
DEFAULT_TTL_SECONDS = 60.0
MAX_HOOKS = 200

_PROBE_LOCK = threading.RLock()
_REFRESH_THREAD: threading.Thread | None = None


def root() -> Path:
    return Path(os.environ.get("ASG_RUN_DIR", "artifacts/stage1/dashboard")).resolve()


def cache_path() -> Path:
    return root() / "native-trust.json"


def binary() -> str | None:
    explicit = os.environ.get("ASG_NATIVE_TRUST_BIN", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in CANDIDATE_BINS:
        if Path(candidate).exists():
            return candidate
    return shutil.which("codex")


def needle() -> str:
    return os.environ.get("ASG_NATIVE_TRUST_MATCH", DEFAULT_NEEDLE).strip() or DEFAULT_NEEDLE


def _owned(command, marker: str) -> bool:
    return bool(marker) and isinstance(command, str) and marker.lower() in command.lower()


def _rpc(binary_path: str, cwd: str, timeout: float) -> tuple[list, str | None]:
    """Run the two read-only JSON-RPC calls; return collected lines and an error."""
    process = subprocess.Popen([binary_path, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, bufsize=1)
    lines: list[str] = []
    done = threading.Event()

    def reader():
        try:
            for line in process.stdout:
                lines.append(line.rstrip("\n"))
                if len(lines) >= 500:
                    break
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(payload: dict):
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def wait_for(request_id: int, budget: float) -> bool:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            for line in list(lines):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return True
            time.sleep(0.1)
        return False

    error = None
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "asg-native-trust", "version": "1"}}})
        if not wait_for(1, min(timeout, 15.0)):
            error = "initialize_timeout"
        else:
            send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwd": cwd}})
            if not wait_for(2, timeout):
                error = "hooks_list_timeout"
    except OSError as exc:
        error = "spawn_failed:%s" % type(exc).__name__
    finally:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return lines, error


def _extract(lines: list, request_id: int = 2) -> dict | None:
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id and isinstance(message.get("result"), dict):
            return message["result"]
    return None


def flatten(result: dict) -> list[dict]:
    """Flatten the layered ``hooks/list`` response into per-Hook entries."""
    hooks: list[dict] = []
    layers = result.get("data") if isinstance(result, dict) else None
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        for hook in layer.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            hooks.append({
                "event": hook.get("eventName"),
                "command": hook.get("command"),
                "source_path": hook.get("sourcePath"),
                "source": hook.get("source"),
                "enabled": hook.get("enabled"),
                "managed": hook.get("isManaged"),
                "trust": hook.get("trustStatus"),
                "hash": hook.get("currentHash"),
                "cwd": layer.get("cwd"),
            })
            if len(hooks) >= MAX_HOOKS:
                return hooks
    return hooks


def summarize(hooks: list[dict], marker: str) -> dict:
    owned = [h for h in hooks if _owned(h.get("command"), marker)]
    by_trust = Counter(str(h.get("trust")) for h in owned)
    sources_by_event: dict[str, set] = {}
    for hook in owned:
        event = str(hook.get("event"))
        sources_by_event.setdefault(event, set()).add(str(hook.get("source_path")))
    duplicates = [{"event": event, "sources": sorted(paths)}
                  for event, paths in sorted(sources_by_event.items()) if len(paths) > 1]
    summary = {
        "matched_by": marker,
        "total": len(owned),
        "enabled": sum(1 for h in owned if h.get("enabled") is True),
        "trusted": by_trust.get("trusted", 0),
        "untrusted": by_trust.get("untrusted", 0),
        "unknown_trust": sum(count for trust, count in by_trust.items()
                             if trust not in ("trusted", "untrusted")),
        "by_trust": dict(by_trust),
        "events": sorted(sources_by_event),
        "duplicate_registrations": duplicates,
        "hooks": owned[:MAX_HOOKS],
    }
    if not owned:
        summary["assessment"] = {"status": "none_registered", "label": "未注册我们的 Hook",
                                 "detail": "目标原生配置中没有匹配的 Hook 定义"}
    elif summary["enabled"] == 0:
        summary["assessment"] = {"status": "disabled", "label": "已注册但被禁用",
                                 "detail": "全部匹配 Hook 的 enabled=false"}
    elif summary["trusted"] == len(owned):
        summary["assessment"] = {"status": "trusted", "label": "已通过原生信任准入",
                                 "detail": "全部匹配 Hook 的 trustStatus=trusted"}
    elif summary["trusted"] > 0:
        summary["assessment"] = {"status": "mixed", "label": "部分通过原生信任准入",
                                 "detail": "%d 个 trusted，%d 个 untrusted" % (summary["trusted"], summary["untrusted"])}
    else:
        summary["assessment"] = {
            "status": "blocked_untrusted", "label": "被原生信任准入阻断",
            "detail": "%d 个匹配 Hook 全部 untrusted；重新加载同一配置仍会被跳过" % len(owned)}
    return summary


def probe(*, cwd: str | None = None, timeout: float = 25.0, marker: str | None = None) -> dict:
    """Run one read-only trust probe and return a bounded, explainable report."""
    started = time.monotonic()
    marker = marker or needle()
    target_cwd = cwd or os.getcwd()
    result: dict = {
        "status": "unavailable",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "source": {"kind": "codex-app-server", "method": "hooks/list",
                   "binary": None, "cwd": target_cwd},
        "hooks": [],
        "owned": None,
        "limitations": [
            "只读取目标自身的 Hook 定义与信任状态，不修改、不批准、不重启目标",
            "信任状态是探测时刻的磁盘定义；目标内存中的旧配置可能不同",
            "其他运行时的准入机制不同，此结论不能外推到它们",
        ],
    }
    binary_path = binary()
    result["source"]["binary"] = binary_path
    if not binary_path:
        result["reason"] = "binary_not_found"
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result
    lines, error = _rpc(binary_path, target_cwd, timeout)
    if error:
        result["reason"] = error
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result
    payload = _extract(lines)
    if payload is None:
        result["reason"] = "hooks_list_missing_result"
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result
    hooks = flatten(payload)
    result.update(status="probed", hooks=hooks, owned=summarize(hooks, marker))
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def write_cache(report: dict) -> None:
    from runtime.learned_install import _atomic
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic(path, json.dumps(report, ensure_ascii=False).encode())


def read_cache() -> dict | None:
    try:
        value = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _age_seconds(report: dict) -> float | None:
    stamp = report.get("probed_at") if isinstance(report, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def current(*, ttl: float = DEFAULT_TTL_SECONDS) -> dict:
    """Cached report with its freshness; never blocks on a probe."""
    report = read_cache()
    if report is None:
        return {"status": "not_probed", "reason": "no_cached_probe",
                "label": "尚未探测原生信任状态"}
    age = _age_seconds(report)
    return {**report, "age_seconds": age, "stale": age is None or age > ttl}


def refresh_async(*, cwd: str | None = None, timeout: float = 25.0) -> bool:
    """Start one background probe if none is running.  Returns True if started."""
    global _REFRESH_THREAD
    with _PROBE_LOCK:
        if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
            return False

        def worker():
            try:
                report = probe(cwd=cwd, timeout=timeout)
                write_cache(report)
            except Exception:  # a probe failure must never break the dashboard
                pass

        _REFRESH_THREAD = threading.Thread(target=worker, name="asg-native-trust", daemon=True)
        _REFRESH_THREAD.start()
        return True


def ensure_async(*, ttl: float = DEFAULT_TTL_SECONDS) -> None:
    """Refresh in the background when the cached report is missing or stale."""
    report = read_cache()
    age = _age_seconds(report) if report else None
    if report is None or age is None or age > ttl:
        refresh_async()


__all__ = ["binary", "current", "ensure_async", "flatten", "probe", "read_cache",
           "refresh_async", "summarize", "write_cache"]
