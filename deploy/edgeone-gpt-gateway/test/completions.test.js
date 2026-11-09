import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequest } from '../cloud-functions/v1/chat/completions.js';

const BACKEND_IP = '192.0.2.10';
const GATEWAY_TOKEN = 'test-gateway-token-with-32-plus-characters';
const ENV = {
  OPENAI_API_KEY: 'mock-openai-key',
  MAHJONG_GATEWAY_TOKEN: GATEWAY_TOKEN,
  MAHJONG_BACKEND_IP: BACKEND_IP,
};
const CHAT = {
  model: 'gpt-6.1-sol',
  messages: [
    { role: 'system', content: '只选择合法候选' },
    { role: 'user', content: '{"candidates":[{"id":"A1"}]}' },
  ],
  temperature: 0.4,
  top_p: 1,
  stream: true,
  n: 1,
  reasoning_effort: 'low',
};

function context({
  method = 'POST', env = ENV, clientIp = BACKEND_IP, token = GATEWAY_TOKEN,
  headers = {}, body = JSON.stringify(CHAT),
} = {}) {
  return {
    env, clientIp,
    request: new Request('https://gateway.example/v1/chat/completions', {
      method,
      headers: {
        'content-type': 'application/json',
        ...(token === null ? {} : { authorization: `Bearer ${token}` }),
        ...headers,
      },
      body: method === 'POST' ? body : undefined,
    }),
  };
}

test('rejects callers and bad configuration before fetching OpenAI', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', () => {});
  const cases = [
    [context({ method: 'GET' }), 405],
    [context({ env: {} }), 503],
    [context({ env: { ...ENV, MAHJONG_GATEWAY_TOKEN: 'short' } }), 503],
    [context({ env: { ...ENV, OPENAI_API_KEY: GATEWAY_TOKEN } }), 503],
    [context({ clientIp: null }), 403],
    [context({
      clientIp: '198.51.100.3',
      headers: { 'x-forwarded-for': BACKEND_IP, 'eo-connecting-ip': BACKEND_IP },
    }), 403],
    [context({ token: null }), 401],
    [context({ token: GATEWAY_TOKEN.replace('test', 'fake') }), 401],
  ];
  for (const [input, status] of cases) {
    assert.equal((await onRequest(input)).status, status);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test('rejects malformed or oversized requests and other models', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', () => {});
  const cases = [
    [context({ body: '{' }), 400],
    [context({ body: JSON.stringify({ ...CHAT, model: 'other-model' }) }), 400],
    [context({ body: JSON.stringify({ ...CHAT, messages: [null] }) }), 400],
    [context({ body: JSON.stringify({ ...CHAT, n: 2 }) }), 400],
    [context({ headers: { 'content-length': String(256 * 1024 + 1) } }), 413],
    [context({ body: '牌'.repeat(100_000) }), 413],
  ];
  for (const [input, status] of cases) {
    assert.equal((await onRequest(input)).status, status);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test('streams OpenAI chunks and final usage while preserving Mahjong prompt text', { timeout: 2000 }, async (t) => {
  let source;
  const stream = new ReadableStream({
    start(controller) {
      source = controller;
    },
  });
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(stream, {
    headers: { 'content-type': 'text/event-stream', 'x-request-id': 'test-id' },
  }));
  const input = context();
  const result = await onRequest(input);
  const [url, options] = fetch.mock.calls[0].arguments;
  assert.equal(url, 'https://api.openai.com/v1/chat/completions');
  assert.equal(options.headers.authorization, `Bearer ${ENV.OPENAI_API_KEY}`);
  assert.equal(options.redirect, 'error');
  assert.equal(options.signal, input.request.signal);
  assert.deepEqual(JSON.parse(options.body), {
    model: CHAT.model,
    messages: CHAT.messages,
    reasoning_effort: 'low',
    stream: true,
    stream_options: { include_usage: true },
    n: 1,
  });

  assert.equal(result.status, 200);
  assert.equal(result.headers.get('content-type'), 'text/event-stream');
  assert.equal(result.headers.get('x-request-id'), 'test-id');
  assert.match(result.headers.get('cache-control'), /no-store/);
  const reader = result.body.getReader();
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const first = 'data: {"choices":[{"delta":{"content":"A1"}}]}\n\n';
  const tail = 'data: {"choices":[],"usage":{"prompt_tokens":1000,"prompt_tokens_details":{"cached_tokens":384},"completion_tokens":42}}\n\ndata: [DONE]\n\n';
  source.enqueue(encoder.encode(first));
  assert.equal(decoder.decode((await reader.read()).value), first);
  source.enqueue(encoder.encode(tail));
  source.close();
  assert.equal(decoder.decode((await reader.read()).value), tail);
  assert.equal((await reader.read()).done, true);
});

test('passes a non-streaming completion and normalizes a legacy token cap', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ choices: [] }));
  const result = await onRequest(context({
    env: { ...ENV, MAHJONG_BACKEND_IP: '2001:db8::10' },
    clientIp: '2001:db8:0:0:0:0:0:10',
    body: JSON.stringify({ ...CHAT, stream: false, max_tokens: 512 }),
  }));
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { choices: [] });
  const body = JSON.parse(fetch.mock.calls[0].arguments[1].body);
  assert.equal(body.max_completion_tokens, 512);
  assert.equal(body.max_tokens, undefined);
  assert.equal(body.stream_options, undefined);
});

test('preserves OpenAI error status and retry headers without exposing the body', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(
    { error: ENV.OPENAI_API_KEY },
    { status: 429, headers: { 'retry-after': '5', 'x-request-id': 'failed-id' } },
  ));
  const result = await onRequest(context());
  assert.equal(result.status, 429);
  assert.equal(result.headers.get('retry-after'), '5');
  assert.equal(result.headers.get('x-request-id'), 'failed-id');
  assert.doesNotMatch(await result.text(), /mock-openai-key/);
  assert.equal(fetch.mock.callCount(), 1);
});

test('reports a network failure without exposing upstream details or retrying', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => {
    throw new Error(ENV.OPENAI_API_KEY);
  });
  const result = await onRequest(context());
  assert.equal(result.status, 502);
  assert.doesNotMatch(await result.text(), /mock-openai-key/);
  assert.equal(fetch.mock.callCount(), 1);
});
