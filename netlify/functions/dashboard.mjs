import { getStore } from '@netlify/blobs';
import { mergeBoards, toSeries } from '../lib/history.mjs';
import { sparkline, panel, geometry } from '../lib/chart.mjs';

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
  // A board with no history is the normal state for the first ten minutes
  // after a deploy, so a missing blob is not an error.
  const history = await store.get('history', { type: 'json' }).catch(() => null);

  const summary = summarize(entries, history);

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
export function summarize(entries, history = null) {
  const now = Date.now();
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
  }

  const inGraph = graph?.by_association ?? {};
  const series = toSeries(history);

  // Deduplicated by mergeBoards rather than here, so the page and the history
  // resolve "two boxes reported this breed" the same way; they used to differ,
  // and the card could disagree with the end of its own line.
  const boards = mergeBoards(entries).map((b) => {
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
      series: series[b.association] ?? [],
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
    series: series.overall ?? [],
    graph,
    graphAgeSec,
    loading,
    behindTotal,
    disk: entries.find((e) => e.disk)?.disk ?? null,
  };
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const ago = (s) => s < 10 ? 'just now'
  : s < 90 ? `${s}s ago`
  : s < 5400 ? `${Math.round(s / 60)}m ago`
  : s < 172800 ? `${Math.round(s / 3600)}h ago`
  : `${Math.round(s / 86400)}d ago`;

/**
 * One word for "this number was refreshed", everywhere on the page. It used to
 * be "read" in one place, "published" in another and "reported" in a third,
 * which made three facts out of one.
 */
const updated = (s) => s === null ? 'Never updated' : `Updated ${ago(s)}`;

const num = (n) => (Number(n) || 0).toLocaleString('en-US');
const pct = (n) => (Number(n) || 0).toFixed(1);
const unit = (u) => `<span class="unit">${u}</span>`;

/**
 * A span of days, said the way a person would say it. Every "how long until
 * this is done" on the page goes through here, so the totals and the cards can
 * never disagree about units again ("4.8 wks" up top, "33.6d" on a card).
 */
const dur = (days) => {
  if (days === null || !isFinite(days)) return '&mdash;';
  if (days < 1) return `${Math.max(1, Math.round(days * 24))}${unit(' hr')}`;
  if (days < 14) { const d = Math.round(days); return `${d}${unit(d === 1 ? ' day' : ' days')}`; }
  if (days < 70) return `${(days / 7).toFixed(1)}${unit(' wks')}`;
  return `${(days / 30.44).toFixed(1)}${unit(' mo')}`;
};

/**
 * Three states, three colours, and the colour means the same thing wherever it
 * appears: green is moving, neutral is resting, red needs a person. Idle used to
 * wear the green "good" pill, which said all was well about a crawler doing
 * nothing.
 */
function stateOf(b) {
  if (b.stale) return { key: 'stalled', label: 'Stalled' };
  const raw = String(b.state ?? 'running').toLowerCase();
  if (raw === 'running') return { key: 'running', label: 'Running' };
  return { key: 'idle', label: raw.charAt(0).toUpperCase() + raw.slice(1) };
}

/**
 * The overall chart and the table that carries the same numbers without a
 * pointer.
 *
 * Two points is the minimum that can be a line; below that the panel says what
 * it is waiting for rather than drawing a dot and calling it a trend. That is
 * the normal state for the first ten minutes after a deploy.
 */
function trend(s) {
  const pts = s.series ?? [];
  if (pts.length < 2) {
    return `<p class="panel note">Progress over time appears here once the
      board has a few readings &mdash; one every ten minutes.</p>`;
  }

  const spanHours = (pts[pts.length - 1].t - pts[0].t) / 3600;
  const span = spanHours < 48 ? `${Math.max(1, Math.round(spanHours))} hours`
    : `${Math.round(spanHours / 24)} days`;

  // Newest first: the recent end is what a reader checks.
  const rows = [...pts].reverse().slice(0, 24).map((p) => `<tr>
      <td>${esc(when(p.t))}</td><td>${num(p.done)}</td><td>${num(p.pending)}</td></tr>`).join('');

  // Two facets, each one series on its own scale. Each is titled, so neither
  // needs a legend box of its own -- a box with one swatch restates the title.
  return `<figure class="panel chart">
    <figcaption>All registries, last ${esc(span)}</figcaption>
    <div class="plot">
      <p class="label"><i class="key rec"></i>Recorded</p>
      ${panel(pts, 'done', { label: 'Recorded' })}
      <p class="label"><i class="key rem"></i>Remaining</p>
      ${panel(pts, 'pending', { label: 'Remaining', xAxis: true })}
      <div class="tip" hidden></div>
      <script type="application/json" class="chart-geom">${JSON.stringify(geometry(pts))}</script>
    </div>
    <details class="numbers">
      <summary>Show the numbers</summary>
      <div class="tscroll"><table>
        <thead><tr><th>Time</th><th>Recorded</th><th>Remaining</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </details>
  </figure>`;
}

const when = (t) => new Date(t * 1000).toLocaleString('en-US', {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
});

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
  return `<div class="tests">
    <h3 class="label">Genetic tests</h3>
    <ul class="chips">${entries.map(([status, n]) =>
      `<li class="${status === 'Carrier' ? 'is-alert' : ''}">${esc(status)}<b>${num(n)}</b></li>`,
    ).join('')}</ul>
  </div>`;
}

