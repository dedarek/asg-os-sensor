// Real bundled Codex protocol client. Does not implement or invoke a Hook.
import fs from 'node:fs';
import path from 'node:path';
const dir = path.resolve(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const meta = JSON.parse(fs.readFileSync(path.join(dir, 'target.json'), 'utf8'));
const ws = new WebSocket(`ws://127.0.0.1:${meta.port}`);
const pending = new Map();
const events = [];
let seq = 0;
let finish;
const completed = new Promise(resolve => { finish = resolve; });
ws.addEventListener('message', event => {
  const value = JSON.parse(event.data);
  if (value.id != null && pending.has(value.id)) {
    const {resolve, reject, timer} = pending.get(value.id);
    pending.delete(value.id); clearTimeout(timer);
    value.error ? reject(new Error(JSON.stringify(value.error))) : resolve(value.result);
  } else {
    events.push(value);
    if (value.method === 'turn/completed') finish(value.params);
  }
});
function rpc(method, params) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timeout: ${method}`)); }, 60000);
    pending.set(id, {resolve, reject, timer});
    ws.send(JSON.stringify({id, method, params}));
  });
}
const deadline = setTimeout(() => { console.error('Real Agent turn timed out'); process.exit(1); }, 300000);
try {
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, {once: true}); ws.addEventListener('error', reject, {once: true}); });
  await rpc('initialize', {clientInfo: {name: 'asg-acceptance', version: '1.0'}, capabilities: {experimentalApi: true}});
  ws.send(JSON.stringify({method: 'initialized', params: {}}));
  const thread = await rpc('thread/start', {cwd: path.join(dir, 'workspace'),
    approvalPolicy: 'never', sandbox: 'workspace-write',
    model: 'command-code/deepseek-deepseek-v4.1-flash'});
  await rpc('turn/start', {threadId: thread.thread.id, input: [{type: 'text', text: input.prompt}]});
  const result = await completed;
  fs.writeFileSync(path.join(dir, input.output || 'turn.json'), JSON.stringify({target: meta, thread: thread.thread.id, result, events}, null, 2));
  console.log(JSON.stringify({thread: thread.thread.id, status: result.turn?.status, events: events.length}));
} finally { clearTimeout(deadline); ws.close(); }
