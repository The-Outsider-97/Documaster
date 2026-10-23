import test from 'node:test';
import assert from 'node:assert/strict';
import { Api, ApiError, type FetchLike } from '../src/client/api.js';

type Call = { url: string; method?: string; headers?: Record<string, string>; body?: unknown };
function mock(responses: Array<{ status?: number; body?: unknown; raw?: string; throws?: Error }>): { fetch: FetchLike; calls: Call[] } {
  const calls: Call[] = [];
  let i = 0;
  const fetchFn: FetchLike = async (url, init) => {
    calls.push({ url, method: init?.method, headers: init?.headers, body: init?.body });
    const r = responses[Math.min(i++, responses.length - 1)]!;
    if (r.throws) throw r.throws;
    const status = r.status ?? 200;
    return { ok: status < 400, status, text: async () => r.raw ?? JSON.stringify(r.body ?? { ok: true, data: null }) };
  };
  return { fetch: fetchFn, calls };
}

test('every call carries the request marker, and JSON bodies are sent as JSON', async () => {
  const m = mock([{ body: { ok: true, data: { x: 1 } } }]);
  const api = new Api('http://h', m.fetch);
  assert.deepEqual(await api.post('/api/merge', { a: 1 }), { x: 1 });
  assert.equal(m.calls[0]!.url, 'http://h/api/merge');
  assert.equal(m.calls[0]!.headers!['X-Documaster'], '1');
  assert.equal(m.calls[0]!.headers!['Content-Type'], 'application/json');
  assert.equal(m.calls[0]!.body, '{"a":1}');
});

test('a GET has no body and no content type', async () => {
  const m = mock([{ body: { ok: true, data: [] } }]);
  await new Api('', m.fetch).get('/api/documents');
  assert.equal(m.calls[0]!.body, undefined);
  assert.equal(m.calls[0]!.headers!['Content-Type'], undefined);
  assert.equal(m.calls[0]!.method, 'GET');
});

test('server errors become ApiError with code, message, status and details', async () => {
  const m = mock([{ status: 400, body: { ok: false, error: { code: 'invalid_request', message: 'Pick a page', details: { field: 'pages' } } } }]);
  await assert.rejects(new Api('', m.fetch).post('/x'), (e: unknown) => {
    if (!(e instanceof ApiError)) throw new Error('not an ApiError');
    assert.equal(e.code, 'invalid_request');
    assert.equal(e.message, 'Pick a page');
    assert.equal(e.status, 400);
    assert.deepEqual(e.details, { field: 'pages' });
    return true;
  });
});

test('a response that is not JSON is reported as such, never as data', async () => {
  const m = mock([{ status: 502, raw: '<html>bad gateway</html>' }]);
  await assert.rejects(new Api('', m.fetch).get('/x'), (e: unknown) => e instanceof ApiError && e.code === 'bad_response' && e.status === 502);
});

test('an unreachable server gives a plain-language network error', async () => {
  const m = mock([{ throws: new TypeError('fetch failed') }]);
  await assert.rejects(new Api('', m.fetch).get('/x'), (e: unknown) => e instanceof ApiError && e.code === 'network' && /not reachable/.test(e.message));
});

test('an aborted request is a cancellation, not a failure', async () => {
  const abort = Object.assign(new Error('aborted'), { name: 'AbortError' });
  const m = mock([{ throws: abort }]);
  await assert.rejects(new Api('', m.fetch).get('/x'), (e: unknown) => e instanceof ApiError && e.cancelled);
});

test('a job is polled until it is done, with progress reported', async () => {
  const m = mock([
    { status: 202, body: { ok: true, data: { job: { id: 'j1', state: 'queued' } } } },
    { body: { ok: true, data: { id: 'j1', state: 'running', progress: 0.4, message: 'Working' } } },
    { body: { ok: true, data: { id: 'j1', state: 'done', progress: 1, result: { answer: 42 } } } },
  ]);
  const seen: number[] = [];
  const result = await new Api('', m.fetch, [1]).runJob<{ answer: number }>('/api/convert', {}, (j) => seen.push(j.progress));
  assert.deepEqual(result, { answer: 42 });
  assert.deepEqual(seen, [0.4, 1]);
  assert.deepEqual(m.calls.map((c) => c.url), ['/api/convert', '/api/jobs/j1', '/api/jobs/j1']);
});

test('a failed job rejects with the server\'s error', async () => {
  const m = mock([
    { status: 202, body: { ok: true, data: { job: { id: 'j2' } } } },
    { body: { ok: true, data: { id: 'j2', state: 'failed', error: { code: 'verification_failed', message: 'The output did not match', details: {} } } } },
  ]);
  await assert.rejects(new Api('', m.fetch, [1]).runJob('/api/merge', {}), (e: unknown) => e instanceof ApiError && e.code === 'verification_failed');
});

test('a cancelled job rejects as a cancellation', async () => {
  const m = mock([
    { status: 202, body: { ok: true, data: { job: { id: 'j3' } } } },
    { body: { ok: true, data: { id: 'j3', state: 'cancelled' } } },
  ]);
  await assert.rejects(new Api('', m.fetch, [1]).runJob('/api/merge', {}), (e: unknown) => e instanceof ApiError && e.cancelled);
});

test('cancelling a job posts to its cancel endpoint', async () => {
  const m = mock([{ status: 202, body: { ok: true, data: { job: { id: 'j4' } } } }, { body: { ok: true, data: {} } }]);
  const api = new Api('', m.fetch, [1]);
  const h = await api.startJob('/api/convert', {});
  await h.cancel();
  assert.equal(m.calls[1]!.url, '/api/jobs/j4/cancel');
  assert.equal(m.calls[1]!.method, 'POST');
});

test('uploads send the name url-encoded and the marker, and never as JSON', async () => {
  const m = mock([{ body: { ok: true, data: { id: 'd1', name: 'a b&c.pdf' } } }]);
  const doc = await new Api('', m.fetch).upload(new Blob(['x']), 'a b&c.pdf');
  assert.equal(doc.id, 'd1');
  assert.equal(m.calls[0]!.url, '/api/documents?name=a%20b%26c.pdf');
  assert.equal(m.calls[0]!.headers!['X-Documaster'], '1');
  assert.equal(m.calls[0]!.headers!['Content-Type'], undefined);
});

test('image uploads ask for images explicitly', async () => {
  const m = mock([{ body: { ok: true, data: {} } }]);
  await new Api('', m.fetch).upload(new Blob(['x']), 'p.png', { images: true });
  assert.match(m.calls[0]!.url, /&images=1$/);
});

test('urls for the browser are built from ids only', () => {
  const api = new Api('http://h');
  assert.equal(api.downloadUrl('o1'), 'http://h/api/outputs/o1/download');
  assert.equal(api.pageImageUrl('d', 3, 199.6, 'abcdefghijklmnop'), 'http://h/api/documents/d/pages/3/image?w=200&v=abcdefghijkl');
});
