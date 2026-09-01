import { getStore } from '@netlify/blobs';
import { timingSafeEqual } from 'node:crypto';
import { appendPoint, isDue, mergeBoards } from '../lib/history.mjs';

/**
 * Where the crawl box sends its progress.
 *
 * The crawlers run on a Lightsail instance with no inbound ports open, so
 * they push rather than being polled. One blob per publisher: they overwrite
 * themselves and never each other, which is what would let a second box (or
 * the laptop) publish alongside without coordinating.
 *
 * Status only, by design. The payload carries counts and rates, never animal
 * records -- see publish_status.py, which does the projecting at the source
 * so that nothing else has to be trusted to strip it later.
 */
export default async (request) => {
  if (request.method !== 'POST') {
    return json({ error: 'POST a status payload here' }, 405);
  }

  const expected = process.env.CATTLE_INGEST_TOKEN;
  if (!expected) {
    // Refuse rather than accept anything: an unset secret must not silently
    // turn this into a public write endpoint. A typo in the variable name
    // should break loudly here, not quietly open the door.
    return json({ error: 'CATTLE_INGEST_TOKEN is not set on this site' }, 503);
  }
  if (!authorized(request.headers.get('authorization'), expected)) {
    return json({ error: 'bad or missing token' }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'body is not JSON' }, 400);
  }

  const source = String(body?.source ?? '').trim();
  // Used as a blob key, so it is checked rather than trusted.
  if (!/^[a-z0-9][a-z0-9_-]{0,39}$/i.test(source)) {
    return json({ error: 'source must be 1-40 chars of [a-z0-9_-]' }, 400);
  }

  if (!Array.isArray(body?.boards)) {
    return json({ error: 'boards must be an array' }, 400);
  }

  // Reject anything that smells like an animal record. The projection happens
  // on the box, but a public write endpoint should not depend on the client
  // having done the right thing.
  const allowed = new Set([
    'association', 'state', 'done', 'pending', 'failed', 'skipped',
    'rate_per_min', 'pct', 'updated_at', 'current',
  ]);
  // `current` names the animal being read right now. Identity only -- the
  // crawler's own record also carries colour, birth date, sex and genetic
  // defect findings, and those are not published. publish_status.py trims it
  // at the source; this is the second gate, so the guarantee does not rest on
  // the client having done it right.
  const allowedCurrent = new Set(['association', 'reg', 'name']);
  for (const b of body.boards) {
    if (!b || typeof b !== 'object') {
      return json({ error: 'each board must be an object' }, 400);
    }
    const extra = Object.keys(b).filter((k) => !allowed.has(k));
    if (extra.length) {
      return json({ error: `board carries unexpected fields: ${extra.join(', ')}` }, 400);
    }
    if (b.current !== undefined && b.current !== null) {
      if (typeof b.current !== 'object' || Array.isArray(b.current)) {
        return json({ error: 'current must be an object' }, 400);
      }
      const extraCur = Object.keys(b.current).filter((k) => !allowedCurrent.has(k));
      if (extraCur.length) {
        return json({ error: `current carries unexpected fields: ${extraCur.join(', ')}` }, 400);
      }
    }
  }

  // The graph half of the board: aggregate counts out of Neo4j, published by
  // the same timer. Checked the same way as boards -- an allowlist of keys
  // that must all be numbers, so a future change on the box cannot quietly
  // start posting animal-level data to a public page.
  if (body.graph !== undefined && body.graph !== null) {
    const bad = checkGraph(body.graph);
    if (bad) return json({ error: bad }, 400);
  }

  // A crawler that has just restarted reports nothing for a moment. Letting
  // that overwrite a good board is how a dashboard erases itself: nothing
  // errors, the numbers simply vanish.
  const store = getStore('cattle-graph');
  const key = `status/${source}`;
  const previous = await store.get(key, { type: 'json' }).catch(() => null);

  if (previous?.boards?.length && !body.boards.length && !body.force) {
    return json({
      error: 'refusing to replace a populated board with an empty one',
      hint: 'pass force: true if this is deliberate',
    }, 409);
  }

  const entry = {
    source,
    boards: body.boards,
    graph: body.graph ?? null,
    disk: body.disk ?? null,
    publishedAt: new Date().toISOString(),
  };
  await store.setJSON(key, entry);

  // Progress over time, for the trend charts. Best effort on purpose: the
  // status blob above is what the board needs to work at all, and losing a
  // point out of the history is not worth failing a publish -- and so
  // rejecting the crawl box's only report -- over.
  let recorded = false;
  try {
    const prev = await store.get('history', { type: 'json' }).catch(() => null);
    // 30s publishes against a 10-minute resolution: usually there is nothing
    // to record, and asking first is what keeps the reads below off the other
    // nineteen calls.
    if (isDue(prev, Date.now())) {
      const boards = mergeBoards(await everySource(store, entry));
      const { points, changed } = appendPoint(prev, boards, Date.now());
      if (changed) {
        await store.setJSON('history', { points });
        recorded = true;
      }
    }
  } catch {
    // Deliberately swallowed; the publish itself already succeeded.
  }

  return json({ ok: true, source, boards: entry.boards.length, recorded });
};

