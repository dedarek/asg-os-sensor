"""OTLP/HTTP JSON or protobuf logs/traces, bound to registered live instances.

Telemetry never grants execution control. Unknown semantic fields remain raw.
"""
import hmac
import json
import time
from pathlib import Path
import psutil
from runtime.file_lock import _FileLock
from runtime.observation_registry import Registry
from runtime.learned_install import _atomic


def value(v):
    if not isinstance(v, dict): return v
    for key in ('stringValue', 'intValue', 'doubleValue', 'boolValue', 'bytesValue'):
        if key in v: return v[key]
    if 'arrayValue' in v: return [value(i) for i in v['arrayValue'].get('values', [])]
    if 'kvlistValue' in v: return attrs(v['kvlistValue'].get('values', []))
    return None


def attrs(items): return {x['key']: value(x.get('value')) for x in items if isinstance(x, dict) and 'key' in x}


def decode(raw, signal, content_type):
    if content_type.split(';')[0] == 'application/x-protobuf':
        from google.protobuf.json_format import MessageToDict
        if signal == 'logs':
            from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
            msg = ExportLogsServiceRequest()
        else:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
            msg = ExportTraceServiceRequest()
        msg.ParseFromString(raw)
        return MessageToDict(msg)
    if content_type.split(';')[0] != 'application/json': raise ValueError('Use OTLP JSON or protobuf')
    payload = json.loads(raw)
    if not isinstance(payload, dict): raise ValueError('OTLP object required')
    return payload


def records(payload, signal):
    resource_key, scope_key, rows_key = ('resourceLogs', 'scopeLogs', 'logRecords') if signal == 'logs' else ('resourceSpans', 'scopeSpans', 'spans')
    for resource in payload.get(resource_key, []):
        resource_attrs = attrs(resource.get('resource', {}).get('attributes', []))
        for scope in resource.get(scope_key, []):
            for row in scope.get(rows_key, []):
                attributes = {**resource_attrs, **attrs(row.get('attributes', []))}
                timestamp = int(row.get('timeUnixNano') or row.get('endTimeUnixNano') or row.get('observedTimeUnixNano') or 0) / 1e9
                base = {'event': row.get('eventName') or 'otel.' + ('log' if signal == 'logs' else 'span'),
                       'attributes': attributes, 'timestamp': timestamp, 'content': value(row.get('body')) if signal == 'logs' else None,
                       'content_complete': False, 'trace_id': row.get('traceId'), 'span_id': row.get('spanId'),
                       'otel_record': row, 'capture_layer': 'otel', 'control_supported': False}
                yield base
                # GenAI message attributes are content evidence, not loss proof.
                for key, event in (('gen_ai.input.messages', 'model.request'), ('gen_ai.output.messages', 'model.response')):
                    if key not in attributes: continue
                    content = attributes[key]
                    if isinstance(content, str):
                        try: content = json.loads(content)
                        except ValueError: pass
                    yield {**base, 'event': event, 'content': content,
                           'request_id': row.get('spanId'), 'derived_from': key,
                           'session_id': attributes.get('gen_ai.conversation.id'),
                           'content_complete': False}


def ingest(payload, signal, root):
    root = Path(root)
    try: bindings = json.loads((root / 'observations.json').read_text())
    except FileNotFoundError: bindings = {}
    targets = {f"{v['target']['pid']}:{v['target']['create_time']}": v['target'] for v in bindings.values()}
    accepted = rejected = 0
    for row in records(payload, signal):
        attributes = row['attributes']; target = targets.get(attributes.get('asg.instance.id'))
        if target is None:
            matches = [t for t in targets.values() if str(t['pid']) == str(attributes.get('process.pid'))]
            alive = []
            for t in matches:
                try:
                    if abs(psutil.Process(t['pid']).create_time() - t['create_time']) < .001: alive.append(t)
                except psutil.Error: pass
            alive = { (t['pid'], t['create_time']): t for t in alive }
            if len(alive) == 1: target = next(iter(alive.values()))
        if not target or row['timestamp'] < target['create_time'] or row['timestamp'] > time.time() + 60:
            rejected += not bool(row.get('derived_from')); continue
        try:
            if abs(psutil.Process(target['pid']).create_time() - target['create_time']) >= .001:
                rejected += not bool(row.get('derived_from')); continue
        except psutil.Error:
            rejected += not bool(row.get('derived_from')); continue
        folder = root / 'otel' / f"{target['pid']}"; folder.mkdir(parents=True, exist_ok=True)
        log = folder / 'events.jsonl'
        with _FileLock(log):
            with log.open('a', encoding='utf-8') as out: out.write(json.dumps({**row, **target}, ensure_ascii=False) + '\n')
        binding = folder / 'binding.json'
        _atomic(binding, json.dumps({'target': target, 'log_path': str(log),
            'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp'}}).encode())
        Registry(root).register(binding, target, source_name='otel')
        accepted += not bool(row.get('derived_from'))
    return accepted, rejected


def handle(handler):
    from urllib.parse import urlsplit
    path = urlsplit(handler.path).path
    if path not in ('/v1/logs', '/v1/traces'): return False
    from runtime import hook_control
    signal = 'logs' if path.endswith('logs') else 'traces'
    proto = handler.headers.get('Content-Type', '').split(';')[0] == 'application/x-protobuf'
    try:
        if handler.command != 'POST': raise ValueError('POST required')
        expected = 'Bearer ' + hook_control.token_path().read_text().strip()
        if not hmac.compare_digest(handler.headers.get('Authorization', ''), expected): raise PermissionError('OTLP credential required')
        length = int(handler.headers.get('Content-Length', 0))
        if not 0 < length <= 4 * 1024 * 1024: raise ValueError('OTLP payload limit: 4 MiB')
        if handler.headers.get('Content-Encoding', 'identity') != 'identity': raise ValueError('Compressed OTLP is not enabled')
        data = decode(handler.rfile.read(length), signal, handler.headers.get('Content-Type', ''))
        accepted, rejected = ingest(data, signal, hook_control.root())
        result = {}
        if rejected: result = {'partialSuccess': {'rejectedLogRecords' if signal == 'logs' else 'rejectedSpans': str(rejected), 'errorMessage': 'missing or stale target binding/timestamp'}}
        code = 200
    except PermissionError as exc: code, result = 403, {'error': str(exc)}
    except Exception as exc: code, result = 400, {'error': type(exc).__name__ + ': invalid OTLP request'}
    if proto and code == 200:
        from google.protobuf.json_format import ParseDict
        if signal == 'logs':
            from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
            reply = ExportLogsServiceResponse()
        else:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
            reply = ExportTraceServiceResponse()
        body = ParseDict(result, reply).SerializeToString()
    else: body = json.dumps(result).encode()
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/x-protobuf' if proto and code == 200 else 'application/json')
    handler.send_header('Content-Length', str(len(body))); handler.end_headers(); handler.wfile.write(body)
    return True
