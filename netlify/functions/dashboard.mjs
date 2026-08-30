import { getStore } from '@netlify/blobs';
import { timingSafeEqual } from 'node:crypto';

/**
 * netlify.toml [[headers]] apply to static assets, not to what a function
 * returns. A probe of the deployed 401 came back with neither X-Robots-Tag
 * nor X-Frame-Options, so the board sets them itself.
 */
const SECURITY = {
  'x-content-type-options': 'nosniff',
  'x-frame-options': 'DENY',
  'referrer-policy': 'strict-origin-when-cross-origin',
  'x-robots-tag': 'noindex, nofollow',
  'cache-control': 'no-store',
};

/**
 * The public status board. Read-only on purpose.
 *
 * dashboard_web.py on the crawl box also exposes /api/load, /api/schema,
 * /api/reconcile and /api/autoload, which start subprocesses. None of that
 * can or should live behind a public URL, so it stays on the box, reachable
 * only through an SSH tunnel. What is here is the half that is safe to read
 * from a phone: how far along the crawl is, and whether it is still moving.
 */
export default async (request) => {
  const expected = process.env.CATTLE_VIEW_PASSWORD;
  if (!expected) {
    // Same refusal as the ingest endpoint. A missing password must not mean
    // "no password required" -- that is how a private board goes public
    // without anyone noticing.
    return new Response('CATTLE_VIEW_PASSWORD is not set on this site', {
      status: 503,
      headers: SECURITY,
    });
  }
  if (!authorized(request.headers.get('authorization'), expected)) {
    // Basic auth: the browser draws the login box, so there is no login page
    // to build and no session cookie to get wrong.
    return new Response('Authentication required', {
      status: 401,
      headers: {
        ...SECURITY,
        'www-authenticate': 'Basic realm="cattle-graph", charset="UTF-8"',
      },
    });
  }

  const store = getStore('cattle-graph');
  const { blobs } = await store.list({ prefix: 'status/' });
  const entries = (await Promise.all(
    blobs.map(({ key }) => store.get(key, { type: 'json' }).catch(() => null)),
  )).filter(Boolean);

  const summary = summarize(entries);

  if (new URL(request.url).pathname.endsWith('.json')) {
    return new Response(JSON.stringify(summary, null, 2), {
      headers: { ...SECURITY, 'content-type': 'application/json' },
    });
  }
  return new Response(renderHtml(summary), {
    headers: { ...SECURITY, 'content-type': 'text/html; charset=utf-8' },
  });
};

/**
 * One board per association, newest publisher wins if two ever report the
 * same one. Staleness is computed here rather than in the page, because a
 * board that has stopped moving is the single thing this page exists to
 * show -- a crawler that dies does not error, its numbers just stop.
 */
