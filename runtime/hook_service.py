"""Standalone Hook runtime: events and control without the discovery pipeline.

Once a Hook is installed, the Agent's producer keeps appending to its bound log
file.  Serving that data and answering control decisions does not require the
scanner, the investigator or Goose.  This service exists so that "the finding
program can be removed and someone else attaches to this port" is a testable
statement instead of an architecture sketch.

It deliberately reuses the dashboard's bounded readers and its decision engine:
there is no second copy of the data contract that can drift.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from runtime import event_vocabulary, hook_control, hook_data

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8099


def run_dir() -> Path:
    return Path(os.environ.get("ASG_RUN_DIR", "artifacts/stage1/dashboard")).resolve()


def binding_count(run: Path | None = None) -> int:
    """Count bindings without opening every historical event log."""
    path = (run or run_dir()) / "observations.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    return len(data) if isinstance(data, dict) else 0


def record_path(run: Path | None = None) -> Path:
    # Distinct from the launcher's settings file so a running service can never
    # overwrite the deployment configuration it was started from.
    return (run or run_dir()) / "hook-runtime-state.json"


def instances(run: Path | None = None) -> list[dict]:
    """Bound instances this service can already serve, without scanning."""
    from runtime.observation_registry import Registry
    try:
        snapshots = Registry(run or run_dir()).snapshots()
    except (OSError, ValueError):
        return []
    rows = []
    for instance_id, snap in sorted(snapshots.items()):
        if not isinstance(snap, dict):
            continue
        events = snap.get("events") if isinstance(snap.get("events"), dict) else {}
        rows.append({
            "instance_id": instance_id,
            "status": snap.get("status"),
            "target_alive": snap.get("target_alive"),
            "valid_events": events.get("valid"),
            "last_event_time": snap.get("last_event_time"),
            "health": (snap.get("health") or {}).get("status") if isinstance(snap.get("health"), dict) else None,
            "blocking": (snap.get("blocking") or {}).get("status") if isinstance(snap.get("blocking"), dict) else None,
        })
    return rows


def servings(run: Path | None = None) -> dict:
    """Whether this service is running, and which instance it covers."""
    root = run or run_dir()
    try:
        meta = json.loads(record_path(root).read_text())
    except (OSError, ValueError):
        return {"status": "not_running", "reason": "no_runtime_record"}
    try:
        import psutil
        process = psutil.Process(int(meta["pid"]))
        if abs(process.create_time() - float(meta["create_time"])) >= .001:
            return {"status": "not_running", "reason": "stale_pid"}
        command = " ".join(process.cmdline())
        if "hook_runtime.py" not in command and "hook_service" not in command:
            return {"status": "not_running", "reason": "pid_reused"}
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        return {"status": "not_running", "reason": "process_not_found"}
    except Exception:  # psutil error taxonomy differs across platforms
        return {"status": "not_running", "reason": "process_check_failed"}
    # The service's own opt-in is authoritative: the caller (dashboard) has a
    # different environment, so reading its own flag would misreport the port.
    remote = meta.get("remote_enabled")
    if not isinstance(remote, bool):
        remote = hook_control.remote_enabled()
    return {"status": "running", "pid": meta.get("pid"), "started_at": meta.get("started_at"),
            "url": "http://%s:%s" % (meta.get("host"), meta.get("port")),
            "host": meta.get("host"), "port": meta.get("port"),
            "remote_enabled": remote,
            "endpoints": ["/health", "/api/instances", "/api/hook-data", "/api/conversation",
                          "/api/hook-control/status", "/api/hook-control/decision"],
            "instance_count": binding_count(root)}


def _instance_id(row: dict) -> str | None:
    if isinstance(row.get("instance_id"), str) and row["instance_id"]:
        return row["instance_id"]
    if row.get("pid"):
        return "%s:%s" % (row.get("pid"), row.get("create_time"))
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ASGHookRuntime/1.0"

    def log_message(self, *args):  # keep the service log readable
        pass

    def _send(self, code: int, payload: dict, *, download: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", "attachment; filename=hook-data.json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A browser refresh or timed-out client may close while a bounded
            # read is completing.  The request is already over for that client.
            self.close_connection = True

    def _gate(self) -> bool:
        allowed, reason = hook_control.authorized(self)
        if not allowed:
            self._send(403, {"error": reason})
        return allowed

    def do_GET(self):
        if hook_control.handle_request(self):
            return
        if not self._gate():
            return
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/health":
            self._send(200, {
                "status": "ok", "service": "asg-hook-runtime",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_dir": str(run_dir()), "remote_enabled": hook_control.remote_enabled(),
                "instances": binding_count(),
                "note": "独立运行服务：不执行扫描、调查或配方学习",
            })
            return
        if parsed.path == "/api/instances":
            self._send(200, {"instances": instances(), "source": "observation_registry"})
            return
        if parsed.path in ("/api/hook-data", "/api/hook-events",
                           "/api/hook-data/download", "/api/hook-events/download"):
            payload = hook_data.snapshot(
                run_dir=run_dir(),
                pid=query.get("pid", [None])[0],
                create_time=query.get("create_time", [None])[0],
                instance_id=query.get("instance_id", [None])[0] or None,
                since=query.get("since", [None])[0] or None,
                until=query.get("until", [None])[0] or None,
                limit=query.get("limit", [None])[0],
                max_bytes=query.get("max_bytes", [None])[0],
            )
            self._send(400 if payload.get("status") == "invalid_request" else 200, payload,
                       download=parsed.path.endswith("/download"))
            return
        if parsed.path == "/api/conversation":
            raw = hook_data.snapshot(
                run_dir=run_dir(),
                pid=query.get("pid", [None])[0],
                create_time=query.get("create_time", [None])[0],
                instance_id=query.get("instance_id", [None])[0] or None,
                limit=query.get("limit", [None])[0],
            )
            self._send(200, {
                "instance_id": raw.get("filter", {}).get("instance_id"),
                "target": (raw.get("bindings") or [{}])[0].get("target") if raw.get("bindings") else None,
                "conversation": raw.get("conversation", []),
                "vocabulary": raw.get("vocabulary"),
                "coverage": raw.get("coverage"),
            })
            return
        self._send(404, {"error": "unknown endpoint", "endpoints": [
            "/health", "/api/instances", "/api/hook-data", "/api/conversation",
            "/api/hook-control/status"]})

    def do_POST(self):
        from runtime.otlp_ingest import handle as handle_otlp
        if handle_otlp(self): return
        if hook_control.handle_request(self):
            return
        if not self._gate():
            return
        self._send(404, {"error": "unknown endpoint"})


def main() -> None:
    host = os.environ.get("ASG_HOOK_HOST", "").strip() or DEFAULT_HOST
    port = int(os.environ.get("ASG_HOOK_PORT", DEFAULT_PORT))
    token = hook_control.token_path()  # create/verify the shared credential
    meta = {"pid": os.getpid(), "host": host, "port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "run_dir": str(run_dir()),
            "remote_enabled": hook_control.remote_enabled()}
    try:
        import psutil
        meta["create_time"] = psutil.Process(os.getpid()).create_time()
    except Exception:
        meta["create_time"] = None
    record_path().parent.mkdir(parents=True, exist_ok=True)
    record_path().write_text(json.dumps(meta, ensure_ascii=False))
    server = ThreadingHTTPServer((host, port), Handler)
    print("[ASG Hook Runtime] http://%s:%s/ · 绑定实例 %d · 远程访问 %s · 令牌 %s"
          % (host, port, len(instances()), "允许（需令牌）" if hook_control.remote_enabled() else "仅本机", token))
    try:
        server.serve_forever()
    finally:
        record_path().unlink(missing_ok=True)


__all__ = ["Handler", "instances", "main", "record_path", "run_dir", "servings"]


if __name__ == "__main__":
    main()