/**
 * Every publisher's latest boards, this request's included.
 *
 * The history is one blob shared by every publisher, so a point has to describe
 * the whole crawl -- not the slice belonging to whichever box happened to POST
 * first after the ten-minute boundary. Recording just `body.boards` meant that
 * with two boxes up (the arrangement this endpoint exists to allow) the winner
 * of that race alternated, and a breed both of them report wrote alternating
 * counts into one series: a card whose number was right and whose line beside
 * it was a sawtooth between two boxes' frontiers.
 *
 * `entry` is merged in directly rather than read back, so a listing that has
 * not yet caught up with the write above cannot drop this box's own reading.
 */
async function everySource(store, entry) {
  // A listing that fails costs the other boxes' readings, not the point: one
  // box's progress recorded is better than a gap in the series.
  const { blobs } = await store.list({ prefix: 'status/' }).catch(() => ({ blobs: [] }));
  const others = await Promise.all(
    blobs
      .filter(({ key }) => key !== `status/${entry.source}`)
      .map(({ key }) => store.get(key, { type: 'json' }).catch(() => null)),
  );
  // `entry` last so that if two boxes somehow stamp the same updated_at, the
  // one that just published is the one kept.
  return [...others.filter(Boolean), entry];
}

/**
 * Validate the graph block, returning an error string or null.
 *
 * Counts only, by construction: every leaf must be a finite number, so a name,
 * a registration or an error string carrying the Aura host cannot ride along
 * inside it even by accident.
 */
function checkGraph(graph) {
  if (typeof graph !== 'object' || Array.isArray(graph)) return 'graph must be an object';

  const allowed = new Set([
    'ok', 'read_at', 'animals', 'registrations', 'multi_assoc',
    'defects', 'by_association',
  ]);
  const extra = Object.keys(graph).filter((k) => !allowed.has(k));
  if (extra.length) return `graph carries unexpected fields: ${extra.join(', ')}`;

  for (const k of ['animals', 'registrations', 'multi_assoc']) {
    if (graph[k] !== undefined && !Number.isFinite(graph[k])) {
      return `graph.${k} must be a number`;
    }
  }

  // The four statuses norm_defect_status() collapses every raw test result to.
  const statuses = new Set(['Free', 'Carrier', 'Suspect', 'Unknown']);
  for (const [k, v] of Object.entries(graph.defects ?? {})) {
    if (!statuses.has(k)) return `graph.defects carries unexpected status: ${k}`;
    if (!Number.isFinite(v)) return `graph.defects.${k} must be a number`;
  }

  for (const [code, v] of Object.entries(graph.by_association ?? {})) {
    if (!/^[a-z0-9_-]{1,40}$/i.test(code)) {
      return `graph.by_association has a bad association code: ${code}`;
    }
    if (!v || typeof v !== 'object' || Array.isArray(v)) {
      return `graph.by_association.${code} must be an object`;
    }
    const extraCode = Object.keys(v).filter((x) => !['registrations', 'behind'].includes(x));
    if (extraCode.length) {
      return `graph.by_association.${code} carries unexpected fields: ${extraCode.join(', ')}`;
    }
    for (const x of ['registrations', 'behind']) {
      if (v[x] !== undefined && !Number.isFinite(v[x])) {
        return `graph.by_association.${code}.${x} must be a number`;
      }
    }
  }
  return null;
}

/** Constant-time compare, so the token cannot be guessed a byte at a time. */
function authorized(header, expected) {
  const given = String(header ?? '').replace(/^Bearer\s+/i, '');
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });

export const config = { path: '/api/publish' };