export function summarize(entries) {
  const now = Date.now();
  const byAssoc = new Map();
  let publishedAt = null;

  for (const e of entries) {
    if (!publishedAt || e.publishedAt > publishedAt) publishedAt = e.publishedAt;
    for (const b of e.boards ?? []) {
      const prev = byAssoc.get(b.association);
      if (!prev || (b.updated_at ?? '') > (prev.updated_at ?? '')) {
        byAssoc.set(b.association, { ...b, source: e.source });
      }
    }
  }

  const boards = [...byAssoc.values()].map((b) => {
    const ageSec = b.updated_at ? Math.round((now - Date.parse(b.updated_at)) / 1000) : null;
    const rate = Number(b.rate_per_min) || 0;
    const pending = Number(b.pending) || 0;
    return {
      ...b,
      ageSec,
      // A crawler writes its status file on every animal, so ~17s at 3.5/min.
      // Five minutes of silence means it is gone, not slow.
      stale: ageSec === null || ageSec > 300,
      etaDays: rate > 0 ? +(pending / rate / 60 / 24).toFixed(1) : null,
    };
  }).sort((a, b) => a.association.localeCompare(b.association));

  const totals = boards.reduce((t, b) => ({
    done: t.done + (Number(b.done) || 0),
    pending: t.pending + (Number(b.pending) || 0),
    failed: t.failed + (Number(b.failed) || 0),
  }), { done: 0, pending: 0, failed: 0 });

  const publishAgeSec = publishedAt ? Math.round((now - Date.parse(publishedAt)) / 1000) : null;

  return {
    boards,
    totals,
    publishedAt,
    publishAgeSec,
    // The box publishes every 60s; five minutes of silence means the box
    // itself is the problem, not any one crawler.
    reporting: publishAgeSec !== null && publishAgeSec < 300,
    disk: entries.find((e) => e.disk)?.disk ?? null,
  };
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const ago = (s) => s === null ? 'never'
  : s < 90 ? `${s}s ago`
  : s < 5400 ? `${Math.round(s / 60)}m ago`
  : s < 172800 ? `${Math.round(s / 3600)}h ago`
  : `${Math.round(s / 86400)}d ago`;

const num = (n) => (Number(n) || 0).toLocaleString('en-US');

export function renderHtml(s) {
  const pctTotal = s.totals.done + s.totals.pending > 0
    ? (s.totals.done / (s.totals.done + s.totals.pending) * 100) : 0;

  const cards = s.boards.map((b) => `
    <article class="card${b.stale ? ' stale' : ''}">
      <header>
        <h2>${esc(b.association)}</h2>
        <span class="pill ${b.stale ? 'bad' : 'good'}">${b.stale ? 'stalled' : esc(b.state ?? 'running')}</span>
      </header>
      <div class="bar"><span style="width:${Math.min(100, Number(b.pct) || 0)}%"></span></div>
      <p class="pct">${(Number(b.pct) || 0).toFixed(1)}%</p>
      <dl>
        <div><dt>done</dt><dd>${num(b.done)}</dd></div>
        <div><dt>pending</dt><dd>${num(b.pending)}</dd></div>
        <div><dt>rate</dt><dd>${esc(b.rate_per_min ?? 0)}/min</dd></div>
        <div><dt>eta</dt><dd>${b.etaDays !== null ? `${b.etaDays}d` : '&mdash;'}</dd></div>
        ${Number(b.failed) ? `<div><dt>failed</dt><dd class="warn">${num(b.failed)}</dd></div>` : ''}
      </dl>
      <footer>updated ${esc(ago(b.ageSec))}</footer>
    </article>`).join('');

  return `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cattle-graph</title>
<style>
  :root{color-scheme:light dark;--bg:#fbfaf8;--fg:#1a1a19;--dim:#6b6b66;--line:#e4e1db;
        --card:#fff;--good:#2a7f4f;--bad:#b4322a;--accent:#8a6a3c}
  @media (prefers-color-scheme:dark){
    :root{--bg:#16161a;--fg:#eceae5;--dim:#96948d;--line:#2c2c33;--card:#1d1d22;
          --good:#5ec07f;--bad:#e8776c;--accent:#c9a25e}}
  *{box-sizing:border-box}
  body{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--fg);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  main{max-width:60rem;margin:0 auto}
  h1{font-size:1.35rem;margin:0 0 .2rem;letter-spacing:-.01em}
  .sub{color:var(--dim);font-size:.85rem;margin:0 0 1.75rem}
  .banner{background:var(--bad);color:#fff;padding:.6rem .9rem;border-radius:.5rem;
          margin:0 0 1.5rem;font-size:.88rem}
  .totals{display:flex;flex-wrap:wrap;gap:1.75rem;padding:1.1rem 1.25rem;background:var(--card);
          border:1px solid var(--line);border-radius:.7rem;margin-bottom:1.5rem}
  .totals div{min-width:6rem}
  .totals dt{color:var(--dim);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;margin:0 0 .15rem}
  .totals dd{margin:0;font-size:1.5rem;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
  .grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))}
  /* Flex column so a card with no failed-count still sits its footer on the
     baseline instead of floating mid-card. */
  .card{background:var(--card);border:1px solid var(--line);border-radius:.7rem;
        padding:1.1rem 1.25rem;display:flex;flex-direction:column}
  .card.stale{border-color:var(--bad)}
  .card header{display:flex;align-items:center;justify-content:space-between;margin-bottom:.85rem}
  .card h2{font-size:1rem;margin:0;letter-spacing:.02em}
  .pill{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;padding:.2rem .5rem;
        border-radius:1rem;border:1px solid currentColor}
  .pill.good{color:var(--good)} .pill.bad{color:var(--bad)}
  .bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden}
  .bar span{display:block;height:100%;background:var(--accent)}
  .pct{margin:.4rem 0 .9rem;font-size:.8rem;color:var(--dim);font-variant-numeric:tabular-nums}
  .card dl{display:grid;grid-template-columns:1fr 1fr;gap:.6rem 1rem;margin:0}
  .card dt{color:var(--dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}
  .card dd{margin:0;font-variant-numeric:tabular-nums}
  .card dd.warn{color:var(--bad)}
  .card footer{margin-top:auto;padding-top:.7rem;border-top:1px solid var(--line);
               color:var(--dim);font-size:.75rem}
  .card dl{margin-bottom:.9rem}
  .foot{margin-top:2rem;color:var(--dim);font-size:.78rem}
</style>
<main>
  <h1>cattle-graph</h1>
  <p class="sub">pedigree crawl across four breed registries &middot; read-only</p>

  ${s.reporting ? '' : `<p class="banner">The crawl box last reported ${esc(ago(s.publishAgeSec))}.
     These numbers are not live &mdash; check the instance.</p>`}

  <dl class="totals">
    <div><dt>animals</dt><dd>${num(s.totals.done)}</dd></div>
    <div><dt>pending</dt><dd>${num(s.totals.pending)}</dd></div>
    <div><dt>complete</dt><dd>${pctTotal.toFixed(1)}%</dd></div>
    ${s.totals.failed ? `<div><dt>failed</dt><dd>${num(s.totals.failed)}</dd></div>` : ''}
    ${s.disk ? `<div><dt>disk free</dt><dd>${esc(s.disk.free_gb)}G</dd></div>` : ''}
  </dl>

  <div class="grid">${cards || '<p class="sub">Nothing published yet.</p>'}</div>

  <p class="foot">published ${esc(ago(s.publishAgeSec))} &middot;
     controls stay on the crawl box, reachable over SSH only</p>
</main>`;
}

function authorized(header, expected) {
  const raw = String(header ?? '');
  if (!/^Basic\s+/i.test(raw)) return false;
  let decoded;
  try {
    decoded = Buffer.from(raw.replace(/^Basic\s+/i, ''), 'base64').toString('utf8');
  } catch { return false; }
  // Any username; the password is the secret. Constant-time so it cannot be
  // guessed a byte at a time.
  const given = decoded.slice(decoded.indexOf(':') + 1);
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export const config = { path: ['/', '/summary.json'] };
