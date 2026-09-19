"""Start/stop the standalone Hook runtime using an explicit settings file.

This is intentionally separate from ``service.py``: the Hook runtime does not
scan processes, run investigations or learn recipes.  It only serves already
bound Hook data and answers control decisions, so it can keep running after the
discovery pipeline is stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent


def _live(record: Path):
    """Return this deployment's own process, or None if it is gone."""
    if not record.exists():
        return None
    try:
        meta = json.loads(record.read_text())
        candidate = psutil.Process(int(meta["pid"]))
        if abs(candidate.create_time() - float(meta["create_time"])) >= .001:
            return None
        command = " ".join(candidate.cmdline())
        if "hook_runtime.py" not in command and "hook_service" not in command:
            raise RuntimeError("Deployment process identity does not match this service")
        return candidate
    except psutil.NoSuchProcess:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    run = Path(settings["ASG_RUN_DIR"])
    run.mkdir(parents=True, exist_ok=True)
    record = run / "hook-runtime-deployment.json"
    process = _live(record)

    if args.action == "status":
        print("running PID %s" % process.pid if process else "stopped")
        return
    if args.action == "stop":
        if process:
            process.terminate()
            process.wait(timeout=10)
            record.unlink(missing_ok=True)
        print("stopped")
        return
    if process:
        print("already running PID %s" % process.pid)
        return

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ASG_"):
            env.pop(key)
    env.update(settings)
    with (run / "hook-runtime.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, "-u", "-B", "-m", "runtime.hook_service"],
                                 cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    record.write_text(json.dumps({"pid": child.pid,
                                  "create_time": psutil.Process(child.pid).create_time(),
                                  "port": int(settings.get("ASG_HOOK_PORT", 8099))}))
    print("started http://%s:%s/" % (settings.get("ASG_HOOK_HOST", "0.0.0.0"),
                                     settings.get("ASG_HOOK_PORT", 8099)))


if __name__ == "__main__":
    main()