/**
 * One breed. The anatomy is fixed -- every card has the same rows in the same
 * order, with a dash where a number is missing -- so cards side by side line up
 * row for row and a reader learns where to look once.
 */
function card(b) {
  const st = stateOf(b);
  const p = Number(b.pct) || 0;
  const failed = Number(b.failed) || 0;
  const rate = Number(b.rate_per_min) || 0;
  return `
    <article class="panel card is-${st.key}">
      <header class="card-head">
        <h2>${esc(b.association)}</h2>
        <span class="status"><i class="dot" aria-hidden="true"></i>${st.label}</span>
      </header>

      <div class="bar" role="img" aria-label="${pct(p)} percent complete">
        <span style="width:${Math.min(100, p)}%"></span>
      </div>
      <p class="meta"><b>${pct(p)}%</b> of known herd</p>

      <dl class="figures">
        <div><dt><i class="key rec"></i>Recorded</dt><dd>${num(b.done)}</dd>
          ${sparkline(b.series, 'done')}</div>
        <div><dt><i class="key rem"></i>Remaining</dt><dd>${num(b.pending)}</dd>
          ${sparkline(b.series, 'pending')}</div>
        <div><dt>Rate</dt><dd>${rate.toFixed(1)}${unit('/min')}</dd></div>
        <div><dt>Time left</dt><dd>${dur(b.etaDays)}</dd></div>
        <div><dt>In graph</dt><dd>${b.graphed !== null ? num(b.graphed) : '&mdash;'}</dd></div>
        <div><dt>To load</dt><dd>${b.behind !== null ? num(b.behind) : '&mdash;'}</dd></div>
      </dl>

      ${b.current?.reg ? `<p class="now">
        <span class="now-txt"><span class="reg">${esc(b.current.reg)}</span>
        <span class="nm">${esc(b.current.name || '')}</span></span>
      </p>` : `<p class="now is-empty">No animal in progress</p>`}

      <footer>
        <span>${esc(updated(b.ageSec))}</span>
        ${failed ? `<span class="alert">${num(failed)} failed</span>` : ''}
      </footer>
    </article>`;
}

