// Relay for nfqws2-ui: a router sends strategies here without a GitHub account; the relay stores each submission
// as a file in the «inbox» branch and wakes the intake Action (scripts/run.sh → intake.py --inbox).
// The GitHub token lives only here (secret GH_TOKEN). The router's installation number never leaves the relay:
// the base gets «r» + 12 hex of sha256(SALT + number), the address — 12 hex of sha256(SALT + "ip:" + address).
//
// POST /v1/submit  {id, name?, items: [{kind, asn, provider, proto, steps[], net?, host?, nfqws2?, ui?}]}
//                  → {ok, me, queued}   (me — the public hash: the router reads stats/<me>.json)
// POST /v1/me      {id} → {me}
// Checks here are about shape and size only; what goes into the base is decided by intake.py.

const REPO = 'zemidala/nfqws2-strategies';
const BRANCH = 'inbox';
const MAX_BODY = 16384;
const MAX_ITEMS = 30;
const MAX_STEPS = 8;
let lastDispatch = 0;   // per isolate: a loose throttle, the half-hourly schedule picks up the rest

const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } });

async function hash(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12);
}

const str = (v, max) => (typeof v === 'string' && v.length <= max ? v : '');
const int = (v) => (Number.isInteger(v) && v > 0 && v < 4294967296 ? v : null);

// keep only the fields intake.py reads, each within its size; anything else is dropped
function clean(it) {
  if (!it || typeof it !== 'object' || !['share', 'works', 'fails'].includes(it.kind)) return null;
  const steps = Array.isArray(it.steps) ? it.steps.filter((s) => typeof s === 'string' && s.length <= 400).slice(0, MAX_STEPS) : [];
  if (!steps.length || !int(it.asn) || !['tls', 'http'].includes(it.proto)) return null;
  return { kind: it.kind, asn: it.asn, provider: str(it.provider, 80), proto: it.proto, steps,
    net: int(it.net), host: str(it.host, 253), nfqws2: str(it.nfqws2, 31), ui: str(it.ui, 31) };
}

async function gh(env, method, path, body) {
  return fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    method,
    headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: 'application/vnd.github+json', 'user-agent': 'nfqws2-strategies-relay',
      'x-github-api-version': '2022-11-28', 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
}

function b64(text) {
  const bytes = new TextEncoder().encode(text);
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

async function readBody(req) {
  const text = await req.text();
  if (text.length > MAX_BODY) throw new Error('too big');
  return JSON.parse(text);
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method !== 'POST') return json({ error: 'POST only' }, 405);
    let body;
    try { body = await readBody(req); } catch { return json({ error: 'bad body' }, 400); }
    const id = typeof body.id === 'string' && /^[0-9a-f]{32}$/.test(body.id) ? body.id : null;
    if (!id) return json({ error: 'bad id' }, 400);
    const me = 'r' + await hash(env.SALT + id);
    if (url.pathname === '/v1/me') return json({ me });
    if (url.pathname !== '/v1/submit') return json({ error: 'not found' }, 404);

    const items = (Array.isArray(body.items) ? body.items : []).slice(0, MAX_ITEMS).map(clean).filter(Boolean);
    if (!items.length) return json({ error: 'nothing to send' }, 400);
    const name = typeof body.name === 'string' && /^[0-9A-Za-zА-Яа-яЁё _-]{1,24}$/.test(body.name.trim()) ? body.name.trim() : '';
    const ip = await hash(env.SALT + 'ip:' + (req.headers.get('cf-connecting-ip') || ''));
    const t = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const file = `inbox/${t.replace(/[-:]/g, '')}-${me}-${crypto.randomUUID().slice(0, 8)}.json`;
    const content = b64(JSON.stringify({ t, who: me, ip, name, items }));
    let res;
    for (let i = 0; i < 4; i++) {   // two files at once may race on the branch head: retry
      res = await gh(env, 'PUT', `contents/${file}`, { message: 'inbox', content, branch: BRANCH });
      if (res.status !== 409 && res.status !== 422) break;
      await new Promise((r) => setTimeout(r, 300 + Math.random() * 700));
    }
    if (!res.ok) return json({ error: 'store failed', status: res.status }, 502);
    if (Date.now() - lastDispatch > 120000) {
      lastDispatch = Date.now();
      ctx.waitUntil(gh(env, 'POST', 'dispatches', { event_type: 'relay' }));
    }
    return json({ ok: true, me, queued: items.length });
  },
};
