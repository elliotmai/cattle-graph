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
  assert.equal(s.boards[0].graphed, 101940);
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
