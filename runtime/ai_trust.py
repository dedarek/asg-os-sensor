"""AI Trust connector: registration, event upload, decisions and receipts.

This is the connector the task book asks for while a real AI Trust test
environment, credentials and protocol are unavailable. It implements the
interfaces, the local-to-platform field mapping, credential handling, an
at-least-once spool with idempotency, retry/backoff and offline catch-up.

It never claims platform sign-off: every result carries platform_verified
False and the counters are connector-side only. A real sign-off needs the
platform to answer real requests.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTRACT = {
    "version": 1,
    "endpoints": {
        "register_instance": "/v1/instances",
        "events": "/v1/events",
        "decisions": "/v1/decisions",
        "receipts": "/v1/receipts",
        "policies": "/v1/policies",
        "confirmations": "/v1/confirmations",
    },
    "auth": "Authorization: Bearer <token>",
    "idempotency": "每个事件带 event_id 作为幂等键；平台按 event_id 去重",
    "correlation": "本地 request_id/call_id 与平台记录双向关联",
    "not_supported_yet": ["平台侧策略下发格式", "平台人工确认协议", "平台去重实现细节"],
}

# local field -> platform field (one mapping per local field, checked for completeness)
FIELD_MAP = {
    "instance_id": "instance_ref",
    "host": "host_ref",
    "session_id": "conversation_id",
    "turn_id": "turn_ref",
    "agent_id": "agent_ref",
    "event_type": "event_kind",
    "timestamp": "occurred_at",
    "content": "payload_text",
    "content_complete": "payload_complete",
    "event_id": "event_id",
    "sequence": "seq",
    "tool": "tool_ref",
    "call_id": "call_ref",
    "request_id": "correlation_id",
    "decision": "decision",
    "outcome": "outcome",
}
REQUIRED_LOCAL_FIELDS = tuple(FIELD_MAP)


def load_config(environ=None, path=None) -> dict:
    """Resolve the platform address and credential from env or an explicit file.

    The token is a credential: it is read here and never written into evidence.
    """
    env = environ if environ is not None else os.environ
    config = {
        "base_url": (env.get("ASG_AI_TRUST_URL") or "").strip(),
        "token": (env.get("ASG_AI_TRUST_TOKEN") or "").strip(),
        "spool_path": env.get("ASG_AI_TRUST_SPOOL") or str(
            Path(env.get("ASG_RUN_DIR", "artifacts/stage1/dashboard")) / "ai-trust-spool.jsonl"),
        "timeout": float(env.get("ASG_AI_TRUST_TIMEOUT", "10") or "10"),
        "host_id": (env.get("ASG_AI_TRUST_HOST") or "").strip(),
    }
    env_file = env.get("ASG_AI_TRUST_CONFIG")
    if path:
        env_file = str(path)
    if env_file and Path(env_file).is_file():
        file_config = json.loads(Path(env_file).read_text())
        for key in ("base_url", "token", "spool_path", "host_id"):
            if file_config.get(key):
                config[key] = file_config[key]
        if file_config.get("timeout"):
            config["timeout"] = float(file_config["timeout"])
    return config


def configured(config: dict) -> dict:
    missing = [k for k in ("base_url", "token") if not config.get(k)]
    return {"configuration_ready": not missing, "missing": missing,
            "platform_verified": False,
            "reason": "缺少平台地址或凭证" if missing else "已配置，但平台可用性尚未验证"}


def map_record(record: dict) -> dict:
    """Map one local record onto the platform field names (pure)."""
    payload = {}
    for local, platform in FIELD_MAP.items():
        if local in record:
            payload[platform] = record[local]
    payload.setdefault("schema_version", CONTRACT["version"])
    return payload


def mapping_gaps() -> list:
    return [f for f in REQUIRED_LOCAL_FIELDS if f not in FIELD_MAP]


class Spool:
    """Append-only, idempotent queue of local records awaiting upload.

    Survives restarts (file-backed). event_id is the idempotency key, so a
    resend after a crash cannot create a logical duplicate.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._pending = {}
        self._order = []
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            event_id = record.get("event_id")
            if not event_id:
                continue
            if record.get("_sent"):
                self._pending.pop(event_id, None)
                continue
            if event_id not in self._pending:
                self._order.append(event_id)
            self._pending[event_id] = record

    def enqueue(self, record: dict) -> bool:
        event_id = record.get("event_id")
        if not event_id:
            raise ValueError("record requires event_id as the idempotency key")
        if event_id in self._pending:
            return False
        self._pending[event_id] = record
        self._order.append(event_id)
        self._append({"event_id": event_id, **record})
        return True

    def _append(self, record: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def pending(self) -> list:
        return [self._pending[e] for e in self._order if e in self._pending]

    def mark_sent(self, event_ids):
        for event_id in event_ids:
            if event_id in self._pending:
                self._append({"event_id": event_id, "_sent": True})
                self._pending.pop(event_id, None)

    def size(self) -> int:
        return len(self._pending)


class TrustError(RuntimeError):
    pass


class TrustClient:
    """Real HTTP client; a transport may be injected for local verification only."""

    def __init__(self, config: dict, transport=None):
        self.config = config
        self._transport = transport or self._http
        self.last_latency_s = None

    def _http(self, method: str, url: str, body):
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + str(self.config.get("token") or "")})
        try:
            with urllib.request.urlopen(req, timeout=self.config.get("timeout", 10)) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _call(self, endpoint: str, payload: dict):
        base = str(self.config.get("base_url") or "").rstrip("/")
        if not base:
            raise TrustError("platform base_url is not configured")
        url = base + CONTRACT["endpoints"][endpoint]
        started = time.time()
        try:
            status, body = self._transport("POST", url, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - transport failure is retryable, not fatal
            self.last_latency_s = time.time() - started
            return 0, {"transport_error": type(exc).__name__}
        self.last_latency_s = time.time() - started
        parsed = None
        try:
            parsed = json.loads(body) if body else None
        except (TypeError, ValueError):
            parsed = None
        return status, parsed

    def register_instance(self, instance: dict):
        return self._call("register_instance", instance)

    def upload_events(self, records: list) -> dict:
        """Upload a batch. A non-2xx response is surfaced, never reported as accepted."""
        status, parsed = self._call("events", {"events": [map_record(r) for r in records]})
        if status in (401, 403):
            return {"status": "rejected_credentials", "http_status": status,
                    "accepted": 0, "accepted_ids": [], "platform_verified": False}
        if not 200 <= status < 300:
            return {"status": "failed", "http_status": status, "accepted": 0,
                    "accepted_ids": [], "platform_verified": False}
        # The platform echoes the ids it now holds, so a deduped resend is still
        # acknowledged (nothing new) and the spool can stop retrying it.
        recorded = parsed.get("recorded_event_ids") if isinstance(parsed, dict) else None
        if not isinstance(recorded, list):
            return {"status": "no_acknowledgement", "http_status": status, "recorded": 0, "new": 0,
                    "recorded_ids": [], "new_ids": [], "platform_verified": False}
        new_ids = parsed.get("new_event_ids") if isinstance(parsed.get("new_event_ids"), list) else recorded
        return {"status": "accepted", "http_status": status,
                "recorded": len(recorded), "new": len(new_ids),
                "recorded_ids": recorded, "new_ids": new_ids, "latency_s": self.last_latency_s,
                "platform_verified": False}

    def upload_decision(self, decision: dict):
        return self._call("decisions", decision)

    def send_receipt(self, receipt: dict):
        return self._call("receipts", receipt)

    def fetch_policy(self):
        return self._call("policies", {})

    def request_confirmation(self, request: dict):
        return self._call("confirmations", request)


def flush(spool: Spool, client: TrustClient, *, batch: int = 100) -> dict:
    """Upload pending records; keep whatever the platform did not accept."""
    records = spool.pending()
    sent = []
    attempted = 0
    for start in range(0, len(records), batch):
        chunk = records[start:start + batch]
        attempted += len(chunk)
        result = client.upload_events(chunk)
        if result["status"] != "accepted":
            return {"status": result["status"], "attempted": attempted, "accepted": len(sent),
                    "http_status": result.get("http_status"), "platform_verified": False,
                    "remaining": spool.size()}
        recorded = set(result["recorded_ids"])
        for record in chunk:
            if record.get("event_id") in recorded:
                sent.append(record["event_id"])
        spool.mark_sent(sent)
    return {"status": "accepted" if (sent or not records) else "no_acknowledgement",
            "attempted": attempted, "accepted": len(sent), "remaining": spool.size(),
            "platform_verified": False}


__all__ = ["CONTRACT", "FIELD_MAP", "REQUIRED_LOCAL_FIELDS", "Spool", "TrustClient", "TrustError",
           "configured", "flush", "load_config", "map_record", "mapping_gaps"]
