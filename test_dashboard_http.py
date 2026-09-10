# -*- coding: utf-8 -*-
"""原多 Agent 看板 HTTP 与观测适配回归。

这些测试通过真实 ThreadingHTTPServer + urllib 访问，不以直接调用 handler
替代 HTTP；观测端使用随机回环端口的最小本地契约服务，不触碰 8080、全局配置
或真实 Agent。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

import monitor_dashboard as dashboard

ROOT = Path(__file__).resolve().parent
GHOST = ROOT / "runtime" / "opencode" / "ghost_install.py"


def run_ghost(*args):
    return subprocess.run([sys.executable, "-B", str(GHOST), *args],
                          capture_output=True, text=True)


class _ObservationFixture(BaseHTTPRequestHandler):
    health = {
        "status": "stale",
        "healthy": False,
        "reason": "latest event older than ttl",
        "loaded_observed": True,
        "instance_pid": 5297,
        "instance_create_time": 1789006943.640438,
        "capabilities": {
            "observation": {"status": "supported", "label": "事件观测"},
            "blocking": {"status": "unsupported", "label": "未支持"},
        },
    }
    events = {"valid": 3, "invalid": 0, "events": [{"event_type": "hook.loaded"}]}

    def do_GET(self):
        payload = self.health if self.path == "/health" else self.events
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(503 if self.path == "/health" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _RedirectFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://example.invalid/outside")
        self.end_headers()

    def log_message(self, *_args):
        pass


class _MalformedEventsFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = _ObservationFixture.health if self.path == "/health" else {
            "valid": "3", "invalid": 0, "events": []
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class DashboardHttpRegressionTests(unittest.TestCase):
    def setUp(self):
        self.old_state = deepcopy(dashboard.SCAN_STATE)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.MonitorHandler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        dashboard.SCAN_STATE.clear()
        dashboard.SCAN_STATE.update(self.old_state)

    def _get(self, path):
        with urlopen("http://127.0.0.1:%d%s" % (self.server.server_port, path), timeout=3) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")

    def test_real_dashboard_root_and_api_state_keep_multi_agent_contract(self):
        dashboard.SCAN_STATE.update({
            "last_scan_time": "2026-09-10 00:00:00",
            "scan_interval": 30,
            "scan_count": 1,
            "autonomous_analysis": False,
            "fingerprints_count": 2,
            "active_investigations": {},
            "observation_adapter": {"status": "not_configured", "source": None},
            "agents": [
                {"pid": 101, "instance_id": "101:1.0", "adapter": {}},
                {"pid": 202, "instance_id": "202:2.0", "adapter": {}},
            ],
        })
        root_status, root_type, root_body = self._get("/")
        self.assertEqual(root_status, 200)
        self.assertIn("text/html", root_type)
        self.assertIn("实时 Agent 监控看板", root_body)

        with patch.object(dashboard, "AUTONOMOUS_ANALYSIS_ENABLED", False):
            state_status, state_type, state_body = self._get("/api/state")
        self.assertEqual(state_status, 200)
        self.assertIn("application/json", state_type)
        state = json.loads(state_body)
        self.assertEqual([agent["pid"] for agent in state["agents"]], [101, 202])
        self.assertEqual(state["fingerprints_count"], 2)
        self.assertEqual(state["agents"][0]["adapter"]["investigation"]["status"], "disabled")

    def test_observation_adapter_reads_local_health_and_events_but_binds_one_instance(self):
        obs_server = ThreadingHTTPServer(("127.0.0.1", 0), _ObservationFixture)
        obs_worker = threading.Thread(target=obs_server.serve_forever, daemon=True)
        obs_worker.start()
        base = "http://127.0.0.1:%d" % obs_server.server_port
        try:
            with patch.object(dashboard, "OBSERVE_URL", base):
                snapshot = dashboard.read_observation_snapshot()
                self.assertEqual(snapshot["status"], "connected")
                self.assertEqual(snapshot["health"]["status"], "stale")
                self.assertEqual(snapshot["events"]["valid"], 3)
                bound = dashboard.observation_for_instance(snapshot, 5297, 1789006943.640438)
                self.assertEqual(bound["status"], "observed")
                self.assertEqual(bound["health_status"], "stale")
                self.assertEqual(bound["blocking"]["status"], "unsupported")
                parent = dashboard.observation_for_instance(snapshot, 4970, 1789006940.319704)
                self.assertEqual(parent["status"], "not_bound")
                self.assertIn("5297", parent["reason"])
                reused = dashboard.observation_for_instance(snapshot, 5297, 9999.0)
                self.assertEqual(reused["status"], "not_bound")
        finally:
            obs_server.shutdown()
            obs_server.server_close()
            obs_worker.join(timeout=3)

    def test_onboarding_api_exposes_plan_and_requires_process_authorization(self):
        pid = os.getpid()
        create_time = __import__("psutil").Process(pid).create_time()
        plan = {
            "plan_version": 1,
            "instance_id": "%s:%s" % (pid, create_time),
            "pid": pid,
            "create_time": create_time,
            "status": "plan_pending_authorization",
            "action": "install_reused_recipe",
            "match_status": "exact",
            "workspace": tempfile.gettempdir(),
        }
        dashboard.SCAN_STATE["agents"] = [{
            "pid": pid,
            "instance_id": "%s:%s" % (pid, create_time),
            "adapter": {"onboarding": {"plan": plan}},
        }]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            plan["workspace"] = str(workspace)
            with patch.dict(os.environ, {
                    "ASG_EXPERIENCE_DB": str(Path(tmp) / "experience.json"),
                    "ASG_ONBOARDING_AUTHORIZED": "0",
                    "ASG_ONBOARDING_AUTO_INSTALL": "0",
                    "ASG_ONBOARDING_SCOPE": "",
            }, clear=False):
                status, content_type, body = self._get("/api/onboarding?pid=%d" % pid)
                self.assertEqual(status, 200)
                self.assertIn("application/json", content_type)
                self.assertEqual(json.loads(body)["onboarding"]["plan"]["status"],
                                 "plan_pending_authorization")

                request = Request("http://127.0.0.1:%d/api/onboarding/execute?pid=%d" %
                                  (self.server.server_port, pid), method="POST")
                with urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["install"]["status"], "pending_authorization")
                self.assertFalse((workspace / ".opencode" / "plugins" /
                                  "asg-observe.js").exists())

    def test_isolated_install_uninstall_revokes_observer_and_dashboard_state(self):
        """真实 ghost CLI + 观测 HTTP 子进程 + 原看板 HTTP，完整验证撤销语义。"""
        pid = os.getpid()
        create_time = __import__("psutil").Process(pid).create_time()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            installed = json.loads(run_ghost("--install", "--workspace", str(ws),
                                              "--nonce", "ISOLATED-REVOCATION").stdout)
            observer = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "runtime" / "opencode" / "server.py"),
                 "--workspace", str(ws), "--pid", str(pid),
                 "--create-time", str(create_time)],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = observer.stdout.readline()
                self.assertIn('"port"', line)
                observer_port = json.loads(line)["port"]
                observer_url = "http://127.0.0.1:%d" % observer_port
                with patch.object(dashboard, "OBSERVE_URL", observer_url), \
                        patch.object(dashboard, "AUTONOMOUS_ANALYSIS_ENABLED", False):
                    before = dashboard.read_observation_snapshot()
                    self.assertEqual(before["status"], "connected")
                    before_evidence = dashboard.observation_for_instance(before, pid, create_time)
                    self.assertEqual(before_evidence["status"], "bound_no_events")
                    dashboard.SCAN_STATE["agents"] = [{
                        "pid": pid, "instance_id": "%s:%s" % (pid, create_time),
                        "adapter": {"observation_evidence": before_evidence},
                    }]
                    _, _, body = self._get("/api/state")
                    self.assertEqual(json.loads(body)["agents"][0]["adapter"]
                                     ["observation_evidence"]["status"], "bound_no_events")

                    uninstalled = run_ghost("--uninstall", "--workspace", str(ws),
                                            "--name", "asg-observe.js")
                    self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)

                    with self.assertRaises(HTTPError) as health_error:
                        urlopen(observer_url + "/health", timeout=3)
                    revoked_health = json.load(health_error.exception)
                    self.assertEqual(revoked_health["status"], "revoked")
                    self.assertFalse(revoked_health["healthy"])
                    self.assertEqual(revoked_health["instance_pid"], pid)
                    self.assertAlmostEqual(revoked_health["instance_create_time"], create_time)

                    with self.assertRaises(HTTPError) as events_error:
                        urlopen(observer_url + "/events", timeout=3)
                    self.assertEqual(json.load(events_error.exception)["status"], "revoked")

                    after = dashboard.read_observation_snapshot()
                    self.assertEqual(after["status"], "revoked")
                    self.assertEqual(after["instance_pid"], pid)
                    self.assertAlmostEqual(after["instance_create_time"], create_time)
                    after_evidence = dashboard.observation_for_instance(after, pid, create_time)
                    self.assertEqual(after_evidence["status"], "revoked")
                    self.assertFalse(after_evidence["loaded_observed"])
                    dashboard.SCAN_STATE["agents"][0]["adapter"]["observation_evidence"] = after_evidence
                    _, _, body = self._get("/api/state")
                    state = json.loads(body)
                    self.assertEqual(state["agents"][0]["adapter"]
                                     ["observation_evidence"]["status"], "revoked")
                    self.assertEqual(state["agents"][0]["adapter"]
                                     ["observation_evidence"]["instance_id"],
                                     "%s:%s" % (pid, create_time))
            finally:
                observer.terminate()
                try:
                    observer.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    observer.kill()
                    observer.wait(timeout=5)

    def test_observe_adapter_rejects_redirect_without_following_it(self):
        obs_server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectFixture)
        worker = threading.Thread(target=obs_server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(dashboard, "OBSERVE_URL",
                              "http://127.0.0.1:%d" % obs_server.server_port):
                snapshot = dashboard.read_observation_snapshot()
            self.assertEqual(snapshot["status"], "unavailable")
            self.assertIn("JSON", snapshot["message"])
        finally:
            obs_server.shutdown()
            obs_server.server_close()
            worker.join(timeout=3)

    def test_malformed_event_counts_degrade_without_aborting_scan(self):
        obs_server = ThreadingHTTPServer(("127.0.0.1", 0), _MalformedEventsFixture)
        worker = threading.Thread(target=obs_server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(dashboard, "OBSERVE_URL",
                              "http://127.0.0.1:%d" % obs_server.server_port):
                snapshot = dashboard.read_observation_snapshot()
            self.assertEqual(snapshot["status"], "degraded")
            evidence = dashboard.observation_for_instance(snapshot, 5297, 1789006943.640438)
            self.assertEqual(evidence["status"], "degraded")
        finally:
            obs_server.shutdown()
            obs_server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
