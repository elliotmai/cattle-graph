import { getStore } from '@netlify/blobs';
import { timingSafeEqual } from 'node:crypto';

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
    disk: body.disk ?? null,
    publishedAt: new Date().toISOString(),
  };
  await store.setJSON(key, entry);

  return json({ ok: true, source, boards: entry.boards.length });
};

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
