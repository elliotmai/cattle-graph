/**
 * The board's memory.
 *
 * Everything else here is instantaneous: the crawlers overwrite one status file
 * per association and the board renders whatever the last publish carried. That
 * answers "where is it now" and nothing else -- not whether the rate is holding,
 * not whether a crawler quietly halved its throughput overnight, not whether the
 * ETA is worth believing.
 *
 * So each publish appends a point here. Kept in the blob store rather than on the
 * crawl box because the box is the thing most likely to be rebuilt, and a history
 * that dies with the instance is not a history.
 *
 * Not a time-series database, deliberately. One blob, a bounded number of points,
 * read whole on every page view -- which is why the retention below matters more
 * than it looks.
 */

/**
 * Resolution by age. Recent progress is what a person actually looks at, so it
 * stays fine-grained; a month back, six-hourly is plenty to see the shape of.
 * Points are never averaged, only dropped -- every point that survives is a real
 * reading that really happened, so a stall shows as a flat run rather than being
 * smoothed into a gentle slope.
 */
export const RESOLUTIONS = [
  { maxAgeSec: 86400, stepSec: 600 },            // last day: every 10 minutes
  { maxAgeSec: 14 * 86400, stepSec: 3600 },      // last fortnight: hourly
  { maxAgeSec: Infinity, stepSec: 6 * 3600 },    // beyond: every 6 hours
];

/** Hard ceiling, so one blob read can never become unbounded. */
export const MAX_POINTS = 700;

/** The finest step, i.e. how often a publish is actually worth recording. */
const FINEST_STEP = RESOLUTIONS[0].stepSec;

const stepFor = (ageSec) =>
  (RESOLUTIONS.find((r) => ageSec <= r.maxAgeSec) ?? RESOLUTIONS[RESOLUTIONS.length - 1]).stepSec;

/**
 * A point is {t: epoch seconds, b: {ASSOC: [done, pending]}}.
 *
 * Arrays rather than objects for the pair: this is the one structure that grows
 * without bound, and `[103876,41000]` against `{"done":103876,"pending":41000}`
 * is the difference between a 25 KB blob and an 80 KB one on every page view.
 */
export function pointFrom(boards, nowMs) {
  const b = {};
  for (const board of boards ?? []) {
    if (!board?.association) continue;
    b[board.association] = [Number(board.done) || 0, Number(board.pending) || 0];
  }
  return { t: Math.round(nowMs / 1000), b };
}

/**
 * Add a reading, or decline to.
 *
 * Returns the same array identity when nothing changed, so the caller can skip
 * the write -- a publish every 30s against a 10-minute resolution means 19 of
 * every 20 calls have nothing to record.
 */
export function appendPoint(history, boards, nowMs) {
  const points = Array.isArray(history?.points) ? history.points : [];
  const point = pointFrom(boards, nowMs);
  if (!Object.keys(point.b).length) return { points, changed: false };

  const last = points[points.length - 1];
  if (last && point.t - last.t < FINEST_STEP) return { points, changed: false };
  // A clock that has gone backwards (or a replayed publish) must not put the
  // series out of order; drop the reading rather than corrupt the shape.
  if (last && point.t <= last.t) return { points, changed: false };

  return { points: compact([...points, point], nowMs), changed: true };
}

/**
 * Thin older points down to the resolution their age allows.
 *
 * Walks newest first, keeping one point per bucket. The newest point in a bucket
 * wins, so the right-hand end of the chart is always the latest reading rather
 * than something up to ten minutes stale.
 */
export function compact(points, nowMs) {
  const now = Math.round(nowMs / 1000);
  const kept = [];
  let lastBucket = null;

  for (let i = points.length - 1; i >= 0; i--) {
    const p = points[i];
    const step = stepFor(Math.max(0, now - p.t));
    const bucket = `${step}:${Math.floor(p.t / step)}`;
    if (bucket === lastBucket) continue;
    lastBucket = bucket;
    kept.push(p);
  }

  kept.reverse();
  return kept.length > MAX_POINTS ? kept.slice(kept.length - MAX_POINTS) : kept;
}

/**
 * History as plottable series: one per association plus an "overall".
 *
 * Overall is summed per point rather than from the association totals, so a
 * point recorded while one crawler was restarting cannot show up as a cliff in
 * the total -- an association missing from a point simply is not counted at that
 * instant, in either the parts or the sum.
 */
export function toSeries(history) {
  const points = Array.isArray(history?.points) ? history.points : [];
  const codes = [...new Set(points.flatMap((p) => Object.keys(p.b ?? {})))].sort();

  const series = {};
  for (const code of codes) {
    series[code] = points
      .filter((p) => p.b?.[code])
      .map((p) => ({ t: p.t, done: p.b[code][0], pending: p.b[code][1] }));
  }

  series.overall = points.map((p) => {
    const vals = Object.values(p.b ?? {});
    return {
      t: p.t,
      done: vals.reduce((s, v) => s + (v[0] || 0), 0),
      pending: vals.reduce((s, v) => s + (v[1] || 0), 0),
    };
  }).filter((p) => p.done || p.pending);

  return series;
}
