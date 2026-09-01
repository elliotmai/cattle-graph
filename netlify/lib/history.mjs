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
 * One board per association out of any number of publishers, newest reading
 * winning -- the same rule the board itself renders by.
 *
 * There is one rule rather than two on purpose. The board deduplicated by
 * `updated_at` while the history took whichever board came last in the array,
 * so a breed reported by two boxes could show one box's number on the card and
 * the other box's at the end of the line right beside it.
 *
 * Takes publisher entries ({source, boards}) so the winner can carry the name
 * of the box it came from.
 */
export function mergeBoards(entries) {
  const byAssoc = new Map();
  for (const e of entries ?? []) {
    for (const b of e?.boards ?? []) {
      if (!b?.association) continue;
      const prev = byAssoc.get(b.association);
      if (!prev || (b.updated_at ?? '') > (prev.updated_at ?? '')) {
        byAssoc.set(b.association, { ...b, source: e?.source });
      }
    }
  }
  return [...byAssoc.values()];
}

/**
 * A point is {t: epoch seconds, b: {ASSOC: [done, pending]}}.
 *
 * Arrays rather than objects for the pair: this is the one structure that grows
 * without bound, and `[103876,41000]` against `{"done":103876,"pending":41000}`
 * is the difference between a 25 KB blob and an 80 KB one on every page view.
 */
export function pointFrom(boards, nowMs) {
  const b = {};
  for (const board of mergeBoards([{ boards }])) {
    b[board.association] = [Number(board.done) || 0, Number(board.pending) || 0];
  }
  return { t: Math.round(nowMs / 1000), b };
}

/**
 * Whether a reading is due, i.e. whether appendPoint would keep one now.
 *
 * Separate from appendPoint so a caller can find out before doing the work of
 * assembling the boards: gathering every publisher's view costs a blob list and
 * a read each, and 19 of every 20 publishes have nothing to record.
 */
export function isDue(history, nowMs) {
  const points = Array.isArray(history?.points) ? history.points : [];
  const last = points[points.length - 1];
  return !last || Math.round(nowMs / 1000) - last.t >= FINEST_STEP;
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
 * A breed's own series carries only its own readings, so a point it is missing
 * from is a gap and draws as a straight run between the readings either side --
 * a stall, honestly, rather than a drop to nothing.
 *
 * Overall carries each association's LATEST reading forward instead of summing
 * only what a point happens to hold. Summing what is present sounds like the
 * cautious choice and is the opposite: an association absent for one point --
 * a crawler restarting, a status file caught mid-write -- subtracted its entire
 * total from the line for that instant, so the overall chart spiked down by a
 * third every time a crawler blinked. Every term in the sum is still a real
 * reading; what carries forward is the last one taken, which is exactly what
 * "recorded so far" means for a count that only ever grows.
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

  const latest = new Map();
  series.overall = points.map((p) => {
    for (const [code, v] of Object.entries(p.b ?? {})) latest.set(code, v);
    let done = 0, pending = 0;
    for (const v of latest.values()) { done += v[0] || 0; pending += v[1] || 0; }
    return { t: p.t, done, pending };
  }).filter((p) => p.done || p.pending);

  return series;
}
