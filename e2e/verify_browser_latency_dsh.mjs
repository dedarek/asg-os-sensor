#!/usr/bin/env node
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {dirname} from 'node:path';
import {spawn} from 'node:child_process';

const dashboardPort = Number(process.env.ASG_DASHBOARD_CDP_PORT || 9333);
const targetPort = Number(process.env.ASG_DSH_CDP_PORT || 9334);
const targetPid = Number(process.env.ASG_DSH_PID || 0);
const eventLog = process.env.ASG_DSH_EVENT_LOG || `${process.env.HOME}/.dsh/profiles/web/asg-runtime-observer/events.jsonl`;
const sampleCount = Number(process.env.ASG_LATENCY_SAMPLES || 5);
const reportPath = process.env.ASG_LATENCY_REPORT || 'artifacts/acceptance/browser-page-latency.json';
if (!targetPid) throw new Error('ASG_DSH_PID is required');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function ensureDashboardBrowser() {
  try {
    await fetch(`http://127.0.0.1:${dashboardPort}/json/version`);
    return null;
  } catch {
    const child = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      `--remote-debugging-port=${dashboardPort}`, '--user-data-dir=/tmp/asg-latency-chrome-e2e',
      'http://127.0.0.1:8081/',
    ], {stdio: 'ignore'});
    await waitFor(async () => {
      try { return (await fetch(`http://127.0.0.1:${dashboardPort}/json/version`)).ok; } catch { return false; }
    }, 15000, 'dashboard browser');
    return child;
  }
}

async function ensureTargetBrowser() {
  try {
    await fetch(`http://127.0.0.1:${targetPort}/json/version`);
    return null;
  } catch {
    const child = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      `--remote-debugging-port=${targetPort}`, '--user-data-dir=/tmp/asg-dsh-chrome-e2e', 'about:blank',
    ], {stdio: 'ignore'});
    await waitFor(async () => {
      try { return (await fetch(`http://127.0.0.1:${targetPort}/json/version`)).ok; } catch { return false; }
    }, 15000, 'DSH browser');
    return child;
  }
}

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
  return {call, evaluate, close: () => ws.close()};
}

function rows() {
  return readFileSync(eventLog, 'utf8').split(/\r?\n/).filter(Boolean).flatMap(line => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}

function decodedContent(row) {
  if (!row) return {};
  if (row.content && typeof row.content === 'object') return row.content;
  if (typeof row.content === 'string') {
    try { return JSON.parse(row.content); } catch { return {}; }
  }
  return {};
}

function containsMarker(row, marker) {
  return JSON.stringify(decodedContent(row)).includes(marker);
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

const dashboardBrowser = await ensureDashboardBrowser();
const targetBrowser = await ensureTargetBrowser();
const dashboard = await cdp(dashboardPort);
const target = await cdp(targetPort);
try {
  if (targetBrowser) {
    const authUrl = readFileSync('/tmp/asg-dsh-auth-url', 'utf8').trim();
    await target.call('Page.navigate', {url: authUrl});
    await waitFor(() => target.evaluate(`document.readyState==='complete' && document.title.includes('DeepSeek Harness')`),
                  15000, 'authenticated DSH page');
  }
  await dashboard.evaluate(`(async()=>{await openHookData(${targetPid});return document.getElementById('hook-data-drawer').classList.contains('active')})()`);
  const samples = [];
  for (let index = 0; index < sampleCount; index += 1) {
    await waitFor(
      () => target.evaluate(`(()=>{const b=document.querySelector('button[aria-label="发送消息"]');return !!b && !b.disabled})()`),
      120000,
      'DSH send control',
    );
    const marker = `ASG-PAGE-LATENCY-${Date.now()}-${index + 1}`;
    const prompt = `Do not call tools. Reply with exactly: ${marker}`;
    await target.evaluate(`(()=>{const e=document.querySelector('[contenteditable=true][role=textbox]');if(!e)return false;e.focus();return true})()`);
    await target.call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 4});
    await target.call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 4});
    await target.call('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Backspace', code: 'Backspace'});
    await target.call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Backspace', code: 'Backspace'});
    await target.call('Input.insertText', {text: prompt});
    const clicked = await target.evaluate(`(()=>{const b=document.querySelector('button[aria-label="发送消息"]');if(!b||b.disabled)return false;b.click();return true})()`);
    if (!clicked) throw new Error('DSH send failed');
    const input = await waitFor(() => Promise.resolve(rows().find(row =>
      row.event === 'user.input' && row.pid === targetPid && containsMarker(row, marker)) || null), 30000, `producer event ${marker}`);
    const eventMs = Date.parse(input.timestamp);
    if (!Number.isFinite(eventMs)) throw new Error(`invalid producer timestamp: ${input.timestamp}`);
    const visiblePrefix = new Date(eventMs).toISOString().slice(0, -1);
    const visibleAtMs = await waitFor(async () => {
      const visible = await dashboard.evaluate(`document.getElementById('hook-data-drawer-body').innerText.includes(${JSON.stringify(visiblePrefix)})`);
      return visible ? Date.now() : null;
    }, 15000, `dashboard visibility ${marker}`);
    samples.push({marker, event_id: input.event_id || null, producer_timestamp: input.timestamp,
                  visible_at: new Date(visibleAtMs).toISOString(), latency_ms: Math.max(0, visibleAtMs - eventMs)});
    await waitFor(() => Promise.resolve(rows().find(row =>
      row.event === 'assistant.output' && row.pid === targetPid && containsMarker(row, marker)) || null), 120000, `assistant completion ${marker}`);
  }
  const ordered = samples.map(item => item.latency_ms).sort((a, b) => a - b);
  const percentile = q => ordered[Math.max(0, Math.ceil(q * ordered.length) - 1)];
  const report = {
    schema: 'asg.browser-page-latency.v1', generated_at: new Date().toISOString(),
    target: {name: '@deepseek-ai/dsh', pid: targetPid},
    measurement: 'producer user.input timestamp to matching event timestamp rendered in the open Hook live-data drawer',
    sample_count: samples.length, samples,
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
  if (dashboardBrowser) dashboardBrowser.kill('SIGTERM');
  if (targetBrowser) targetBrowser.kill('SIGTERM');
}
