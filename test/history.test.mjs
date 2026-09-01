// The history blob is the one structure here that grows without bound and is
// read whole on every page view, so the retention rules are worth pinning down.
//
//   node --test test/history.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { appendPoint, compact, isDue, mergeBoards, toSeries, MAX_POINTS }
  from '../netlify/lib/history.mjs';

const boards = (done, pending) => [
  { association: 'CHIA', done, pending },
  { association: 'MAINE', done: done - 2000, pending: pending + 1000 },
];
const HOUR = 3600e3;

test('a publish inside the finest step records nothing', () => {
  const t0 = Date.parse('2026-08-31T12:00:00Z');
  const first = appendPoint(null, boards(100, 50), t0);
  assert.equal(first.changed, true);
  assert.equal(first.points.length, 1);

  // The box publishes every 30s against a 10-minute resolution.
  const soon = appendPoint(first, boards(102, 48), t0 + 30e3);
  assert.equal(soon.changed, false, 'must not write on every publish');
  assert.equal(soon.points.length, 1);

  const later = appendPoint(first, boards(140, 10), t0 + 11 * 60e3);
  assert.equal(later.changed, true);
  assert.equal(later.points.length, 2);
});

test('a clock that jumps backwards cannot disorder the series', () => {
  const t0 = Date.parse('2026-08-31T12:00:00Z');
  const one = appendPoint(null, boards(100, 50), t0);
  const back = appendPoint(one, boards(90, 60), t0 - HOUR);
  assert.equal(back.changed, false);
  assert.equal(back.points.length, 1);
});

test('a publish with no boards is not recorded as zeroes', () => {
  // A crawler restarting reports nothing for a moment; a zero point would draw
  // a cliff in the chart that never happened.
  const t0 = Date.parse('2026-08-31T12:00:00Z');
  assert.equal(appendPoint(null, [], t0).changed, false);
  assert.equal(appendPoint(null, undefined, t0).changed, false);
});

test('older points thin out to the resolution their age allows', () => {
  const now = Date.parse('2026-08-31T12:00:00Z');
  // Ten days of 10-minute readings: 1,440 points before compaction.
  const points = [];
  for (let t = now - 10 * 24 * HOUR; t <= now; t += 10 * 60e3) {
    points.push({ t: Math.round(t / 1000), b: { CHIA: [1, 2] } });
  }
  const kept = compact(points, now);

  // 144 ten-minute points for the last day + 24 hourly for each of the 9 days
  // before it, so a shade over 360 out of 1,441.
  assert.ok(kept.length < points.length / 3, `thinned to ${kept.length} of ${points.length}`);
  assert.ok(kept.length > 300, `but not over-thinned: ${kept.length}`);
  assert.ok(kept.length <= MAX_POINTS);

  // Newest stays newest, oldest stays oldest, order preserved throughout.
  assert.equal(kept[kept.length - 1].t, points[points.length - 1].t);
  for (let i = 1; i < kept.length; i++) {
    assert.ok(kept[i].t > kept[i - 1].t, 'points must stay in ascending time order');
  }

  // The last day keeps its 10-minute detail; the week before it goes hourly.
  const nowSec = now / 1000;
  const recent = kept.filter((p) => nowSec - p.t <= 86400);
  const older = kept.filter((p) => nowSec - p.t > 86400 && nowSec - p.t <= 14 * 86400);
  assert.ok(recent.length > 100, `last day should stay fine-grained, got ${recent.length}`);
  assert.ok(older.length < 24 * 10, `older points should be hourly at most, got ${older.length}`);
});

test('the point cap holds even against a long flat history', () => {
  const now = Date.parse('2026-08-31T12:00:00Z');
  const points = [];
  for (let i = 0; i < 5000; i++) {
    points.push({ t: Math.round(now / 1000) - (5000 - i) * 6 * 3600, b: { CHIA: [i, 1] } });
  }
  const kept = compact(points, now);
  assert.ok(kept.length <= MAX_POINTS, `capped at ${MAX_POINTS}, got ${kept.length}`);
  assert.equal(kept[kept.length - 1].b.CHIA[0], 4999, 'the cap drops the oldest, never the newest');
});

test('series come out per association plus a summed overall', () => {
  const t = 1756_000_000;
  const history = { points: [
    { t, b: { CHIA: [100, 50], MAINE: [80, 70] } },
    { t: t + 600, b: { CHIA: [110, 40], MAINE: [85, 65] } },
  ] };
  const s = toSeries(history);
  assert.deepEqual(Object.keys(s).sort(), ['CHIA', 'MAINE', 'overall']);
  assert.deepEqual(s.CHIA.map((p) => p.done), [100, 110]);
  assert.deepEqual(s.overall.map((p) => p.done), [180, 195]);
  assert.deepEqual(s.overall.map((p) => p.pending), [120, 105]);
});

