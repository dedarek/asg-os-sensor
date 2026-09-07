/* 通用 Node 挂接钩子: 只认公共面(https/fetch), 不认任何 harness 内部模块.
 * 用法: NODE_OPTIONS="--require <本文件绝对路径>" ASG_ADAPTER_EVENTS=<事件文件>
 * 抓: 请求方法/主机/路径 + 请求体 + 返回体 + 工具调用(若在 body 内).
 * 脱敏: authorization/cookie/x-api-key/proxy-authorization/set-cookie 头全掩;
 *   body 内 access_token/refresh_token 字段与 sk-类密钥正则掩码.
 * 铁律: 全程 try/catch, 钩子异常永不抛给业务 (fail-open).
 * adapter_source=auto-runtime-node. CJS 格式(NODE_OPTIONS --require 要求).
 */
'use strict';
const fs = require('fs');
const EV = process.env.ASG_ADAPTER_EVENTS || '';
const SRC = 'auto-runtime-node';
function now() { return new Date().toISOString(); }
function emit(ev) {
  if (!EV) return;
  try { fs.appendFileSync(EV, JSON.stringify(ev) + '\n'); } catch (e) { /* 落盘失败不影响业务 */ }
}
function safe(o, n) {
  try {
    const s = typeof o === 'string' ? o : JSON.stringify(o);
    return s.length > (n || 6000) ? s.slice(0, n || 6000) : s;
  } catch (e) { return '[unserializable]'; }
}
const SECRET_RES = [/\"access_token\"\s*:\s*\"[^\"]+\"/g, /\"refresh_token\"\s*:\s*\"[^\"]+\"/g, /sk-[A-Za-z0-9\-_]{8,}/g];
function scrubBody(s) {
  if (typeof s !== 'string') return s;
  let r = s;
  try { SECRET_RES.forEach(function (re) { r = r.replace(re, '"access_token":"[REDACTED]"'); }); } catch (e) {}
  return r;
}
function scrubHeaders(h) {
  const out = {};
  try {
    Object.keys(h || {}).forEach(function (k) {
      if (/^(authorization|cookie|x-api-key|proxy-authorization|set-cookie)$/i.test(k)) out[k] = '[REDACTED]';
      else out[k] = h[k];
    });
  } catch (e) {}
  return out;
}
/* --- https.request / https.get --- */
try {
  const https = require('https');
  ['request', 'get'].forEach(function (m) {
    try {
      const orig = https[m];
      https[m] = function (opts, cb) {
        let chunks = [];
        let req;
        try {
          req = orig.call(this, opts, function (res) {
            const host = (opts && (opts.hostname || opts.host)) || '';
            const path = (opts && opts.path) || '';
            const rchunks = [];
            res.on('data', function (c) { rchunks.push(c); });
            res.on('end', function () {
              try {
                emit({ ts: now(), event_type: 'llm.response', adapter_source: SRC, sdk: 'node-https',
                  host: String(host), path: String(path).slice(0, 200),
                  status: res.statusCode, body: scrubBody(Buffer.concat(rchunks).toString('utf8').slice(0, 6000)) });
              } catch (e) {}
            });
            if (cb) { try { cb(res); } catch (e) {} }
          });
          const owrite = req.write, oend = req.end;
          req.write = function (c) { try { if (c) chunks.push(Buffer.from(c)); } catch (e) {} return owrite.apply(this, arguments); };
          req.end = function (c) {
            try {
              if (c) chunks.push(Buffer.from(c));
              const u = (req.protocol || 'https:') + '//' + (req.host || '') + (req.path || '');
              emit({ ts: now(), event_type: 'llm.request', adapter_source: SRC, sdk: 'node-https',
                method: req.method, url: String(u).slice(0, 300),
                headers: scrubHeaders(req.getHeaders ? req.getHeaders() : {}),
                body: scrubBody(Buffer.concat(chunks).toString('utf8').slice(0, 6000)) });
            } catch (e) {}
            return oend.apply(this, arguments);
          };
        } catch (e) { try { req = orig.apply(this, arguments); } catch (e2) { throw e2; } }
        return req;
      };
    } catch (e) {}
  });
} catch (e) {}
/* --- global fetch (undici) --- */
try {
  if (typeof globalThis.fetch === 'function') {
    const ofetch = globalThis.fetch;
    globalThis.fetch = async function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      try {
        let body = '';
        if (init && init.body) body = typeof init.body === 'string' ? init.body : '[stream]';
        emit({ ts: now(), event_type: 'llm.request', adapter_source: SRC, sdk: 'node-fetch',
          method: (init && init.method) || 'GET', url: String(url).slice(0, 300),
          headers: scrubHeaders((init && init.headers) || {}), body: scrubBody(String(body).slice(0, 6000)) });
      } catch (e) {}
      const res = await ofetch(input, init);
      try {
        const txt = await res.clone().text();
        emit({ ts: now(), event_type: 'llm.response', adapter_source: SRC, sdk: 'node-fetch',
          url: String(url).slice(0, 300), status: res.status,
          body: scrubBody(txt.slice(0, 6000)) });
      } catch (e) {}
      return res;
    };
  }
} catch (e) {}
