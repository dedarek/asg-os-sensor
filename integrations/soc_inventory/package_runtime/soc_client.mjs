// Installed Hook transport. It runs with the Agent's own Node runtime and
// talks to SOC directly; no ASG service, release path, or npm dependency.
import { createHash, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'

const config = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const action = process.argv[3] || 'decision'
const input = JSON.parse(await new Promise((resolve, reject) => {
  let value = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', (chunk) => { value += chunk })
  process.stdin.on('end', () => resolve(value || '{}'))
  process.stdin.on('error', reject)
}))

function allowedBase () {
  const base = new URL(config.backend_url)
  if (base.protocol !== 'https:' && !(base.protocol === 'http:' && ['127.0.0.1', 'localhost', '::1'].includes(base.hostname))) {
    throw new Error('remote SOC requires HTTPS')
  }
  return base.href.replace(/\/$/, '')
}

async function request (path, body) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), Number(config.timeout_seconds || 30) * 1000)
  try {
    const response = await fetch(allowedBase() + path, {
      method: 'POST', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + readFileSync(config.token_file, 'utf8').trim() },
      body: JSON.stringify(body),
    })
    if (!response.ok) throw new Error('HTTP ' + String(response.status))
    return await response.json()
  } finally { clearTimeout(timer) }
}

async function exchange (kind, data) {
  if (kind === 'event') {
    const eventType = String(data.event || data.event_type || '')
    if (!eventType || eventType.length > 200) throw new Error('invalid event type')
    const instanceId = String(config.instance_id || config.agent_id)
    const eventId = createHash('sha256').update(JSON.stringify([instanceId, data])).digest('hex')
    return await request('/api/asg/events', { instance_id: instanceId, events: [{
      event_id: eventId, instance_id: instanceId, event_type: eventType,
      timestamp: data.timestamp, channel: 'direct', payload: data,
    }] })
  }
  if (kind === 'ack') {
    const event = {
      event: 'execution.ack', request_id: String(data.request_id || ''),
      decision: String(data.decision || ''), tool: data.tool, call_id: data.call_id,
      outcome: data.outcome, timestamp: data.timestamp || new Date().toISOString(),
    }
    if (typeof data.applied === 'boolean') event.applied = data.applied
    if (typeof data.executed === 'boolean') event.executed = data.executed
    const result = await exchange('event', event)
    return { ...result, scope: result.accepted ? 'execution_ack_reported' : 'execution_ack_queued', enforcement_verified: false }
  }
  if (kind !== 'decision') throw new Error('unsupported Hook action')
  const payload = {
    platform: config.platform, agent_id: config.agent_id, agent_name: config.agent_name || '',
    layer: 'before_tool_call', session_id: String(data.session_id || data.pid || 'default'),
    timestamp: new Date().toISOString(), query: JSON.stringify(data.input),
    tool: { name: data.tool, call_id: data.call_id, args: data.input },
    extra: { protocol_version: '1.0', pid: data.pid }, trace: [],
  }
  const raw = await request('/api/analyze/before-tool-call', payload)
  const nested = raw.ret_data || {}
  const value = raw.action ?? nested.action
  const allow = [0, '0', 'allow', 'audit', 'inject'].includes(value)
  const deny = [3, 8, 9, '3', '8', '9', 'block', 'confirm'].includes(value)
  if (!allow && !deny) throw new Error('SOC returned no recognized decision')
  return { request_id: raw.request_id || randomUUID(), decision: allow ? 'allow' : 'deny',
    reason: raw.message || raw.ret_msg || 'SOC policy', enforcement_verified: false }
}

try {
  process.stdout.write(JSON.stringify(await exchange(action, input)))
} catch (error) {
  process.stdout.write(JSON.stringify({ decision: 'deny', reason: 'soc_unavailable', error: error?.name || 'Error', enforcement_verified: false }))
  process.exitCode = 1
}