test('an association absent from a point is skipped, not counted as zero', () => {
  // MAINE restarting must not read as "MAINE dropped to nothing" in its own
  // series, nor drag the overall down for that instant. Summing only what a
  // point holds did exactly that: 180 -> 110 is a cliff the crawl never took.
  const t = 1756_000_000;
  const s = toSeries({ points: [
    { t, b: { CHIA: [100, 50], MAINE: [80, 70] } },
    { t: t + 600, b: { CHIA: [110, 40] } },
  ] });
  assert.equal(s.MAINE.length, 1, 'MAINE has one reading, not a zero second one');
  assert.deepEqual(s.overall.map((p) => p.done), [180, 190],
    'MAINE keeps its last real reading in the total rather than vanishing');
  assert.deepEqual(s.overall.map((p) => p.pending), [120, 110]);
});

test('two boxes reporting one breed do not saw between their counts', () => {
  // The history is one blob shared by every publisher, so whichever box POSTs
  // first after the ten-minute boundary used to write the whole point. With a
  // breed both boxes report, that alternated between two frontiers and drew a
  // sawtooth on a count that only ever climbs. Merging first is what fixes it.
  const t0 = Date.parse('2026-08-31T12:00:00Z');
  const lightsail = (done, at) => ({
    source: 'lightsail',
    boards: [{ association: 'CHIA', done, pending: 41000, updated_at: at }],
  });
  const windows = (done, at) => ({
    source: 'windows',
    boards: [{ association: 'CHIA', done, pending: 900, updated_at: at }],
  });

  let history = null;
  const seen = [];
  for (let i = 0; i < 4; i++) {
    const at = new Date(t0 + i * 11 * 60e3).toISOString();
    // The laptop's crawl is long dead: its reading never moves and its
    // updated_at never advances, so it must never win.
    const entries = i % 2
      ? [windows(20000, '2026-08-20T09:00:00Z'), lightsail(128900 + i, at)]
      : [lightsail(128900 + i, at), windows(20000, '2026-08-20T09:00:00Z')];
    history = appendPoint(history, mergeBoards(entries), t0 + i * 11 * 60e3);
    seen.push(history.points[history.points.length - 1].b.CHIA[0]);
  }
  assert.deepEqual(seen, [128900, 128901, 128902, 128903]);
});

test('the newest reading wins whichever box lists it, and whichever order', () => {
  const boards = (done, at) => [{ association: 'CHIA', done, updated_at: at }];
  const stale = { source: 'windows', boards: boards(20000, '2026-08-20T09:00:00Z') };
  const fresh = { source: 'lightsail', boards: boards(128926, '2026-09-01T21:00:00Z') };

  for (const entries of [[stale, fresh], [fresh, stale]]) {
    const merged = mergeBoards(entries);
    assert.equal(merged.length, 1, 'one board per association');
    assert.equal(merged[0].done, 128926);
    assert.equal(merged[0].source, 'lightsail', 'the winner names the box it came from');
  }

  // Two status files for one breed on a single box collapse the same way.
  assert.deepEqual(
    mergeBoards([{ source: 'lightsail', boards: [...boards(20000, '2026-08-20T09:00:00Z'),
                                                 ...boards(128926, '2026-09-01T21:00:00Z')] }])
      .map((b) => b.done),
    [128926]);

  assert.deepEqual(mergeBoards(null), []);
  assert.deepEqual(mergeBoards([{ boards: [{ done: 1 }] }]), [], 'a board with no association');
});

test('isDue answers what appendPoint would do, without the boards', () => {
  const t0 = Date.parse('2026-08-31T12:00:00Z');
  assert.equal(isDue(null, t0), true, 'an empty history always wants a reading');

  const one = appendPoint(null, boards(100, 50), t0);
  assert.equal(isDue(one, t0 + 30e3), false);
  assert.equal(appendPoint(one, boards(101, 49), t0 + 30e3).changed, false);
  assert.equal(isDue(one, t0 + 11 * 60e3), true);
  assert.equal(appendPoint(one, boards(140, 10), t0 + 11 * 60e3).changed, true);
});

test('a corrupt or empty blob reads as an empty history', () => {
  for (const bad of [null, undefined, {}, { points: 'nope' }]) {
    assert.deepEqual(toSeries(bad), { overall: [] });
    assert.equal(appendPoint(bad, [], Date.now()).changed, false);
  }
});
