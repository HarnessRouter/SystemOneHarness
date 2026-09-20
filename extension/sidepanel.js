// A plain Unified Harness Protocol client: connect, list harnesses, start a task, stream its items,
// cancel, continue. Every number shown comes from the server's own events.
const $ = (id) => document.getElementById(id);
const state = { base: '', key: '', harnesses: [], responseId: null, sessionId: null, controller: null, startedAt: 0 };

function headers(extra = {}) {
  return { 'authorization': `Bearer ${state.key}`, 'content-type': 'application/json', 'UHP-Version': '2026-09-12', ...extra };
}
function setStatus(text, cls) { const s = $('status'); s.textContent = text; s.className = 'pill' + (cls ? ' ' + cls : ''); }

async function connect() {
  state.base = $('base').value.trim().replace(/\/+$/, '');
  state.key = $('key').value;
  $('connectNote').textContent = '';
  try {
    const d = await (await fetch(`${state.base}/v1/uhp`)).json();
    const r = await fetch(`${state.base}/v1/harnesses`, { headers: headers() });
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 120)}`);
    const j = await r.json();
    state.harnesses = j.harnesses || [];
    const sel = $('harness'); sel.innerHTML = '';
    for (const h of state.harnesses) { const o = document.createElement('option'); o.value = h.id; o.textContent = `${h.name} (${h.defaultModel || ''})`; sel.appendChild(o); }
    chrome.storage.local.set({ base: state.base, key: state.key });
    setStatus(`${d.implementation?.name || 'server'} ${d.implementation?.version || ''}`.trim(), 'ok');
    $('task').hidden = false;
    $('connectNote').textContent = `${state.harnesses.length} harness${state.harnesses.length === 1 ? '' : 'es'}`;
  } catch (e) {
    setStatus('not connected', 'bad');
    $('connectNote').textContent = `Could not connect: ${e.message}`;
  }
}

function values() {
  const out = {};
  for (const line of $('values').value.split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) out['value:' + line.slice(0, i).trim()] = line.slice(i + 1);
  }
  return out;
}

function addStep(html, cls) {
  const li = document.createElement('li'); li.className = cls || ''; li.innerHTML = html; $('steps').appendChild(li);
  li.scrollIntoView({ block: 'nearest' });
}
const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

async function start(previous) {
  const goal = $('goal').value.trim();
  if (!goal) { $('taskNote').textContent = 'Give the loop a goal.'; return; }
  const body = { input: goal, stream: true, metadata: { harness_id: $('harness').value, ...values() } };
  if (previous) body.previous_response_id = previous;
  $('steps').innerHTML = ''; $('final').hidden = true; $('trace').hidden = false;
  $('traceMeta').textContent = ''; $('taskNote').textContent = '';
  $('startBtn').disabled = true; $('cancelBtn').hidden = false; $('continueBtn').hidden = true;
  setStatus('running', 'running');
  state.controller = new AbortController(); state.startedAt = performance.now();
  let calls = 0;
  try {
    const r = await fetch(`${state.base}/v1/responses`, { method: 'POST', headers: headers(), body: JSON.stringify(body), signal: state.controller.signal });
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, nl); buf = buf.slice(nl + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const ev = JSON.parse(line.slice(6));
          handle(ev);
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') { setStatus('failed', 'bad'); $('taskNote').textContent = e.message; }
  } finally {
    $('startBtn').disabled = false; $('cancelBtn').hidden = true;
    if (state.responseId) $('continueBtn').hidden = false;
  }

  function handle(ev) {
    const t = ev.type;
    if (t === 'response.created') { state.responseId = ev.response.id; state.sessionId = ev.response.metadata?.session_id || null;
      $('traceTitle').textContent = `Steps · ${state.responseId.slice(0, 12)}…`; return; }
    if (t === 'response.output_item.done') {
      const it = ev.item; const ms = Math.round(performance.now() - state.startedAt);
      if (it.type === 'function_call') { calls += 1; addStep(`<span class="action">${esc(it.name)}(${esc(it.arguments)})</span> <span class="note">${ms} ms</span>`); }
      else if (it.type === 'function_call_output') addStep(`<span class="result">${esc(it.output)}</span>`, 'out');
      else if (it.type === 'reasoning') addStep(`<span class="reason">${esc((it.summary || [])[0]?.text || '')}</span>`);
      else if (it.type === 'message') { $('final').textContent = (it.content || [])[0]?.text || ''; $('final').hidden = false; }
      return;
    }
    if (t === 'response.completed' || t === 'response.incomplete' || t === 'response.failed') {
      const r = ev.response; const u = r.usage; const secs = ((performance.now() - state.startedAt) / 1000).toFixed(1);
      setStatus(r.status + (r.incomplete_details?.reason ? `: ${r.incomplete_details.reason}` : ''), r.status === 'completed' ? 'ok' : 'bad');
      $('traceMeta').textContent = `${calls} action${calls === 1 ? '' : 's'} · ${secs} s` + (u ? ` · ${u.input_tokens} in / ${u.output_tokens} out` : '') + (r.model ? ` · ${r.model}` : '');
      if (r.error) $('taskNote').textContent = r.error.message || JSON.stringify(r.error);
    }
  }
}

async function cancel() {
  if (!state.responseId) return;
  try { await fetch(`${state.base}/v1/responses/${state.responseId}/cancel`, { method: 'POST', headers: headers() }); } catch {}
}

$('connectBtn').addEventListener('click', connect);
$('startBtn').addEventListener('click', () => start(null));
$('continueBtn').addEventListener('click', () => start(state.responseId));
$('cancelBtn').addEventListener('click', cancel);
chrome.storage.local.get(['base', 'key'], (v) => { if (v.base) $('base').value = v.base; if (v.key) $('key').value = v.key; if (v.base && v.key) connect(); });
