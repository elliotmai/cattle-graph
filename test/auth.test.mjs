// The paths that decide whether this board is private. Each of these returns
// before the blob store is ever touched, so they run without a Netlify
// context -- which is exactly why they are worth asserting here rather than
// discovering in production.
//
//   node --test netlify/functions/auth.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
// import() takes the URL directly; going via .pathname yields "/C:/..." on
// Windows, which then resolves to a nonexistent "C:\C:\...".
const load = async (file) => (await import(new URL('../netlify/functions/' + file, import.meta.url).href)).default;

const basic = (user, pass) => 'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64');
const get = (headers = {}) => new Request('https://example.test/', { headers });
const post = (body, headers = {}) => new Request('https://example.test/api/publish', {
  method: 'POST',
  headers: { 'content-type': 'application/json', ...headers },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('dashboard refuses to serve when no password is configured', async () => {
  delete process.env.CATTLE_VIEW_PASSWORD;
  const dashboard = await load('dashboard.mjs');
  const res = await dashboard(get());
  // 503, never 200: an unset password must not mean "no password required".
  assert.equal(res.status, 503);
});

test('dashboard challenges when the password is missing or wrong', async () => {
  process.env.CATTLE_VIEW_PASSWORD = 'correct-horse';
  const dashboard = await load('dashboard.mjs');

  const none = await dashboard(get());
  assert.equal(none.status, 401);
  assert.match(none.headers.get('www-authenticate'), /^Basic realm=/);

  const wrong = await dashboard(get({ authorization: basic('me', 'wrong') }));
  assert.equal(wrong.status, 401);

  // Same length as the real password, to prove the compare is not a prefix
  // check that a guesser could walk one byte at a time.
  const sameLength = await dashboard(get({ authorization: basic('me', 'correct-horsX') }));
  assert.equal(sameLength.status, 401);
});

test('dashboard lets the right password through to the store', async () => {
  process.env.CATTLE_VIEW_PASSWORD = 'correct-horse';
  const dashboard = await load('dashboard.mjs');
  // No Netlify context here, so getStore throws -- which is itself the proof
  // that auth passed and execution reached the store.
  await assert.rejects(() => dashboard(get({ authorization: basic('anyone', 'correct-horse') })));
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

test('publish rejects boards carrying animal data', async () => {
  process.env.CATTLE_INGEST_TOKEN = 'ingest-secret';
  const publish = await load('publish.mjs');
  const auth = { authorization: 'Bearer ingest-secret' };

  // This is the "status only" guarantee. The projection happens on the box,
  // but the endpoint must not depend on the client having done it right.
  const res = await publish(post({
    source: 'lightsail',
    boards: [{ association: 'CHIA', done: 1, current: { name: 'TLF MS TANDY 1CA', reg: '212025' } }],
  }, auth));

  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /unexpected fields: current/);
});
