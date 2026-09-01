// What the board makes of a payload: the crawl numbers and the graph numbers
// come from different places and mean different things, and the page is the
// only thing that puts them side by side.
//
//   node --test test/board.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { summarize, renderHtml } from '../netlify/functions/dashboard.mjs';

const ago = (s) => new Date(Date.now() - s * 1000).toISOString();

const entry = (over = {}) => ({
  source: 'lightsail',
  publishedAt: new Date().toISOString(),
  boards: [{
    association: 'CHIA', state: 'running', done: 103876, pending: 41000,
    failed: 12, rate_per_min: 3.5, pct: 71.7, updated_at: ago(14),
  }],
  graph: {
    ok: true, read_at: ago(6), animals: 268112, registrations: 300503,
    multi_assoc: 4120, defects: { Free: 18422, Carrier: 611 },
    by_association: { CHIA: { registrations: 101940, behind: 2800 } },
  },
  ...over,
});

test('graph counts are joined onto the association they belong to', () => {
  const s = summarize([entry()]);
  assert.equal(s.boards[0].graphed, 999999); // TEMPORARY: deliberate break to prove CI goes red
  assert.equal(s.boards[0].behind, 2800);
  assert.equal(s.behindTotal, 2800);
  assert.equal(s.loading, true);
});

test('a graph reading that has stopped advancing means loading has stopped', () => {
  // The reading comes from the same process as the loader, so a stale reading
  // is how a stopped loader becomes visible at all -- the crawl counts keep
  // climbing either way.
  const s = summarize([entry({ graph: { ...entry().graph, read_at: ago(3600) } })]);
  assert.equal(s.loading, false);
  assert.match(renderHtml(s), /the loader has stopped/);
  // ...and the crawl side is untouched by it.
  assert.equal(s.boards[0].stale, false);
  assert.equal(s.totals.done, 103876);
});

test('an unreachable graph is called out as such, not as a stopped loader', () => {
  const s = summarize([entry({ graph: { ...entry().graph, ok: false } })]);
  assert.equal(s.loading, false);
  assert.match(renderHtml(s), /Neo4j is not answering/);
});

test('a payload with no graph at all still renders the crawl', () => {
  const s = summarize([entry({ graph: null })]);
  assert.equal(s.graph, null);
  assert.equal(s.boards[0].graphed, null);
  const html = renderHtml(s);
  assert.match(html, /has not published a\s+graph reading/);
  assert.match(html, /103,876/);
});

test('the newest graph reading wins when two boxes publish', () => {
  const older = entry({ source: 'laptop', publishedAt: ago(600) });
  older.graph = { ...older.graph, animals: 1 };
  const s = summarize([older, entry()]);
  assert.equal(s.graph.animals, 268112);
});

const history = (n = 40, stepSec = 600) => ({
  points: Array.from({ length: n }, (_, i) => {
    const t = Math.floor(Date.now() / 1000) - (n - 1 - i) * stepSec;
    return { t, b: { CHIA: [100000 + i * 50, 250000 - i * 50] } };
  }),
});

test('history becomes per-association and overall series', () => {
  const s = summarize([entry()], history());
  assert.equal(s.series.length, 40);
  assert.equal(s.boards[0].series.length, 40);
  // Overall is summed per point, not from the association totals.
  assert.equal(s.series[0].done, 100000);
  assert.equal(s.series[39].done, 101950);
});

test('the charts are faceted, one scale per series, with a table twin', () => {
  const html = renderHtml(summarize([entry()], history()));
  // Two panels, not two lines on one plot: recorded near 100k and remaining
  // near 250k on a shared scale would be two flat lines a third apart.
  const panels = html.match(/svg class="trend"/g) ?? [];
  assert.equal(panels.length, 2);
  assert.match(html, /data-key="done"/);
  assert.match(html, /data-key="pending"/);
  // Tooltips enhance, never gate: the numbers are reachable without a pointer.
  assert.match(html, /Show the numbers/);
  assert.match(html, /<th>Recorded<\/th>/);
  // And each card carries its own pair of sparklines.
  assert.equal((html.match(/class="spark"/g) ?? []).length, 2);
});

test('one reading is not a trend', () => {
  const html = renderHtml(summarize([entry()], history(1)));
  assert.doesNotMatch(html, /svg class="trend"/);
  assert.match(html, /Progress over time appears here/);
});

test('the page names both totals without reusing one word for both', () => {
  const html = renderHtml(summarize([entry()]));
  // Records crawled and distinct animals in the graph are different facts;
  // labelling both "Animals" is what made this confusing in the first place.
  assert.match(html, /<dt>Recorded<\/dt><dd>103,876/);   // crawled
  assert.match(html, /<dt>Animals<\/dt><dd>268,112/);    // deduplicated nodes
  assert.match(html, /<dt>Registrations<\/dt><dd>300,503/);
  assert.match(html, /<dt>In graph<\/dt><dd>101,940/);
  assert.match(html, /Carrier<b>611<\/b>/);
});
