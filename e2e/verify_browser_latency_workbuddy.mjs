#!/usr/bin/env node
import {readFileSync, writeFileSync} from 'node:fs';
import {dirname} from 'node:path';
import {mkdirSync} from 'node:fs';

const dashboardPort = Number(process.env.ASG_DASHBOARD_CDP_PORT || 9333);
const targetPort = Number(process.env.ASG_WORKBUDDY_CDP_PORT || 9223);
const targetPid = Number(process.env.ASG_WORKBUDDY_PID || 0);
const eventLog = process.env.ASG_WORKBUDDY_EVENT_LOG || `${process.env.HOME}/.workbuddy-ai/asg-hook/events.jsonl`;
const sampleCount = Number(process.env.ASG_LATENCY_SAMPLES || 5);
const reportPath = process.env.ASG_LATENCY_REPORT || 'artifacts/acceptance/browser-page-latency.json';
if (!targetPid) throw new Error('ASG_WORKBUDDY_PID is required');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function cdp(port) {
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find(item => item.type === 'page');
  if (!page) throw new Error(`no debuggable page on port ${port}`);
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  };
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  const call = (method, params = {}) => {
    const id = ++seq;
    return new Promise(resolve => {
      pending.set(id, resolve);
      ws.send(JSON.stringify({id, method, params}));
    });
  };
  const evaluate = async expression => {
    const response = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (response.error || response.result?.exceptionDetails) throw new Error(JSON.stringify(response));
    return response.result?.result?.value;
  };
  return {evaluate, close: () => ws.close()};
}

function rows() {
  const text = readFileSync(eventLog, 'utf8');
  return text.split(/\r?\n/).filter(Boolean).flatMap(line => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}

function contentObject(row) {
  if (row && row.content && typeof row.content === 'object') return row.content;
  return {};
}

async function waitFor(find, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await find();
    if (value) return value;
    await sleep(50);
  }
  throw new Error(`timeout waiting for ${label}`);
}

const dashboard = await cdp(dashboardPort);
const target = await cdp(targetPort);
try {
  await dashboard.evaluate(`(async()=>{await openHookData(${targetPid});return document.getElementById('hook-data-drawer').classList.contains('active')})()`);
  const samples = [];
  for (let index = 0; index < sampleCount; index += 1) {
    await waitFor(
      () => target.evaluate(`!!Array.from(document.querySelectorAll('button[aria-label="Send"]')).at(-1)`),
      120000,
      'WorkBuddy send control',
    );
    const marker = `ASG-PAGE-LATENCY-${Date.now()}-${index + 1}`;
    const prompt = `Do not call tools. Reply with exactly this text: ${marker}`;
    const sendResult = await target.evaluate(`(async()=>{const b=Array.from(document.querySelectorAll('button[aria-label="Send"]')).at(-1);if(!b)return {ok:false,reason:'no-send-button'};const fk=Object.keys(b).find(k=>k.startsWith('__reactFiber'));let f=b[fk];while(f&&f.type?.name!=='InputBoxStoreProvider')f=f.return;if(!f)return {ok:false,reason:'no-input-store'};const s=f.memoizedProps.store;s.api.setText(${JSON.stringify(prompt)},{focus:true});const value=await s.api.send();return {ok:true,value:value??null}})()`);
    if (!sendResult?.ok) throw new Error(`WorkBuddy send failed: ${JSON.stringify(sendResult)}`);
    const input = await waitFor(() => {
      const match = rows().find(row => row.event === 'user.input' && row.agent_pid === targetPid && contentObject(row).prompt === prompt);
      return Promise.resolve(match || null);
    }, 30000, `producer event ${marker}`);
    const eventMs = Date.parse(input.timestamp);
    if (!Number.isFinite(eventMs)) throw new Error(`invalid producer timestamp: ${input.timestamp}`);
    const visiblePrefix = new Date(eventMs).toISOString().slice(0, -1);
    const visibleAtMs = await waitFor(async () => {
      const visible = await dashboard.evaluate(`document.getElementById('hook-data-drawer-body').innerText.includes(${JSON.stringify(visiblePrefix)})`);
      return visible ? Date.now() : null;
    }, 15000, `dashboard visibility ${marker}`);
    samples.push({
      marker,
      event_id: input.event_id || null,
      producer_timestamp: input.timestamp,
      visible_at: new Date(visibleAtMs).toISOString(),
      latency_ms: Math.max(0, visibleAtMs - eventMs),
    });
    await waitFor(() => {
      const match = rows().find(row => row.event === 'assistant.output' && row.agent_pid === targetPid && String(contentObject(row).text || '').includes(marker));
      return Promise.resolve(match || null);
    }, 120000, `assistant completion ${marker}`);
  }
  const ordered = samples.map(item => item.latency_ms).sort((a, b) => a - b);
  const percentile = q => ordered[Math.max(0, Math.ceil(q * ordered.length) - 1)];
  const report = {
    schema: 'asg.browser-page-latency.v1',
    generated_at: new Date().toISOString(),
    target: {name: 'WorkBuddy AI', pid: targetPid},
    measurement: 'producer user.input timestamp to matching event timestamp rendered in the open Hook live-data drawer',
    sample_count: samples.length,
    samples,
    metrics: {min_ms: ordered[0], median_ms: percentile(0.5), p95_ms: percentile(0.95), max_ms: ordered.at(-1)},
    gates: {sample_count_at_least_5: samples.length >= 5, p95_le_5000ms: percentile(0.95) <= 5000},
  };
  report.passed = Object.values(report.gates).every(Boolean);
  mkdirSync(dirname(reportPath), {recursive: true});
  writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
} finally {
  dashboard.close();
  target.close();
}
