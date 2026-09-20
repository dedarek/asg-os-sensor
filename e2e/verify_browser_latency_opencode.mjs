// Page-visibility latency: event produced -> marker visible in the dashboard DOM.
// Drives a real headless Chrome over CDP: sends a canary through the running
// OpenCode demo agent over its own HTTP API, then watches the rendered hook
// drawer text for the marker. Producer time comes from the record's own
// timestamp. No DSH/WorkBuddy dependency: the target is headless on purpose.
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {spawn} from 'node:child_process';

const dashPort = Number(process.env.ASG_DASHBOARD_CDP_PORT || 9333);
const dashBase = process.env.ASG_DASHBOARD_URL || 'http://127.0.0.1:8081';
const meta = JSON.parse(readFileSync('artifacts/autonomous-demo/target.json', 'utf8'));
const gatewayCfg = JSON.parse(readFileSync('artifacts/autonomous-demo/gateway.json', 'utf8'));
const target = 'http://127.0.0.1:' + meta.port;
const instance = process.argv.includes('--instance')
  ? process.argv[process.argv.indexOf('--instance') + 1]
  : meta.pid + ':' + meta.create_time;
const sampleCount = Number(process.env.ASG_LATENCY_SAMPLES || 30);
const reportPath = process.env.ASG_LATENCY_REPORT || 'artifacts/acceptance/browser-page-latency-opencode.json';
const noProxy = {proxy: undefined};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  if (!response.ok) throw new Error(url + ' -> ' + response.status);
  return response.json();
}

async function waitFor(find, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await find();
    if (value) return value;
    await sleep(200);
  }
  throw new Error('timeout waiting for ' + label);
}

async function dashboardBrowser() {
  try { await fetchJson(`http://127.0.0.1:${dashPort}/json/version`); return null; } catch {}
  const child = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${dashPort}`, '--user-data-dir=/tmp/asg-page-latency-chrome', dashBase + '/',
  ], {stdio: 'ignore'});
  await waitFor(async () => {
    try { return (await fetch(`http://127.0.0.1:${dashPort}/json/version`, noProxy)).ok; } catch { return false; }
  }, 20000, 'dashboard browser');
  return child;
}

async function cdp(port) {
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`, noProxy)).json();
  const page = pages.find(item => item.type === 'page' && item.url.includes('8081')) || pages.find(item => item.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
  };
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  const call = (method, params = {}) => new Promise(resolve => {
    const id = ++seq; pending.set(id, resolve); ws.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const response = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (response.result?.exceptionDetails) throw new Error(JSON.stringify(response.result.exceptionDetails).slice(0, 300));
    return response.result?.result?.value;
  };
  return {call, evaluate, close: () => ws.close()};
}

function percentile(values, fraction) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.max(0, Math.round(fraction * ordered.length) - 1))];
}

const browserChild = await dashboardBrowser();
const dash = await cdp(dashPort);
const samples = [];
const failures = [];
try {
  // Open the hook drawer for the demo instance exactly like a user clicking it.
  await waitFor(() => dash.evaluate(`(typeof openHookData === 'function')`), 20000, 'dashboard app ready');
  await dash.evaluate(`(async()=>{await openHookData(${meta.pid});return document.getElementById('hook-data-drawer').classList.contains('active')})()`);
  for (let index = 0; index < sampleCount; index += 1) {
    const marker = 'ASG-PAGE-LAT-' + Date.now() + '-' + index;
    const sessionId = (await fetchJson(target + '/session', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: 'page-latency', permission: [{permission: '*', pattern: '*', action: 'allow'}]})})).id;
    fetch(target + '/session/' + sessionId + '/message', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: {providerID: 'demo', modelID: gatewayCfg.model},
        parts: [{type: 'text', text: 'Do not call tools. Reply with exactly: ' + marker}]})}).catch(() => {});
    try {
      const visibleAt = await waitFor(async () => {
        const data = await fetchJson(dashBase + '/api/hook-data?' + new URLSearchParams({instance_id: instance, limit: '200', max_bytes: '524288'}));
        const record = (data.records || []).find(item => JSON.stringify(item).includes(marker));
        if (!record) return null;
        const shown = await dash.evaluate(`document.getElementById('hook-data-drawer-body').textContent.includes(${JSON.stringify(marker)})`);
        if (!shown) return null;
        // Hook records carry producer timestamps as epoch seconds (numbers) or
        // ISO strings; parse both, and never return NaN (falsy would loop forever).
        const producerMs = typeof record.timestamp === 'number'
          ? record.timestamp * 1000 : Date.parse(record.timestamp);
        if (!Number.isFinite(producerMs)) return null;
        return Math.max(0, Date.now() - producerMs);
      }, 30000, 'marker ' + marker + ' visible in DOM');
      samples.push(visibleAt / 1000);
    } catch (error) {
      failures.push(String(error.message).slice(0, 160));
    }
  }
} finally {
  dash.close();
  if (browserChild) { browserChild.kill(); }
}
const p95 = percentile(samples, 0.95);
const max = samples.length ? Math.max(...samples) : null;
const report = {
  method: 'headless-chrome-cdp-dom', instance, samples: samples.length,
  requested: sampleCount, failures,
  page_latency_s: samples.map(value => Math.round(value * 1000) / 1000),
  page_p95_s: p95 === null ? null : Math.round(p95 * 1000) / 1000,
  page_max_s: max === null ? null : Math.round(max * 1000) / 1000,
  gates: {samples_at_least_30: samples.length >= 30, page_p95_le_5s: p95 !== null && p95 <= 5.0, page_max_le_10s: max !== null && max <= 10.0},
};
report.passed = Object.values(report.gates).every(Boolean);
mkdirSync(reportPath.split('/').slice(0, -1).join('/'), {recursive: true});
writeFileSync(reportPath, JSON.stringify(report, null, 1));
console.log(JSON.stringify({gates: report.gates, passed: report.passed, p95: report.page_p95_s, max: report.page_max_s, samples: samples.length, failures: failures.length}));
process.exit(report.passed ? 0 : 1);
