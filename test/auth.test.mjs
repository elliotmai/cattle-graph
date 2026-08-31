// The paths that decide what may be written to this board, and that reading it
// needs no credential. Each of these returns before the blob store is ever
// touched, so they run without a Netlify context -- which is exactly why they
// are worth asserting here rather than discovering in production.
//
//   node --test test/auth.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
// import() takes the URL directly; going via .pathname yields "/C:/..." on
// Windows, which then resolves to a nonexistent "C:\C:\...".
const load = async (file) => (await import(new URL('../netlify/functions/' + file, import.meta.url).href)).default;

const get = (headers = {}) => new Request('https://example.test/', { headers });
const post = (body, headers = {}) => new Request('https://example.test/api/publish', {
  method: 'POST',
  headers: { 'content-type': 'application/json', ...headers },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('dashboard serves an anonymous reader straight through to the store', async () => {
  // The board is read-only and carries nothing private, so there is no
  // credential to present. No Netlify context here, so getStore throws --
  // which is itself the proof that nothing turned the request away first.
  const dashboard = await load('dashboard.mjs');
  await assert.rejects(() => dashboard(get()));
});

test('dashboard does not gate on a leftover view password', async () => {
  // A CATTLE_VIEW_PASSWORD left set in Netlify from before the board went
  // open must not resurrect the login box.
  process.env.CATTLE_VIEW_PASSWORD = 'correct-horse';
  try {
    const dashboard = await load('dashboard.mjs');
    await assert.rejects(() => dashboard(get()));
  } finally {
    delete process.env.CATTLE_VIEW_PASSWORD;
  }
});

test('publish refuses to accept when no token is configured', async () => {
  delete process.env.CATTLE_INGEST_TOKEN;
  const publish = await load('publish.mjs');
  const res = await publish(post({ source: 'lightsail', boards: [] }));
  assert.equal(res.status, 503);
});

test('publish rejects GET, bad tokens and unparseable bodies', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');

  assert.equal((await publish(new Request('https://example.test/api/publish'))).status, 405);
  assert.equal((await publish(post({}, { authorization: 'Bearer nope' }))).status, 401);
  assert.equal((await publish(post('not json', { authorization: 'Bearer ingest-secret' }))).status, 400);
});

test('publish rejects a source that is not safe as a blob key', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };

  for (const source of ['', '../escape', 'has space', 'x'.repeat(41)]) {
    const res = await publish(post({ source, boards: [] }, auth));
    assert.equal(res.status, 400, `source ${JSON.stringify(source)} should be rejected`);
  }
});

test('publish rejects unknown board fields', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };

  const res = await publish(post({
    source: 'lightsail',
    boards: [{ association: 'CHIA', done: 1, pid: 4016, owner: 'someone' }],
  }, auth));

  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /unexpected fields: pid, owner/);
});

test('publish rejects a current animal carrying more than identity', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };

  // `current` is published so the board can show what is being read. Identity
  // only -- the crawler's record also holds birth date, sex, colour and
  // genetic defect findings, and those must not reach a public URL even if a
  // future change to publish_status.py stops trimming them.
  const res = await publish(post({
    source: 'lightsail',
    boards: [{
      association: 'CHIA',
      done: 1,
      current: {
        association: 'CHIA', reg: '212025', name: 'TLF MS TANDY 1CA',
        dob: '1991-04-03', sex: 'F', defects: [{ code: 'AM', status: 'Suspect' }],
      },
    }],
  }, auth));

  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /current carries unexpected fields: dob, sex, defects/);
});

test('publish rejects a graph block that is not counts', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };
  const send = (graph) => publish(post({ source: 'lightsail', boards: [], graph }, auth));

  // The graph figures are aggregates. Anything shaped like a record, or the
  // driver's error text (which names the Aura host), must not ride along.
  const cases = [
    [{ animals: 1, sample_animal: 'TLF MS TANDY 1CA' }, /unexpected fields: sample_animal/],
    [{ note: 'Unable to connect to abc123.databases.neo4j.io' }, /unexpected fields: note/],
    [{ animals: 'lots' }, /graph.animals must be a number/],
    [{ defects: { Carrier: 3, Reg: 'MA430053' } }, /unexpected status: Reg/],
    [{ by_association: { CHIA: { registrations: 5, owner: 'someone' } } },
     /by_association.CHIA carries unexpected fields: owner/],
    [{ by_association: { CHIA: { behind: 'some' } } }, /by_association.CHIA.behind must be a number/],
    [{ by_association: { '../escape': { behind: 1 } } }, /bad association code/],
    [['not', 'an', 'object'], /graph must be an object/],
  ];

  for (const [graph, expected] of cases) {
    const res = await send(graph);
    assert.equal(res.status, 400, `${JSON.stringify(graph)} should be rejected`);
    assert.match((await res.json()).error, expected);
  }
});

test('publish accepts a well-formed graph block', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  // Reaches the blob store, which throws without a Netlify context -- proof
  // the payload cleared validation rather than being turned away.
  await assert.rejects(() => publish(post({
    source: 'lightsail',
    boards: [{ association: 'CHIA', done: 1 }],
    graph: {
      ok: true, read_at: '2026-08-31T12:00:00Z', animals: 268112,
      registrations: 300503, multi_assoc: 4120,
      defects: { Free: 18422, Carrier: 611 },
      by_association: { CHIA: { registrations: 101940, behind: 2800 } },
    },
  }, { authorization: 'Bearer ingest-secret' })));
});

test('publish rejects a current that is not an object', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };

  const res = await publish(post({
    source: 'lightsail',
    boards: [{ association: 'CHIA', done: 1, current: ['TLF MS TANDY 1CA'] }],
  }, auth));

  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /current must be an object/);
});
