/**
 * Server-rendered SVG for the progress charts.
 *
 * Drawn here rather than in the browser because the board already renders on
 * the server and refreshes by swapping <main> -- a client charting library
 * would mean a second renderer to keep in step, a CDN dependency on a page
 * that has none, and a chart that is blank until script runs. The hover layer
 * is the only part that needs the browser, and it reads the numbers back out
 * of the page.
 *
 * SMALL MULTIPLES, one series each, rather than both series on one plot.
 * Recorded and remaining are the same unit, so sharing an axis is the honest
 * choice and two axes on one plot would be the classic lie -- but recorded sits
 * near 338k while remaining sits near 887k, and over three days each moves
 * about 1%. On a shared scale that is two flat lines a third of the panel
 * apart: truthful, and completely unreadable. Faceting keeps one scale per
 * plot, which is the sanctioned way out.
 *
 * Each scale is also non-zero-based, for the same reason: zero-basing a series
 * that moves 1% flattens exactly the change the chart exists to show. The y
 * ticks carry real numbers, so the baseline is stated rather than implied.
 */

const PAD = 0.08;   // headroom above/below the data, as a fraction of its range

/** Nice round tick values inside [lo, hi] -- 1/2/5 x a power of ten. */
export function ticks(lo, hi, count = 3) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const rough = span / count;
  const mag = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= rough) ?? mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

/** Padded domain for one series, never a zero-width one. */
export function domain(values) {
  const vals = values.filter((v) => Number.isFinite(v));
  if (!vals.length) return [0, 1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * PAD;
  return [lo - pad, hi + pad];
}

const round = (n) => Math.round(n * 100) / 100;

export const compact = (n) => {
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e3).toFixed(1)}k`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(Math.round(v));
};

/** Straight segments between readings -- a line, not a smoothed model. */
const linePath = (pts, xOf, yOf) =>
  pts.map((p, i) => `${i ? 'L' : 'M'}${round(xOf(p.t))} ${round(yOf(p.v))}`).join(' ');

const seriesOf = (points, key) => points.map((p) => ({ t: p.t, v: p[key] }));

/**
 * The card-sized chart: one series, no chrome.
 *
 * No axes or labels -- the card prints the current value right beside it, and
 * the line-key on that label is what ties number to line. A sparkline that
 * repeated the number would just be smaller, worse text.
 */
export function sparkline(points, key, { w = 240, h = 26 } = {}) {
  if (!points || points.length < 2) return '';
  const pts = seriesOf(points, key);
  const [lo, hi] = domain(pts.map((p) => p.v));
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const xOf = (t) => 2 + ((t - t0) / span) * (w - 4);
  const yOf = (v) => h - 3 - ((v - lo) / (hi - lo)) * (h - 6);
  const last = pts[pts.length - 1];
  const stroke = key === 'done' ? 'var(--rec)' : 'var(--rem)';

  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-hidden="true" focusable="false">
    <path d="${linePath(pts, xOf, yOf)}" fill="none" stroke="${stroke}"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.v))}" r="2.5"
      fill="${stroke}" stroke="var(--card)" stroke-width="2"/>
  </svg>`;
}

const day = (t) => new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

/**
 * One faceted panel: a single series with its own scale, gridlines and ticks.
 *
 * `xAxis` only on the last panel of a stack -- the facets share an x domain, so
 * repeating the dates under each one is ink that says nothing new.
 */
export function panel(points, key, { w = 560, h = 104, label = '', xAxis = false } = {}) {
  if (!points || points.length < 2) return '';
  const pts = seriesOf(points, key);
  const [lo, hi] = domain(pts.map((p) => p.v));
  const m = { t: 10, r: 40, b: xAxis ? 20 : 8, l: 46 };
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const xOf = (t) => m.l + ((t - t0) / span) * (w - m.l - m.r);
  const yOf = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (h - m.t - m.b);
  const last = pts[pts.length - 1];
  const stroke = key === 'done' ? 'var(--rec)' : 'var(--rem)';

  // Two or three lines: enough to read a slope against, few enough to stay
  // recessive on a 92px panel.
  const grid = ticks(lo, hi, 2).map((v) => `
    <line x1="${m.l}" y1="${round(yOf(v))}" x2="${w - m.r}" y2="${round(yOf(v))}"
      stroke="var(--rule)" stroke-width="1"/>
    <text x="${m.l - 6}" y="${round(yOf(v)) + 3}" class="tick" text-anchor="end">${compact(v)}</text>`).join('');

  const dates = xAxis ? `
    <text x="${m.l}" y="${h - 6}" class="tick">${day(t0)}</text>
    <text x="${w - m.r}" y="${h - 6}" class="tick" text-anchor="end">${day(t1)}</text>` : '';

  return `<svg class="trend" data-key="${key}" viewBox="0 0 ${w} ${h}"
      role="img" aria-label="${label} over time, ending at ${compact(last.v)}">
    ${grid}
    ${dates}
    <path d="${linePath(pts, xOf, yOf)}" fill="none" stroke="${stroke}"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.v))}" r="4"
      fill="${stroke}" stroke="var(--card)" stroke-width="2"/>
    <text x="${w - m.r + 6}" y="${round(yOf(last.v)) + 3}" class="endlab">${compact(last.v)}</text>
    <line class="crosshair" x1="0" y1="${m.t}" x2="0" y2="${h - m.b}"
      stroke="var(--dim)" stroke-width="1" opacity="0"/>
  </svg>`;
}

/**
 * What the hover layer needs: the facets share an x domain, so one geometry
 * serves both and a pointer in either panel can drive both crosshairs.
 */
export function geometry(points, { w = 560 } = {}) {
  const m = { l: 46, r: 40 };
  return {
    w, plot: [m.l, w - m.r],
    t0: points[0].t, t1: points[points.length - 1].t,
    points: points.map((p) => [p.t, p.done, p.pending]),
  };
}
