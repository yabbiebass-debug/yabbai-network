/*
 * realm-client.js — a drop-in replacement for @supabase/supabase-js in the
 * YABBAI surfaces.
 *
 * The six surfaces (/app /os /floor /studio /portal /vault) were written against
 * Supabase Postgres. That project was never provisioned — the schema was never run
 * and the four edge functions were never written — so every surface boots into a
 * "paste your keys" gate and shows nothing.
 *
 * This exposes the same shape (.from().select().eq().order(), .rpc(),
 * .functions.invoke(), .channel(), .auth) but talks to /api/realm on the unified
 * backend, which is already deployed, already behind Google + 2FA, and already
 * holds the data. Swapping the import is the whole migration.
 *
 * Deliberate differences from Supabase, all of them honest ones:
 *   · auth is the network's Google + TOTP session, not a magic link. There is one
 *     sign-in for the whole realm; signInWithOtp redirects to it.
 *   · realtime is polling. No websocket, so it says so rather than pretending to
 *     be push. 12s default, pauses when the tab is hidden.
 *   · a failed read returns { data:null, error } — callers that render a count
 *     must show "—", never a confident 0.
 */

const BASE = window.location.origin + '/api/realm';

async function post(path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body || {}),
    });
  } catch (e) {
    return { data: null, error: { message: 'Network unreachable — is the backend up?' } };
  }
  let payload = null;
  try { payload = await res.json(); } catch (_) { payload = null; }
  if (!res.ok) {
    const message = (payload && (payload.detail || payload.message)) || `Request failed (${res.status})`;
    return { data: null, error: { message, status: res.status } };
  }
  return { payload };
}

/* ── query builder ───────────────────────────────────────────────────────────
   Thenable, like PostgREST's: the request fires when you await it, so existing
   chains such as .select('*').eq('status','pending').order('created_at') work
   unchanged. */
class Query {
  constructor(table) {
    this._ = { table, columns: '*', filters: [], order: null, limit: null,
               count: false, head: false, single: false, maybe_single: false };
    this._mode = 'select';
    this._rows = null;
    this._patch = null;
  }
  select(columns = '*', opts = {}) {
    // .insert(...).select().single() is a returning-clause, not a new query.
    if (this._mode !== 'insert') this._mode = 'select';
    this._.columns = columns || '*';
    if (opts.count) this._.count = true;
    if (opts.head) { this._.head = true; this._.count = true; }
    return this;
  }
  insert(rows) { this._mode = 'insert'; this._rows = Array.isArray(rows) ? rows : [rows]; return this; }
  update(patch) { this._mode = 'update'; this._patch = patch; return this; }
  eq(col, val)  { this._.filters.push({ op: 'eq',  col, val }); return this; }
  neq(col, val) { this._.filters.push({ op: 'neq', col, val }); return this; }
  gt(col, val)  { this._.filters.push({ op: 'gt',  col, val }); return this; }
  gte(col, val) { this._.filters.push({ op: 'gte', col, val }); return this; }
  lt(col, val)  { this._.filters.push({ op: 'lt',  col, val }); return this; }
  lte(col, val) { this._.filters.push({ op: 'lte', col, val }); return this; }
  in(col, val)  { this._.filters.push({ op: 'in',  col, val }); return this; }
  order(col, opts = {}) { this._.order = { col, ascending: opts.ascending !== false }; return this; }
  limit(n) { this._.limit = n; return this; }
  single() { this._.single = true; return this; }
  maybeSingle() { this._.maybe_single = true; return this; }

  async _run() {
    if (this._mode === 'insert') {
      const r = await post('/insert', { table: this._.table, rows: this._rows });
      if (r.error) return r;
      const rows = r.payload.data || [];
      if (this._.single || this._.maybe_single) {
        return { data: rows[0] || null, error: null };
      }
      return { data: rows, error: null };
    }
    if (this._mode === 'update') {
      const r = await post('/update', {
        table: this._.table, patch: this._patch, filters: this._.filters });
      if (r.error) return r;
      return { data: r.payload, error: null };
    }
    const r = await post('/select', this._);
    if (r.error) return { data: null, count: null, error: r.error };
    return { data: r.payload.data, count: r.payload.count, error: null };
  }

  then(resolve, reject) { return this._run().then(resolve, reject); }
  catch(fn) { return this._run().catch(fn); }
}

/* ── polling channel ─────────────────────────────────────────────────────── */
class Channel {
  constructor(name) { this.name = name; this._handlers = []; this._timer = null; }
  on(_event, _filter, handler) { this._handlers.push(handler); return this; }
  subscribe(cb) {
    const tick = () => {
      if (document.visibilityState === 'visible') {
        this._handlers.forEach(h => { try { h({ source: 'poll' }); } catch (_) {} });
      }
    };
    this._timer = setInterval(tick, Channel.INTERVAL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') tick();
    });
    if (cb) cb('SUBSCRIBED');
    return this;
  }
  unsubscribe() { if (this._timer) clearInterval(this._timer); return this; }
}
Channel.INTERVAL_MS = 12000;

/* ── auth ─────────────────────────────────────────────────────────────────── */
const auth = {
  async me() {
    try {
      const r = await fetch(window.location.origin + '/api/auth/me', { credentials: 'include' });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  },
  async getSession() {
    const me = await auth.me();
    // /api/auth/me returns the FastAPI user with fields at the TOP LEVEL
    // (user_id, email, role, mfa_verified) — there is no `.user` wrapper.
    if (!me || !me.user_id) return { data: { session: null } };
    // A Director who hasn't cleared 2FA is not yet inside the realm.
    if (me.mfa_required && !me.mfa_verified) {
      return { data: { session: null }, mfa: 'required' };
    }
    return { data: { session: { user: { ...me, id: me.user_id }, mfa_verified: true } } };
  },
  async signInWithOtp() {
    // One realm, one sign-in. Send them to it rather than pretending to email a link.
    window.location.href = '/login/index.html?next=' + encodeURIComponent(window.location.pathname);
    return { error: null };
  },
  async signOut() {
    try {
      await fetch(window.location.origin + '/api/auth/logout',
                  { method: 'POST', credentials: 'include' });
    } catch (_) {}
    return { error: null };
  },
  onAuthStateChange(cb) {
    // Session state only changes via the /login round-trip, which reloads the page.
    // Re-check on focus so a completed sign-in in another tab lands here too.
    const check = async () => {
      const { data: { session } } = await auth.getSession();
      if (session) cb('SIGNED_IN', session);
    };
    window.addEventListener('focus', check);
    return { data: { subscription: { unsubscribe: () => window.removeEventListener('focus', check) } } };
  },
};

/* ── the client ───────────────────────────────────────────────────────────── */
export function createRealmClient() {
  return {
    from: (table) => new Query(table),
    rpc: async (name, args) => {
      const r = await post('/rpc/' + name, args || {});
      if (r.error) return { data: null, error: r.error };
      return { data: r.payload.data, error: null };
    },
    functions: {
      invoke: async (name, opts = {}) => {
        const r = await post('/fn/' + name, opts.body || {});
        if (r.error) return { data: null, error: r.error };
        return { data: r.payload, error: null };
      },
    },
    channel: (name) => new Channel(name),
    removeChannel: (ch) => ch && ch.unsubscribe(),
    auth,
  };
}

// Signature-compatible with supabase-js so a surface only changes its import line.
export function createClient() { return createRealmClient(); }

export default createRealmClient;
