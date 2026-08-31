import { getStore } from '@netlify/blobs';

/**
 * netlify.toml [[headers]] apply to static assets, not to what a function
 * returns. A probe of the deployed function came back with neither
 * X-Robots-Tag nor X-Frame-Options, so the board sets them itself.
 */
const SECURITY = {
  'x-content-type-options': 'nosniff',
  'x-frame-options': 'DENY',
  'referrer-policy': 'strict-origin-when-cross-origin',
  'x-robots-tag': 'noindex, nofollow',
  'cache-control': 'no-store',
};

/**
 * The public status board. Read-only on purpose, and open: it carries counts,
 * rates and the registration of the animal being read, none of which is worth
 * a password. The gate is on what is published, not on who may look --
 * publish_status.py projects each status file down to an allowlist and
 * publish.mjs rejects anything outside it, so nothing reaches this page that
 * a stranger should not see.
 *
 * dashboard_web.py on the crawl box also exposes /api/load, /api/schema,
 * /api/reconcile and /api/autoload, which start subprocesses. None of that
 * can or should live behind a public URL, so it stays on the box, reachable
 * only through an SSH tunnel. What is here is the half that is safe to read
 * from a phone: how far along the crawl is, and whether it is still moving.
 */
export default async (request) => {
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
  let graph = null, graphFrom = null;

  for (const e of entries) {
    if (!publishedAt || e.publishedAt > publishedAt) publishedAt = e.publishedAt;
    // Newest graph reading wins, independently of which publisher sent it --
    // only the box running the loader has one at all.
    if (e.graph && (!graphFrom || e.publishedAt > graphFrom)) {
      graph = e.graph;
      graphFrom = e.publishedAt;
    }
    for (const b of e.boards ?? []) {
      const prev = byAssoc.get(b.association);
      if (!prev || (b.updated_at ?? '') > (prev.updated_at ?? '')) {
        byAssoc.set(b.association, { ...b, source: e.source });
      }
    }
  }

  const inGraph = graph?.by_association ?? {};

  const boards = [...byAssoc.values()].map((b) => {
    const ageSec = b.updated_at ? Math.round((now - Date.parse(b.updated_at)) / 1000) : null;
    const rate = Number(b.rate_per_min) || 0;
    const pending = Number(b.pending) || 0;
    const g = inGraph[b.association];
    return {
      ...b,
      ageSec,
      // What is actually in Neo4j for this association, and how many crawled
      // records are still waiting to get there.
      graphed: g ? Number(g.registrations) || 0 : null,
      behind: g ? Number(g.behind) || 0 : null,
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

  // The graph reading comes from a loop inside the same process as the loader,
  // so a reading that has stopped advancing means loading has stopped -- the
  // one failure the crawl counts cannot show, because the crawlers keep
  // filling JSONL whether or not anything is reading it.
  const graphAgeSec = graph?.read_at
    ? Math.round((now - Date.parse(graph.read_at)) / 1000) : null;
  const loading = Boolean(graph?.ok) && graphAgeSec !== null && graphAgeSec < 300;
  const behindTotal = boards.reduce((t, b) => t + (Number(b.behind) || 0), 0);

  // When the whole crawl finishes is the LONGEST leg, not the sum: the three
  // crawlers run at once against separate registries, so MAINE finishing in
  // three weeks does nothing for CHIA's eight. Summing them would roughly
  // treble the real answer.
  const etas = boards.map((b) => b.etaDays).filter((d) => d !== null);
  const etaDaysOverall = etas.length ? Math.max(...etas) : null;

  return {
    boards,
    totals,
    etaDaysOverall,
    publishedAt,
    publishAgeSec,
    // The box publishes every 60s; five minutes of silence means the box
    // itself is the problem, not any one crawler.
    reporting: publishAgeSec !== null && publishAgeSec < 300,
    graph,
    graphAgeSec,
    loading,
    behindTotal,
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

/** A span of days, said the way a person would say it. */
const dur = (days) => {
  if (days === null || !isFinite(days)) return '&mdash;';
  if (days < 1) return `${Math.max(1, Math.round(days * 24))} hr`;
  if (days < 14) return `${Math.round(days)} days`;
  if (days < 70) return `${(days / 7).toFixed(1)} wks`;
  return `${(days / 30.44).toFixed(1)} mo`;
};

/**
 * Why nothing is being written, in the words that name the thing to go and
 * look at. All three mean the same to a reader -- the graph is not moving --
 * and completely different things to whoever has to fix it.
 */
function loadWarning(s) {
  if (!s.graph) return 'No graph reading has ever been published.';
  if (!s.graph.ok) return 'Neo4j is not answering the crawl box.';
  return `The graph was last read ${ago(s.graphAgeSec)}, so the loader has stopped.`;
}

/** Genetic-test tallies across the whole graph, carriers called out. */
function defectRow(defects) {
  const entries = Object.entries(defects ?? {}).filter(([, n]) => Number(n) > 0);
  if (!entries.length) return '';
  return `<ul class="defects">${entries.map(([status, n]) =>
    `<li class="${status === 'Carrier' ? 'carrier' : ''}">${esc(status)}<b>${num(n)}</b></li>`,
  ).join('')}</ul>`;
}

export function renderHtml(s) {
  const pctTotal = s.totals.done + s.totals.pending > 0
    ? (s.totals.done / (s.totals.done + s.totals.pending) * 100) : 0;

  const cards = s.boards.map((b) => `
    <article class="card${b.stale ? ' stale' : ''}">
      <header class="card-head">
        <h2>${esc(b.association)}</h2>
        <span class="pill ${b.stale ? 'bad' : 'good'}">${b.stale ? 'stalled' : esc(b.state ?? 'running')}</span>
      </header>

      <div class="bar" role="img" aria-label="${(Number(b.pct) || 0).toFixed(1)} percent complete">
        <span style="width:${Math.min(100, Number(b.pct) || 0)}%"></span>
      </div>
      <p class="pct"><b>${(Number(b.pct) || 0).toFixed(1)}%</b> of known herd</p>

      <dl class="figures">
        <div><dt>Recorded</dt><dd>${num(b.done)}</dd></div>
        <div><dt>Remaining</dt><dd>${num(b.pending)}</dd></div>
        <div><dt>Rate</dt><dd>${esc(b.rate_per_min ?? 0)}<span class="unit">/min</span></dd></div>
        <div><dt>Finishes</dt><dd>${b.etaDays !== null
          ? `${b.etaDays}<span class="unit">d</span>` : '&mdash;'}</dd></div>
        ${b.graphed !== null ? `<div><dt>In graph</dt><dd>${num(b.graphed)}</dd></div>` : ''}
        ${b.behind ? `<div><dt>To load</dt><dd class="warn">${num(b.behind)}</dd></div>` : ''}
        ${Number(b.failed) ? `<div><dt>Failed</dt><dd class="warn">${num(b.failed)}</dd></div>` : ''}
      </dl>

      ${b.current?.reg ? `<p class="now">
        <span class="dot" aria-hidden="true"></span>
        <span class="now-txt"><span class="reg">${esc(b.current.reg)}</span>
        <span class="nm">${esc(b.current.name || '')}</span></span>
      </p>` : ''}

      <footer>read ${esc(ago(b.ageSec))}</footer>
    </article>`).join('');

  return `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Cattle Graph</title>
<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="manifest" href="/site.webmanifest">
<meta name="apple-mobile-web-app-title" content="Cattle Graph">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f7f1e6">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#17130d">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bitter:wght@500;600;700&display=swap">
<style>
  /* --------------------------------------------------------------------
     Ranch ledger. Warm paper, saddle tan, pasture green, barn red. The
     restraint is deliberate: this is a thing glanced at on a phone in a
     barn, so the numbers carry the page and the theme stays in the palette
     and the slab serif rather than in decoration.
     Mobile first -- every base rule targets a narrow screen, and the two
     min-width blocks at the end are the only widening.
     -------------------------------------------------------------------- */
  :root{
    --paper:#f7f1e6; --card:#fffdf8; --ink:#241c12; --dim:#7d6b55;
    --rule:#e2d6c0; --rule-soft:#efe6d5;
    --tan:#a8763a; --wheat:#d8b56a;
    --pasture:#4a7c4e; --barn:#a33a2c;
    --shadow:0 1px 2px rgba(60,42,20,.05), 0 6px 16px -10px rgba(60,42,20,.22);
  }
  @media (prefers-color-scheme:dark){
    :root{
      --paper:#17130d; --card:#211a12; --ink:#f2e9da; --dim:#a08e76;
      --rule:#3a2f20; --rule-soft:#2b2318;
      --tan:#c99553; --wheat:#e0bd76;
      --pasture:#77b07a; --barn:#e0786a;
      --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 18px -12px rgba(0,0,0,.7);
    }
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0; padding:1.5rem 1.1rem 3rem;
    background:var(--paper); color:var(--ink);
    font:400 16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    /* A whisper of tooth in the paper. Two very low-contrast washes, no image. */
    background-image:
      radial-gradient(ellipse at 12% -10%, rgba(168,118,58,.07), transparent 60%),
      radial-gradient(ellipse at 92% 4%, rgba(74,124,78,.05), transparent 55%);
    background-attachment:fixed;
  }
  main{max-width:64rem;margin:0 auto}

  /* ---------- masthead ---------- */
  .masthead{margin:0 0 1.5rem;text-align:center}
  .masthead h1{
    font-family:Bitter,Georgia,"Iowan Old Style",serif;
    font-weight:700; font-size:clamp(1.5rem,7vw,2.1rem); line-height:1.1;
    margin:0; letter-spacing:.02em; text-transform:uppercase;
  }
  .masthead .sub{
    margin:.5rem 0 0; color:var(--dim);
    font-size:.76rem; letter-spacing:.16em; text-transform:uppercase;
  }
  /* Ledger rule: a heavy hair over a light one, the way a stock certificate
     or a feed-store letterhead separates the name from the business. */
  .masthead::after{
    content:""; display:block; margin:.95rem auto 0; width:min(100%,22rem); height:3px;
    border-top:2px solid var(--tan); border-bottom:1px solid var(--rule);
  }

  .banner{
    background:var(--barn); color:#fff; padding:.75rem .95rem; border-radius:.6rem;
    margin:0 0 1.25rem; font-size:.9rem; line-height:1.4;
    box-shadow:var(--shadow);
  }

  /* ---------- totals ---------- */
  .totals{
    display:grid; grid-template-columns:repeat(2,1fr); gap:1rem .75rem;
    margin:0 0 1.4rem; padding:1.1rem 1rem;
    background:var(--card); border:1px solid var(--rule); border-radius:.85rem;
    box-shadow:var(--shadow);
  }
  .totals dt{
    color:var(--dim); font-size:.66rem; letter-spacing:.12em;
    text-transform:uppercase; margin:0 0 .2rem;
  }
  .totals dd{
    margin:0; font-family:Bitter,Georgia,serif; font-weight:600;
    font-size:clamp(1.25rem,5.5vw,1.6rem); line-height:1.1;
    font-variant-numeric:tabular-nums; letter-spacing:-.01em;
  }
  /* "How long until this is over" is the question the page gets asked most,
     so it is the one figure given the accent colour. */
  .totals .eta dd{color:var(--tan)}

  /* ---------- cards ---------- */
  .grid{display:grid; gap:1rem; grid-template-columns:1fr}
  .card{
    background:var(--card); border:1px solid var(--rule); border-radius:.85rem;
    padding:1.15rem 1.15rem 1rem; display:flex; flex-direction:column;
    box-shadow:var(--shadow);
    /* A branded edge, like a paint stripe down a stall post. */
    border-left:4px solid var(--tan);
  }
  .card.stale{border-left-color:var(--barn)}
  .card-head{
    display:flex; align-items:center; justify-content:space-between;
    gap:.75rem; margin-bottom:.9rem;
  }
  .card-head h2{
    font-family:Bitter,Georgia,serif; font-weight:700; font-size:1.15rem;
    margin:0; letter-spacing:.06em;
  }
  .pill{
    font-size:.62rem; letter-spacing:.13em; text-transform:uppercase;
    padding:.28rem .6rem; border-radius:1rem; border:1px solid currentColor;
    white-space:nowrap; flex:none;
  }
  .pill.good{color:var(--pasture)} .pill.bad{color:var(--barn)}

  .bar{height:7px; background:var(--rule-soft); border-radius:4px; overflow:hidden}
  .bar span{
    display:block; height:100%; border-radius:4px;
    background:linear-gradient(90deg,var(--tan),var(--wheat));
  }
  .pct{margin:.5rem 0 1rem; font-size:.8rem; color:var(--dim)}
  .pct b{color:var(--ink); font-variant-numeric:tabular-nums; font-weight:600}

  .figures{display:grid; grid-template-columns:repeat(2,1fr); gap:.85rem 1rem; margin:0 0 1rem}
  .figures dt{
    color:var(--dim); font-size:.64rem; letter-spacing:.11em;
    text-transform:uppercase; margin-bottom:.15rem;
  }
  .figures dd{
    margin:0; font-variant-numeric:tabular-nums;
    font-size:1.05rem; font-weight:600;
  }
  .figures .unit{font-weight:400; font-size:.8em; color:var(--dim); margin-left:.1em}
  .figures dd.warn{color:var(--barn)}

  /* ---------- the animal on the stand right now ---------- */
  .now{
    margin:0 0 .85rem; padding:.55rem .7rem;
    background:var(--rule-soft); border-radius:.5rem;
    display:flex; align-items:center; gap:.55rem;
    font-size:.85rem; min-width:0;
  }
  .now-txt{min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .now .reg{font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--dim); font-size:.92em}
  .now .nm{font-weight:600}
  .dot{
    width:7px; height:7px; border-radius:50%; background:var(--pasture); flex:none;
    box-shadow:0 0 0 3px color-mix(in srgb,var(--pasture) 22%,transparent);
    animation:pulse 2.4s ease-in-out infinite;
  }
  .card.stale .dot{background:var(--barn); box-shadow:none; animation:none}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  @media (prefers-reduced-motion:reduce){.dot{animation:none}}

  .card footer{
    margin-top:auto; padding-top:.7rem; border-top:1px solid var(--rule-soft);
    color:var(--dim); font-size:.72rem; letter-spacing:.04em;
  }

  /* ---------- section labels ---------- */
  /* Two sources of truth on one page -- what has been crawled, and what is
     actually in Neo4j. They are different numbers and get confused if the
     page does not say plainly which is which. */
  .sect{
    display:flex; align-items:baseline; justify-content:space-between; gap:.75rem;
    margin:2rem 0 .85rem; padding-bottom:.4rem; border-bottom:1px solid var(--rule);
  }
  .sect h2{
    font-family:Bitter,Georgia,serif; font-weight:700; font-size:.82rem;
    letter-spacing:.16em; text-transform:uppercase; margin:0;
  }
  .sect .when{color:var(--dim); font-size:.7rem; letter-spacing:.04em; white-space:nowrap}
  .sect:first-of-type{margin-top:0}

  /* ---------- defect tallies ---------- */
  .defects{
    display:flex; flex-wrap:wrap; gap:.4rem .5rem; margin:.9rem 0 0;
    padding:0; list-style:none;
  }
  .defects li{
    border:1px solid var(--rule); border-radius:1rem; padding:.25rem .65rem;
    font-size:.72rem; color:var(--dim); white-space:nowrap;
  }
  .defects b{
    color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums;
    margin-left:.3em;
  }
  .defects li.carrier{border-color:var(--barn); color:var(--barn)}
  .defects li.carrier b{color:var(--barn)}

  .foot{
    margin:1.75rem 0 0; text-align:center; color:var(--dim);
    font-size:.72rem; line-height:1.6;
  }

  /* ---------- widening ---------- */
  @media (min-width:34rem){
    body{padding:2.25rem 1.5rem 4rem}
    .totals{grid-template-columns:repeat(3,1fr); padding:1.25rem 1.4rem}
  }
  @media (min-width:52rem){
    .totals{display:flex; flex-wrap:wrap; gap:2rem}
    .totals > div{min-width:6.5rem}
    .grid{grid-template-columns:repeat(auto-fit,minmax(16rem,1fr))}
  }
</style>
<main>
  <div class="masthead">
    <h1>Cattle Graph</h1>
    <p class="sub">Pedigree crawl &middot; four breed registries</p>
  </div>

  ${s.reporting ? '' : `<p class="banner">The crawl box last reported ${esc(ago(s.publishAgeSec))}.
     These numbers are not live &mdash; check the instance.</p>`}

  ${s.reporting && !s.loading ? `<p class="banner">${esc(loadWarning(s))}
     The crawlers keep filling their files either way, so the counts below
     still rise &mdash; but nothing is reaching Neo4j.</p>` : ''}

  <div class="sect"><h2>The crawl</h2>
    <span class="when">read ${esc(ago(s.publishAgeSec))}</span></div>

  <dl class="totals">
    <div><dt>Recorded</dt><dd>${num(s.totals.done)}</dd></div>
    <div><dt>Remaining</dt><dd>${num(s.totals.pending)}</dd></div>
    <div><dt>Complete</dt><dd>${pctTotal.toFixed(1)}%</dd></div>
    <div class="eta"><dt>Time left</dt><dd>${dur(s.etaDaysOverall)}</dd></div>
    ${s.totals.failed ? `<div><dt>Failed</dt><dd>${num(s.totals.failed)}</dd></div>` : ''}
    ${s.disk ? `<div><dt>Disk free</dt><dd>${esc(s.disk.free_gb)}G</dd></div>` : ''}
  </dl>

  <div class="grid">${cards || '<p class="foot">Nothing published yet.</p>'}</div>

  <div class="sect"><h2>The graph</h2>
    <span class="when">${s.graph ? `read ${esc(ago(s.graphAgeSec))}` : 'never read'}</span></div>

  ${s.graph ? `<dl class="totals">
    <div><dt>Animals</dt><dd>${num(s.graph.animals)}</dd></div>
    <div><dt>Registrations</dt><dd>${num(s.graph.registrations)}</dd></div>
    <div><dt>In two registries</dt><dd>${num(s.graph.multi_assoc)}</dd></div>
    <div${s.behindTotal ? ' class="eta"' : ''}><dt>Waiting to load</dt>
      <dd>${s.behindTotal ? num(s.behindTotal) : 'none'}</dd></div>
  </dl>
  ${defectRow(s.graph.defects)}` : `<p class="foot">The box has not published a
     graph reading. That is cattle-dashboard's job &mdash; check it is running.</p>`}

  <p class="foot">Published ${esc(ago(s.publishAgeSec))}<br>
     Animals are deduplicated across registries, so the graph holds fewer of
     them than there are registrations<br>
     Controls stay on the crawl box, reachable over SSH only</p>
</main>
<script>
// Live update. Rather than re-implementing the card markup in the browser and
// keeping two renderers in step, this refetches the page and swaps <main> --
// the server stays the only thing that knows how a card looks.
//
// The script lives outside <main> deliberately, so swapping the contents does
// not duplicate or re-run it.
(function () {
  var PERIOD = 10000;
  var main = document.querySelector('main');
  var timer = null, failures = 0;

  async function tick() {
    // A tab left open overnight would otherwise keep billing function
    // invocations against a page nobody is looking at.
    if (document.hidden) return;
    try {
      var res = await fetch(location.pathname, {
        cache: 'no-store',
        headers: { 'x-live-refresh': '1' },
      });
      if (!res.ok) throw new Error(res.status);
      var doc = new DOMParser().parseFromString(await res.text(), 'text/html');
      var fresh = doc.querySelector('main');
      if (fresh) { main.innerHTML = fresh.innerHTML; failures = 0; }
    } catch (e) {
      // Back off rather than hammering a site that is having a bad time; the
      // "published Ns ago" line ages visibly in the meantime, so a stalled
      // updater shows up on the page instead of pretending everything is fine.
      if (++failures >= 3 && timer) { clearInterval(timer); timer = null; }
    }
  }

  function start() { if (!timer) timer = setInterval(tick, PERIOD); }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { if (timer) { clearInterval(timer); timer = null; } }
    else { tick(); start(); }
  });
  start();
})();
</script>`;
}

export const config = { path: ['/', '/summary.json'] };