export function renderHtml(s) {
  const pctTotal = s.totals.done + s.totals.pending > 0
    ? (s.totals.done / (s.totals.done + s.totals.pending) * 100) : 0;
  const n = s.boards.length;
  const cards = s.boards.map(card).join('');

  return `<!doctype html>
<html lang="en">
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
     Ranch ledger. Warm paper, saddle tan, pasture green, barn red, and a
     slab serif. This is glanced at on a phone in a barn, so the numbers
     carry the page and the theme stays in the palette and the type.

     Two rules hold the whole page together:
       - Type: numbers and names wear the slab; labels and prose wear the
         sans, in sentence case. Sizes come only from the --t-* scale.
       - Colour: green is moving, neutral is resting, red needs a person.
         Tan is the one accent and goes on exactly one figure: time left.

     Mobile first -- every base rule targets a narrow screen, and the
     min-width blocks at the end are the only widening.
     -------------------------------------------------------------------- */
  :root{
    --paper:#f7f1e6; --card:#fffdf8; --ink:#241c12; --dim:#73624d;
    --rule:#e2d6c0; --rule-soft:#efe6d5;
    --tan:#a06d31; --wheat:#d8b56a;
    --pasture:#457549; --barn:#a33a2c;
    /* Chart series. Harvest amber and denim -- warm/cool, so they separate
       under protan and deutan where the board's green and tan do not (that
       pair measures 3.6 dE simulated, 13.5 unsimulated: indistinguishable).
       Both steps pass the lightness band, chroma floor, CVD separation and
       contrast checks against the card surface. */
    --rec:#b0700f; --rem:#2f6ea8;
    --shadow:0 1px 2px rgba(60,42,20,.05), 0 6px 16px -10px rgba(60,42,20,.22);

    /* Type scale: a major third (1.25) from a 16px body. */
    --t-xs:.75rem; --t-sm:.875rem; --t-md:1rem; --t-lg:1.25rem;
    --t-xl:1.625rem; --t-2xl:2rem;
    --slab:Bitter,Georgia,"Iowan Old Style",serif;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;

    /* Spacing: a 4px base, doubling past 1rem. */
    --s1:.25rem; --s2:.5rem; --s3:.75rem; --s4:1rem; --s5:1.5rem; --s6:2rem; --s7:3rem;

    /* Two radii: panels, and the small things that sit inside them. */
    --r-lg:.75rem; --r-sm:.375rem;
  }
  @media (prefers-color-scheme:dark){
    :root{
      --paper:#17130d; --card:#211a12; --ink:#f2e9da; --dim:#a8967d;
      --rule:#3a2f20; --rule-soft:#2b2318;
      --tan:#d29d5a; --wheat:#e0bd76;
      --pasture:#7fb882; --barn:#e8806f;
      /* Re-stepped for the dark surface rather than flipped: the light steps
         sit outside the dark lightness band. */
      --rec:#bf8526; --rem:#4a8ec9;
      --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 18px -12px rgba(0,0,0,.7);
    }
  }

  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0; padding:var(--s5) var(--s4) var(--s7);
    background:var(--paper); color:var(--ink);
    font:400 var(--t-md)/1.5 var(--sans);
  }
  main{max-width:64rem; margin:0 auto}
  h1,h2,h3,p,dl,dd,figure,ul{margin:0}
  :focus-visible{outline:2px solid var(--tan); outline-offset:3px; border-radius:var(--r-sm)}

  /* ---------- shared atoms ---------- */
  /* Every small caption on the page -- totals, card figures, chart facets,
     section timestamps -- is this one style. */
  .label, .totals dt, .figures dt{color:var(--dim); font-size:var(--t-xs); font-weight:500; line-height:1.3}
  .unit{font-family:var(--sans); font-weight:400; font-size:var(--t-sm); color:var(--dim)}
  .alert{color:var(--barn)}
  /* The line-key is how a number ties to a line. Text never wears the series
     colour -- a mark beside it carries the identity instead. */
  .key{
    display:inline-block; width:.75rem; height:2px; border-radius:1px;
    margin-right:var(--s2); vertical-align:middle;
  }
  .key.rec{background:var(--rec)} .key.rem{background:var(--rem)}

  /* One surface for everything that holds numbers. */
  .panel{
    background:var(--card); border:1px solid var(--rule); border-radius:var(--r-lg);
    box-shadow:var(--shadow); padding:var(--s4);
  }
  .note{color:var(--dim); font-size:var(--t-sm)}

  /* ---------- masthead ---------- */
  .masthead{margin:0 0 var(--s5)}
  .masthead h1{
    font-family:var(--slab); font-weight:700; font-size:var(--t-2xl);
    line-height:1.1; letter-spacing:-.01em;
  }
  .masthead p{margin-top:var(--s1); color:var(--dim); font-size:var(--t-sm)}
  /* Ledger rule: a heavy line over a hair, the way a stock certificate or a
     feed-store letterhead separates the name from the business. This is the
     page's one ornament. */
  .masthead::after{
    content:""; display:block; margin-top:var(--s4); height:4px;
    border-top:2px solid var(--tan); border-bottom:1px solid var(--rule);
  }

  .banner{
    background:var(--barn); color:#fff; padding:var(--s3) var(--s4);
    border-radius:var(--r-lg); margin:0 0 var(--s4);
    font-size:var(--t-sm); box-shadow:var(--shadow);
  }
  .banner strong{font-weight:700}
  @media (prefers-color-scheme:dark){ .banner{color:#1c0d0a} }

  /* ---------- section headers ---------- */
  /* Two sources of truth on one page -- what has been crawled, and what is
     actually in Neo4j. They are different numbers and get confused if the
     page does not say plainly which is which. */
  .sect{
    display:flex; align-items:baseline; justify-content:space-between; gap:var(--s3);
    margin:var(--s7) 0 var(--s3);
  }
  .masthead + .sect, .banner + .sect{margin-top:0}
  .sect h2{font-family:var(--slab); font-weight:700; font-size:var(--t-lg); line-height:1.2}
  .sect .label{white-space:nowrap}

  .stack{display:grid; gap:var(--s4)}

  /* ---------- totals ---------- */
  /* auto-fill, not auto-fit: both totals panels lay out on the same column
     grid, so "Animals" sits directly under "Recorded" on a wide screen. */
  .totals{
    display:grid; gap:var(--s4) var(--s3);
    grid-template-columns:repeat(auto-fill,minmax(min(100%,8.75rem),1fr));
  }
  .totals dd{
    margin-top:var(--s1); font-family:var(--slab); font-weight:600;
    font-size:var(--t-xl); line-height:1.1; font-variant-numeric:tabular-nums;
  }
  /* "How long until this is over" is the question the page gets asked most,
     so it is the one figure given the accent colour. */
  .totals .eta dd{color:var(--tan)}

  .tests{margin-top:var(--s4); padding-top:var(--s4); border-top:1px solid var(--rule-soft)}
  .chips{display:flex; flex-wrap:wrap; gap:var(--s2); margin-top:var(--s2); padding:0; list-style:none}
  .chips li{
    border:1px solid var(--rule); border-radius:var(--r-sm); padding:var(--s1) var(--s2);
    font-size:var(--t-xs); color:var(--dim); white-space:nowrap;
  }
  .chips b{color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; margin-left:var(--s1)}
  .chips .is-alert, .chips .is-alert b{border-color:var(--barn); color:var(--barn)}

  /* ---------- cards ---------- */
  .grid{display:grid; gap:var(--s4); grid-template-columns:1fr}
  .card{
    display:flex; flex-direction:column;
    /* The stripe is the card's state, read from across the room: the same
       green, neutral or red as the status beside the name. */
    border-left:4px solid var(--st);
    --st:var(--pasture);
  }
  .card.is-idle{--st:var(--rule)}
  .card.is-stalled{--st:var(--barn)}
  .card-head{
    display:flex; align-items:center; justify-content:space-between;
    gap:var(--s3); margin-bottom:var(--s3);
  }
  .card-head h2{font-family:var(--slab); font-weight:700; font-size:var(--t-lg); line-height:1.2}
  .status{
    display:inline-flex; align-items:center; gap:var(--s2);
    font-size:var(--t-sm); font-weight:600; color:var(--pasture); white-space:nowrap;
  }
  .card.is-idle .status{color:var(--dim)}
  .card.is-stalled .status{color:var(--barn)}
  .dot{width:.5rem; height:.5rem; border-radius:50%; background:currentColor; flex:none}
  /* The only ambient motion on the page: a running crawler breathes. */
  .card.is-running .dot{animation:pulse 2.4s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  @media (prefers-reduced-motion:reduce){.card.is-running .dot{animation:none}}

  .bar{height:6px; background:var(--rule-soft); border-radius:3px; overflow:hidden}
  .bar span{
    display:block; height:100%; border-radius:3px;
    background:linear-gradient(90deg,var(--tan),var(--wheat));
  }
  .meta{margin:var(--s2) 0 var(--s4); font-size:var(--t-sm); color:var(--dim)}
  .meta b{color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums}

  .figures{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:var(--s4) var(--s4); margin-bottom:var(--s4)}
  .figures dd{
    margin-top:var(--s1); font-family:var(--slab); font-weight:600;
    font-size:var(--t-lg); line-height:1.2; font-variant-numeric:tabular-nums;
  }
  .spark{display:block; width:100%; height:auto; margin-top:var(--s1)}

  /* ---------- the animal on the stand right now ---------- */
  .now{
    margin-bottom:var(--s4); padding:var(--s2) var(--s3);
    background:var(--rule-soft); border-radius:var(--r-sm);
    font-size:var(--t-sm); min-width:0;
  }
  .now-txt{display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .now .reg{font-family:var(--mono); color:var(--dim); font-size:var(--t-xs); margin-right:var(--s1)}
  .now .nm{font-weight:600}
  .now.is-empty{color:var(--dim)}

  .card footer{
    margin-top:auto; padding-top:var(--s3); border-top:1px solid var(--rule-soft);
    display:flex; justify-content:space-between; gap:var(--s3);
    color:var(--dim); font-size:var(--t-xs);
  }
  .card footer .alert{font-weight:600}

  /* ---------- chart ---------- */
  .chart figcaption{color:var(--dim); font-size:var(--t-sm); margin-bottom:var(--s3)}
  .plot{position:relative}
  .plot .label{margin:var(--s2) 0 0}
  .trend{display:block; width:100%; height:auto; overflow:visible}
  /* SVG text is sized in viewBox units, so it grows and shrinks with the
     chart -- huge on a desktop, unreadable on a phone. The script sets --k to
     the chart's on-screen scale so the labels hold a constant size; without
     script they fall back to the drawn size. */
  .trend{--k:1}
  .trend .tick{fill:var(--dim); font-size:calc(11px / var(--k)); font-variant-numeric:tabular-nums}
  .trend .endlab{fill:var(--ink); font-size:calc(12px / var(--k)); font-weight:600;
    font-variant-numeric:tabular-nums}
  .trend:focus-visible{outline-offset:4px}

  /* Values lead, series names follow -- the reader already knows the series. */
  .tip{
    position:absolute; pointer-events:none; z-index:2; min-width:9rem;
    background:var(--card); border:1px solid var(--rule); border-radius:var(--r-sm);
    padding:var(--s2) var(--s3); box-shadow:var(--shadow); font-size:var(--t-xs);
    transform:translate(-50%,-105%);
  }
  .tip .tw{color:var(--dim); margin-bottom:var(--s1)}
  .tip .tr{display:flex; align-items:center; line-height:1.6}
  .tip .tv{font-weight:600; font-variant-numeric:tabular-nums; margin-left:auto; padding-left:var(--s3)}

  .numbers{margin-top:var(--s3)}
  .numbers summary{
    cursor:pointer; color:var(--dim); font-size:var(--t-sm);
    display:inline-flex; align-items:center; min-height:2.75rem;
  }
  .numbers summary:hover{color:var(--ink)}
  .tscroll{overflow-x:auto}
  .numbers table{border-collapse:collapse; width:100%; font-size:var(--t-sm)}
  .numbers th, .numbers td{
    text-align:right; padding:var(--s1) var(--s2); border-bottom:1px solid var(--rule-soft);
    font-variant-numeric:tabular-nums; white-space:nowrap;
  }
  .numbers th:first-child, .numbers td:first-child{text-align:left; padding-left:0}
  .numbers th:last-child, .numbers td:last-child{padding-right:0}
  .numbers th{color:var(--dim); font-weight:500; font-size:var(--t-xs)}

  .foot{
    margin-top:var(--s6); padding-top:var(--s4); border-top:1px solid var(--rule);
    color:var(--dim); font-size:var(--t-xs);
  }
  .foot p{max-width:38rem}
  .foot p + p{margin-top:var(--s1)}

  /* ---------- widening ---------- */
  @media (min-width:40rem){
    body{padding:var(--s6) var(--s5) var(--s7)}
    .panel{padding:var(--s5)}
    .masthead h1{font-size:2.5rem}
    .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  }
</style>
<main>
  <header class="masthead">
    <h1>Cattle Graph</h1>
    <p>Pedigree crawl progress across ${n || 'the'} breed ${n === 1 ? 'registry' : 'registries'}</p>
  </header>

  ${s.reporting ? '' : `<p class="banner"><strong>These numbers are not live.</strong>
     The crawl box last reported ${esc(ago(s.publishAgeSec))} &mdash; check the instance.</p>`}

  ${s.reporting && !s.loading ? `<p class="banner"><strong>Nothing is reaching Neo4j.</strong>
     ${esc(loadWarning(s))} The crawlers keep filling their files either way,
     so the counts below still rise.</p>` : ''}

  <header class="sect"><h2>The crawl</h2>
    <span class="label">${esc(updated(s.publishAgeSec))}</span></header>

  <div class="stack">
    <dl class="panel totals">
      <div><dt>Recorded</dt><dd>${num(s.totals.done)}</dd></div>
      <div><dt>Remaining</dt><dd>${num(s.totals.pending)}</dd></div>
      <div><dt>Complete</dt><dd>${pct(pctTotal)}%</dd></div>
      <div class="eta"><dt>Time left</dt><dd>${dur(s.etaDaysOverall)}</dd></div>
      ${s.totals.failed ? `<div><dt>Failed</dt><dd class="alert">${num(s.totals.failed)}</dd></div>` : ''}
      ${s.disk ? `<div><dt>Disk free</dt><dd>${esc(s.disk.free_gb)}${unit(' GB')}</dd></div>` : ''}
    </dl>

    ${trend(s)}

    ${cards ? `<div class="grid">${cards}</div>`
      : `<p class="panel note">No crawler has published yet. Cards appear here
          within a minute of the crawl box starting to publish.</p>`}
  </div>

  <header class="sect"><h2>The graph</h2>
    <span class="label">${esc(s.graph ? updated(s.graphAgeSec) : updated(null))}</span></header>

  ${s.graph ? `<section class="panel">
    <dl class="totals">
      <div><dt>Animals</dt><dd>${num(s.graph.animals)}</dd></div>
      <div><dt>Registrations</dt><dd>${num(s.graph.registrations)}</dd></div>
      <div><dt>In two registries</dt><dd>${num(s.graph.multi_assoc)}</dd></div>
      <div><dt>To load</dt><dd>${num(s.behindTotal)}</dd></div>
    </dl>
    ${defectRow(s.graph.defects)}
  </section>` : `<p class="panel note">The box has not published a
     graph reading. That is cattle-dashboard's job &mdash; check it is running.</p>`}

  <footer class="foot">
    <p>Animals are deduplicated across registries, so the graph holds fewer
      animals than registrations.</p>
    <p>Crawl controls stay on the crawl box, reachable over SSH only.</p>
  </footer>
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

  // ---- hover layer -------------------------------------------------------
  // The crosshair finds the X: a reader aims at a time, never at a 2px line,
  // and one tooltip lists both series so the pointer never has to land on a
  // stroke. Re-attached after every refresh, because the swap below replaces
  // the chart node this closed over.
  function initChart() {
    var plot = main.querySelector('.plot');
    if (!plot) return;
    var svgs = [].slice.call(plot.querySelectorAll('svg.trend'));
    var geomEl = plot.querySelector('.chart-geom');
    var tip = plot.querySelector('.tip');
    if (!svgs.length || !geomEl || !tip) return;

    var g = JSON.parse(geomEl.textContent);

    // Hold the chart's text at a constant on-screen size (see .trend in the
    // stylesheet): tell CSS how much the viewBox is being scaled by.
    function rescale() {
      svgs.forEach(function (s) {
        var wpx = s.getBoundingClientRect().width;
        // Bounded so a very narrow phone cannot push labels past the margins.
        if (wpx) s.style.setProperty('--k', Math.min(2, Math.max(0.6, wpx / g.w)).toFixed(3));
      });
    }
    rescale();
    if (!window.__chartResize) {
      window.__chartResize = true;
      var queued = false;
      window.addEventListener('resize', function () {
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; initChart.rescale && initChart.rescale(); });
      });
    }
    initChart.rescale = rescale;
    // The facets share an x domain, so one pointer drives both crosshairs and
    // a single readout lists both series -- the reader never has to hover the
    // right panel to get the other number.
    var hairs = svgs.map(function (s) { return s.querySelector('.crosshair'); });
    var idx = -1;

    function show(i) {
      if (i < 0 || i >= g.points.length) return;
      idx = i;
      var p = g.points[i];
      var x = g.plot[0] + ((p[0] - g.t0) / Math.max(1, g.t1 - g.t0)) * (g.plot[1] - g.plot[0]);
      hairs.forEach(function (hair) {
        hair.setAttribute('x1', x); hair.setAttribute('x2', x);
        hair.setAttribute('opacity', '1');
      });

      // textContent throughout: these are numbers today, but a tooltip built
      // by string concatenation is a habit that goes wrong the moment a label
      // comes from data.
      tip.textContent = '';
      var when = document.createElement('div');
      when.className = 'tw';
      when.textContent = new Date(p[0] * 1000).toLocaleString();
      tip.appendChild(when);
      [['rec', 'Recorded', p[1]], ['rem', 'Remaining', p[2]]].forEach(function (row) {
        var r = document.createElement('div');
        r.className = 'tr';
        var k = document.createElement('i');
        k.className = 'key ' + row[0];
        var name = document.createElement('span');
        name.textContent = row[1];
        var val = document.createElement('span');
        val.className = 'tv';
        val.textContent = row[2].toLocaleString('en-US');
        r.appendChild(k); r.appendChild(name); r.appendChild(val);
        tip.appendChild(r);
      });

      var rect = svgs[0].getBoundingClientRect();
      var box = plot.getBoundingClientRect();
      tip.style.left = (x / g.w) * rect.width + 'px';
      tip.style.top = (rect.bottom - box.top) + 'px';
      tip.hidden = false;
    }

    function hide() {
      tip.hidden = true;
      hairs.forEach(function (h) { h.setAttribute('opacity', '0'); });
      idx = -1;
    }

    function nearest(clientX) {
      var rect = svgs[0].getBoundingClientRect();
      var xUser = ((clientX - rect.left) / rect.width) * g.w;
      var frac = (xUser - g.plot[0]) / (g.plot[1] - g.plot[0]);
      var t = g.t0 + frac * (g.t1 - g.t0);
      var best = 0, bestD = Infinity;
      for (var i = 0; i < g.points.length; i++) {
        var d = Math.abs(g.points[i][0] - t);
        if (d < bestD) { bestD = d; best = i; }
      }
      return best;
    }

    svgs.forEach(function (svg) {
      svg.addEventListener('pointermove', function (e) { show(nearest(e.clientX)); });
      svg.addEventListener('pointerleave', hide);
    });

    // Keyboard gets the same readout as the pointer, on one focus stop rather
    // than one per facet -- they show the same instant either way.
    var svg = svgs[0];
    svg.setAttribute('tabindex', '0');
    svg.addEventListener('focus', function () { show(g.points.length - 1); });
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { show(Math.max(0, (idx < 0 ? g.points.length - 1 : idx) - 1)); e.preventDefault(); }
      else if (e.key === 'ArrowRight') { show(Math.min(g.points.length - 1, idx + 1)); e.preventDefault(); }
      else if (e.key === 'Escape') { hide(); }
    });
  }

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
      if (fresh) {
        // Don't yank the table open under someone reading it, and don't
        // reset the chart while they are hovering it.
        var open = main.querySelector('.numbers[open]');
        var hovering = main.querySelector('.tip:not([hidden])');
        if (hovering) return;
        main.innerHTML = fresh.innerHTML;
        if (open) { var d = main.querySelector('.numbers'); if (d) d.open = true; }
        initChart();
        failures = 0;
      }
    } catch (e) {
      // Back off rather than hammering a site that is having a bad time; the
      // "published Ns ago" line ages visibly in the meantime, so a stalled
      // updater shows up on the page instead of pretending everything is fine.
      if (++failures >= 3 && timer) { clearInterval(timer); timer = null; }
    }
  }

  initChart();

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
