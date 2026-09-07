"""End-to-end supervisor for an anonymous, previously unseen runtime.

The model loop is Goose. This file only orchestrates OS discovery, starts the
independent Analyst, verifies its proposal, applies the least-invasive stream
adapter, and records first-seen versus remembered behavior.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "e2e" / "artifacts" / "unknown-runtime"
RECIPE = ROOT / "recipes" / "runtime_analyst.yaml"
GOOSE = Path.home() / ".local" / "bin" / "goose.exe"
if not GOOSE.exists():
    GOOSE = Path(shutil.which("goose") or shutil.which("goose.exe") or "goose")

sys.path.insert(0, str(ROOT))
from runtime import analyzer, matcher  # noqa: E402


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def redact(s: Any) -> Any:
    if isinstance(s, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)(key|token|secret|password|cookie|authorization)", str(k)) else redact(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [redact(v) for v in s]
    if isinstance(s, str):
        s = re.sub(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b", "[REDACTED]", s)
        return re.sub(r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", s)
    return s


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redact(value), ensure_ascii=False) + "\n")


def target_argv(stream: Path, duration: float) -> list[str]:
    raw = os.environ.get("ASG_TARGET_ARGV_JSON", "")
    if raw:
        argv = json.loads(raw)
        if not isinstance(argv, list) or not argv:
            raise ValueError("ASG_TARGET_ARGV_JSON must be a non-empty JSON argv list")
        return [str(x) for x in argv]
    # Default autonomous target if not overridden by ASG_TARGET_ARGV_JSON
    target_exe = os.environ.get("ASG_TARGET_EXE", "")
    if target_exe and Path(target_exe).exists():
        # Generic long-running agent command for an external CLI target
        return [
            target_exe,
            "--bare",
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", "dontAsk",
            "--no-session-persistence",
            "Perform a detailed workspace check: list files, summarize the findings, and ensure all checks are complete."
        ]
    return [sys.executable, str(ROOT / "e2e" / "unknown_harness.py"), "--model", "runtime-model", "--output-format", "jsonl", "--system-prompt", "runtime-system", "--duration", str(duration), "--stream", str(stream)]


def wait_for_event(path: Path, predicate, timeout: float = 10.0) -> dict[str, Any] | None:
    deadline = now() + timeout
    while now() < deadline:
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    event = json.loads(line)
                    if predicate(event):
                        return event
                except json.JSONDecodeError:
                    if predicate(line):
                        return {"raw": line}
        time.sleep(0.15)
    return None


def start_sensor(run_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["ASG_EVENTS_FILE"] = str(run_dir / "sensor_events.jsonl")
    env["ASG_AGENT_SCORE_THRESHOLD"] = "50"
    return subprocess.Popen([sys.executable, str(ROOT / "asg_os_sensor.py"), "--interval", "0.15"], cwd=ROOT, env=env, stdout=(run_dir / "sensor_stdout.log").open("w", encoding="utf-8"), stderr=subprocess.STDOUT, text=True)


def start_target(run_dir: Path, duration: float) -> tuple[subprocess.Popen, Path, list[str]]:
    stream = run_dir / "target_stream.jsonl"
    argv = target_argv(stream, duration)
    env = os.environ.copy()
    env["ASG_TARGET_STREAM_FILE"] = str(stream)
    stdout_path = run_dir / "target_stdout.log"
    mirror = os.environ.get("ASG_MIRROR_TARGET_STDOUT", "0") == "1"
    if not mirror:
        stdout = stdout_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=stdout, stderr=subprocess.STDOUT, text=True)
        return proc, stream, argv
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    def drain() -> None:
        with stdout_path.open("w", encoding="utf-8") as log:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(redact(line))
                log.flush()
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type"):
                    append_jsonl(stream, item)
    threading.Thread(target=drain, name="target-output-mirror", daemon=True).start()
    return proc, stream, argv


def analyst_route() -> dict[str, str]:
    route = os.environ.get("ASG_ANALYST_ROUTE", "opencode-go").strip().lower()
    routes = {
        "commandcode": {
            "provider": "openai",
            "model": "deepseek/deepseek-v4-flash",
            "base_url": "https://api.commandcode.ai/provider/v1",
            "key_env": "COMMANDCODE_API_KEY",
        },
        "opencode-go": {
            "provider": "openai",
            "model": "deepseek-v4-flash",
            "base_url": "https://opencode.ai/zen/go/v1",
            "key_env": "OPENCODE_GO_API_KEY",
        },
    }
    if route not in routes:
        raise ValueError(f"ASG_ANALYST_ROUTE must be commandcode or opencode-go, got {route!r}")
    return {"route": route, **routes[route]}


def run_analyst(run_dir: Path, pid: int, stream: Path) -> tuple[int, list[str]]:
    route = analyst_route()
    key = os.environ.get(route["key_env"], "")
    if not key:
        raise RuntimeError(f"missing active credential: {route['key_env']}")
    extension = "asg-runtime-tools:ASG_TARGET_PID={pid} ASG_AUDIT_DIR={audit} ASG_RECIPE_DIR={recipes} ASG_TARGET_STREAM_FILE={stream} python runtime/analyst_tools.py".format(pid=pid, audit=run_dir, recipes=run_dir / "recipes", stream=stream)
    # Goose remains the mature loop; the provider is OpenAI-compatible and the
    # only permitted model is DeepSeek. Secrets stay in the child environment.
    cmd = [str(GOOSE), "run", "--no-profile", "--no-session", "--recipe", str(RECIPE), "--params", f"target_pid={pid}", "--provider", route["provider"], "--model", route["model"], "--max-turns", "12", "--max-tool-repetitions", "2", "--output-format", "stream-json", "--with-extension", extension]
    env = os.environ.copy()
    env["GOOSE_PROVIDER"] = route["provider"]
    env["GOOSE_MODEL"] = route["model"]
    env["GOOSE_MODE"] = "auto"
    env["OPENAI_BASE_URL"] = route["base_url"]
    env["OPENAI_API_KEY"] = key
    if route["route"] == "opencode-go":
        # OpenCode Go requires a stable session-routing header; Goose exposes
        # custom OpenAI headers through a comma-separated setting. This value is per Analyst run,
        # never a user credential.
        env["OPENAI_CUSTOM_HEADERS"] = f"x-opencode-session=asg-runtime-{pid},x-opencode-client=asg-runtime-analyst"
    out_path, err_path = run_dir / "analyst_stdout.jsonl", run_dir / "analyst_stderr.log"
    completed = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out_path.open("w", encoding="utf-8"), stderr=err_path.open("w", encoding="utf-8"), text=True, timeout=180)
    write_json(run_dir / "analyst_command.json", {"argv": cmd, "environment": {"GOOSE_PROVIDER": route["provider"], "GOOSE_MODEL": route["model"], "OPENAI_BASE_URL": route["base_url"], "credential_env": route["key_env"], "GOOSE_MODE": "auto"}, "returncode": completed.returncode})
    return completed.returncode, cmd


def apply_stream_adapter(run_dir: Path, stream: Path) -> dict[str, Any]:
    out = run_dir / "semantic_events.jsonl"
    if not stream.exists():
        return {"mounted": False, "reason": "no supervisor-visible structured stream"}
    env = os.environ.copy()
    env["ASG_ADAPTER_EVENTS"] = str(out)
    cp = subprocess.run([sys.executable, str(ROOT / "runtime" / "stream_parser.py")], cwd=ROOT, env=env, stdin=stream.open("r", encoding="utf-8", errors="ignore"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    event_types = []
    if out.exists():
        for line in out.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                event_types.append(json.loads(line).get("event_type"))
            except json.JSONDecodeError:
                pass
    return {"mounted": cp.returncode == 0, "parser_stdout": cp.stdout.strip(), "parser_stderr": cp.stderr.strip(), "event_count": len(event_types), "event_types": event_types, "source": str(stream), "adapter": "auto-runtime-stream"}


def verify_candidate(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "recipes" / "candidate.json"
    if not path.exists():
        raise RuntimeError("Analyst finished without propose_recipe candidate")
    payload = json.loads(path.read_text(encoding="utf-8"))
    recipe = payload.get("recipe")
    if not isinstance(recipe, dict):
        raise RuntimeError("candidate recipe is not an object")
    required = {"match_features", "observation", "hook", "fallback"}
    missing = sorted(required - set(recipe))
    if missing:
        raise RuntimeError("candidate recipe missing: " + ", ".join(missing))
    forbidden = json.dumps(recipe, ensure_ascii=False).lower()
    if any(x in forbidden for x in ("shell", "powershell", "cmd.exe", "api_key", "access_token", "private_key")):
        raise RuntimeError("candidate contains forbidden execution or secret material")
    committed = {"status": "committed", "committed_at": iso(), "recipe": recipe}
    write_json(run_dir / "recipes" / "committed.json", committed)
    return committed


def tool_call_summary(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / "analyst_tool_calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]


def run_phase(label: str, duration: float = 18.0) -> dict[str, Any]:
    run_dir = RUN_ROOT / label
    run_dir.mkdir(parents=True, exist_ok=True)
    sensor = start_sensor(run_dir)
    target = None
    started = now()
    try:
        # 等待 sensor 完成首轮 baseline 预热 (见 sensor_stdout 打印 -- scan#1)
        wait_for_event(run_dir / "sensor_stdout.log", lambda line: "-- scan#1" in line if isinstance(line, str) else False, 20)
        time.sleep(1.0)
        target, stream, argv = start_target(run_dir, duration)
        pid = target.pid
        identified = wait_for_event(run_dir / "sensor_events.jsonl", lambda e: e.get("event_type") == "proc.agent_identified" and (e.get("pid") == pid or any(p in str(e.get("cmdline", "")) for p in ["--permission-mode", "--bare"])), 40)
        if not identified:
            raise RuntimeError(f"sensor did not identify target pid={pid}; see {run_dir / 'sensor_stdout.log'}")
        struct = analyzer.analyze(pid)
        matched, match_ms = matcher.match(struct)
        result: dict[str, Any] = {"label": label, "pid": pid, "argv": argv, "identified": identified, "structure": struct, "matcher": {"hit": bool(matched), "elapsed_ms": match_ms}, "started_at": iso(started)}
        if matched:
            result["path"] = "remembered-recipe"
            result["recipe_source"] = "runtime/fingerprints.json"
            result["adapter"] = apply_stream_adapter(run_dir, stream)
        else:
            analyst_started = now()
            rc, analyst_cmd = run_analyst(run_dir, pid, stream)
            candidate = verify_candidate(run_dir)
            result["path"] = "first-seen-autonomous-investigation"
            result["analyst"] = {"returncode": rc, "elapsed_ms": int((now() - analyst_started) * 1000), "command": analyst_cmd, "tool_calls": tool_call_summary(run_dir)}
            result["candidate"] = candidate
            mount_started = now()
            result["adapter"] = apply_stream_adapter(run_dir, stream)
            entry = matcher.remember(struct, candidate["recipe"], int((now() - mount_started) * 1000))
            result["memory_entry"] = entry
        result["duration_ms"] = int((now() - started) * 1000)
        write_json(run_dir / "phase.json", result)
        return result
    finally:
        if target is not None and target.poll() is None:
            target.terminate()
            try:
                target.wait(timeout=3)
            except subprocess.TimeoutExpired:
                target.kill()
        if sensor.poll() is None:
            # Windows Popen has no SIGINT implementation; terminate is the
            # portable Supervisor shutdown path for the child daemon.
            sensor.terminate()
            try:
                sensor.wait(timeout=4)
            except subprocess.TimeoutExpired:
                sensor.kill()


def build_execution_manifest(report: dict[str, Any]) -> dict[str, Any]:
    files = []
    for p in sorted(RUN_ROOT.rglob("*")):
        if p.is_file():
            files.append(str(p.relative_to(RUN_ROOT)).replace("\\", "/"))
    return {
        "purpose": "blind unknown-runtime discovery and iterative attachment",
        "model_constraint": "DeepSeek-only Analyst; target identity is not supplied to Analyst",
        "blind_boundary": {
            "analyst_receives": ["behavioral process shape", "tree depth/count", "runtime capability shapes", "event types and content-block types", "redacted prior memory"],
            "analyst_does_not_receive": ["vendor/product identity", "executable path/name", "raw cwd/open paths", "prompt bodies", "tool arguments", "credentials"],
            "raw_supervisor_evidence": ["sensor_events.jsonl", "sensor_stdout.log", "target_stream.jsonl", "target_stdout.log"]
        },
        "phases": {
            "first": {"path": "first", "path_taken": report["first"].get("path"), "matcher": report["first"].get("matcher"), "tool_call_count": len(report["first"].get("analyst", {}).get("tool_calls", [])), "recipe": "first/recipes/committed.json"},
            "second": {"path": "second", "path_taken": report["second"].get("path"), "matcher": report["second"].get("matcher"), "memory": "runtime/fingerprints.json"}
        },
        "replay_files": files,
        "report": "run_report.json"
    }


def main() -> int:
    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    (ROOT / "runtime" / "fingerprints.json").write_text(json.dumps({"fingerprints": [], "version": 1}, indent=1), encoding="utf-8")
    first = run_phase("first", 180.0)
    second = run_phase("second", 60.0)
    report = {"run_id": iso(), "root": str(RUN_ROOT), "first": first, "second": second, "assertions": {"first_identified": bool(first.get("identified")), "first_was_miss": first.get("matcher", {}).get("hit") is False, "analyst_called_tools": len(first.get("analyst", {}).get("tool_calls", [])) > 0, "candidate_committed": bool(first.get("candidate")), "semantic_events_captured": first.get("adapter", {}).get("event_count", 0) >= 2, "second_was_hit": second.get("matcher", {}).get("hit") is True, "second_used_memory": second.get("path") == "remembered-recipe", "no_forbidden_tool": not any(c.get("tool") in {"shell", "developer", "filesystem"} for c in first.get("analyst", {}).get("tool_calls", []))}}
    report["passed"] = all(report["assertions"].values())
    write_json(RUN_ROOT / "run_report.json", report)
    write_json(RUN_ROOT / "execution_manifest.json", build_execution_manifest(report))
    print(json.dumps({"passed": report["passed"], "root": str(RUN_ROOT), "assertions": report["assertions"], "first_ms": first.get("duration_ms"), "second_ms": second.get("duration_ms"), "tool_calls": len(first.get("analyst", {}).get("tool_calls", [])), "event_types": first.get("adapter", {}).get("event_types", [])}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
