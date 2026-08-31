/**
 * Server-rendered SVG for the progress charts.
 *
 * Drawn here rather than in the browser because the board already renders on the
 * server and refreshes by swapping <main> -- a client charting library would mean
 * a second renderer to keep in step, a CDN dependency on a page that has none,
 * and a chart that is blank until script runs. The hover layer is the only part
 * that needs the browser, and it reads the same numbers back out of the page.
 *
 * Both series are animals, so they share one y scale. Two scales on one plot
 * would invent a relationship between "recorded" and "remaining" that isn't
 * there. The scale is not zero-based: over a day the crawl moves a few thousand
 * out of hundreds of thousands, and a zero-based axis flattens exactly the
 * change the chart exists to show. The y-axis ticks carry real numbers, so the
 * baseline is stated rather than implied.
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

/** Domain covering every series, padded, so no line touches the frame. */
export function domain(seriesList) {
  const vals = seriesList.flat().filter((v) => Number.isFinite(v));
  if (!vals.length) return [0, 1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * PAD;
  return [lo - pad, hi + pad];
}

const round = (n) => Math.round(n * 100) / 100;

/** Points -> an SVG path. Straight segments: readings, not a smooth model. */
export function linePath(points, xOf, yOf) {
  return points.map((p, i) => `${i ? 'L' : 'M'}${round(xOf(p.t))} ${round(yOf(p.v))}`).join(' ');
}

export const compact = (n) => {
  const v = Number(n) || 0;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e4) return `${Math.round(v / 1e3)}k`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(Math.round(v));
};

/**
 * The card-sized chart: two lines, no chrome.
 *
 * No axes or labels by design -- the card prints both current values right
 * beside it, and the line-keys on those labels are what tie number to line. A
 * sparkline that repeats them would just be smaller, worse text.
 */
export function sparkline(series, { w = 240, h = 44 } = {}) {
  if (!series || series.length < 2) return '';
  const done = series.map((p) => p.done);
  const pending = series.map((p) => p.pending);
  const [lo, hi] = domain([done, pending]);
  const t0 = series[0].t, t1 = series[series.length - 1].t;
  const span = Math.max(1, t1 - t0);

  const xOf = (t) => 2 + ((t - t0) / span) * (w - 4);
  const yOf = (v) => h - 3 - ((v - lo) / (hi - lo)) * (h - 6);
  const at = (key) => series.map((p) => ({ t: p.t, v: p[key] }));
  const last = series[series.length - 1];

  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-hidden="true" focusable="false">
    <path d="${linePath(at('done'), xOf, yOf)}" fill="none" stroke="var(--rec)"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${linePath(at('pending'), xOf, yOf)}" fill="none" stroke="var(--rem)"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.done))}" r="3"
      fill="var(--rec)" stroke="var(--card)" stroke-width="2"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.pending))}" r="3"
      fill="var(--rem)" stroke="var(--card)" stroke-width="2"/>
  </svg>`;
}

const day = (t) => new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

/**
 * The overall chart: axes, gridlines, a key per series, end labels where they
 * fit, and the geometry the hover layer needs.
 */
export function trendChart(series, { w = 340, h = 190 } = {}) {
  if (!series || series.length < 2) return '';
  const m = { t: 12, r: 12, b: 24, l: 46 };
  const done = series.map((p) => p.done);
  const pending = series.map((p) => p.pending);
  const [lo, hi] = domain([done, pending]);
  const t0 = series[0].t, t1 = series[series.length - 1].t;
  const span = Math.max(1, t1 - t0);

  const xOf = (t) => m.l + ((t - t0) / span) * (w - m.l - m.r);
  const yOf = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (h - m.t - m.b);
  const at = (key) => series.map((p) => ({ t: p.t, v: p[key] }));
  const last = series[series.length - 1];

  // Hairline grid, solid, one step off the surface -- never dashed.
  const grid = ticks(lo, hi).map((v) => `
    <line x1="${m.l}" y1="${round(yOf(v))}" x2="${w - m.r}" y2="${round(yOf(v))}"
      stroke="var(--rule)" stroke-width="1"/>
    <text x="${m.l - 6}" y="${round(yOf(v)) + 3}" class="tick" text-anchor="end">${compact(v)}</text>`).join('');

  // End labels only when the two lines are far enough apart to stay attached to
  // the right line; converging series fall back to the key and the tooltip.
  const gap = Math.abs(yOf(last.done) - yOf(last.pending));
  const endLabels = gap > 22 ? `
    <text x="${round(xOf(last.t))}" y="${round(yOf(last.done)) - 8}" class="endlab" text-anchor="end">${compact(last.done)}</text>
    <text x="${round(xOf(last.t))}" y="${round(yOf(last.pending)) + 14}" class="endlab" text-anchor="end">${compact(last.pending)}</text>` : '';

  return `<svg class="trend" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
      role="img" aria-label="Recorded and remaining animals over time">
    ${grid}
    <line x1="${m.l}" y1="${h - m.b}" x2="${w - m.r}" y2="${h - m.b}" stroke="var(--rule)" stroke-width="1"/>
    <text x="${m.l}" y="${h - 8}" class="tick">${day(t0)}</text>
    <text x="${w - m.r}" y="${h - 8}" class="tick" text-anchor="end">${day(t1)}</text>
    <path d="${linePath(at('done'), xOf, yOf)}" fill="none" stroke="var(--rec)"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${linePath(at('pending'), xOf, yOf)}" fill="none" stroke="var(--rem)"
      stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.done))}" r="4"
      fill="var(--rec)" stroke="var(--card)" stroke-width="2"/>
    <circle cx="${round(xOf(last.t))}" cy="${round(yOf(last.pending))}" r="4"
      fill="var(--rem)" stroke="var(--card)" stroke-width="2"/>
    ${endLabels}
    <line class="crosshair" x1="0" y1="${m.t}" x2="0" y2="${h - m.b}"
      stroke="var(--dim)" stroke-width="1" opacity="0"/>
  </svg>`;
}
