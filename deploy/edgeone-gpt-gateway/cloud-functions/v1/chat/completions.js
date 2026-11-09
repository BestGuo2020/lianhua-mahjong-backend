import { timingSafeEqual } from 'node:crypto';
import { BlockList, isIP } from 'node:net';

const MODEL = 'gpt-6.1-sol';
const OPENAI_URL = 'https://api.openai.com/v1/chat/completions';
const MAX_REQUEST_BYTES = 256 * 1024;

function apiError(status, message, headers = new Headers()) {
  headers.set('cache-control', 'no-store');
  return Response.json({ error: { message } }, { status, headers });
}

export async function onRequest({ request, env, clientIp }) {
  if (request.method !== 'POST') {
    return apiError(405, 'Only POST is supported', new Headers({ allow: 'POST' }));
  }

  const gatewayToken = env?.MAHJONG_GATEWAY_TOKEN ?? '';
  const openaiKey = env?.OPENAI_API_KEY?.trim();
  const backendIp = env?.MAHJONG_BACKEND_IP?.trim() ?? '';
  const backendIpVersion = isIP(backendIp);
  if (!/^[A-Za-z0-9_-]{32,}$/.test(gatewayToken) || !openaiKey ||
      gatewayToken === openaiKey || !backendIpVersion) {
    return apiError(503, 'Mahjong gateway is not configured');
  }

  // Use the client IP supplied by Makers rather than a request header.
  const allowedIps = new BlockList();
  allowedIps.addAddress(backendIp, backendIpVersion === 4 ? 'ipv4' : 'ipv6');
  const clientIpVersion = isIP(clientIp ?? '');
  if (!clientIpVersion || !allowedIps.check(clientIp, clientIpVersion === 4 ? 'ipv4' : 'ipv6')) {
    return apiError(403, 'Caller IP is not allowed');
  }
  const expected = Buffer.from(`Bearer ${gatewayToken}`);
  const supplied = Buffer.from(request.headers.get('authorization') ?? '');
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    return apiError(401, 'Invalid gateway token', new Headers({ 'www-authenticate': 'Bearer' }));
  }

  if (Number(request.headers.get('content-length')) > MAX_REQUEST_BYTES) {
    return apiError(413, 'Chat request is too large');
  }
  let body;
  try {
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > MAX_REQUEST_BYTES) {
      return apiError(413, 'Chat request is too large');
    }
    body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    return apiError(400, 'Invalid JSON body');
  }

  const {
    model, messages, stream = false, n = 1, reasoning_effort,
    max_completion_tokens = body?.max_tokens,
  } = body ?? {};
  if (model !== MODEL) {
    return apiError(400, 'Model is not allowed');
  }
  if (!Array.isArray(messages) || !messages.length || messages.some(
    (message) => !['system', 'user'].includes(message?.role) || typeof message.content !== 'string',
  ) || typeof stream !== 'boolean' || n !== 1 ||
      (reasoning_effort !== undefined && !['low', 'medium'].includes(reasoning_effort)) ||
      (max_completion_tokens !== undefined &&
        (!Number.isSafeInteger(max_completion_tokens) || max_completion_tokens < 1))) {
    return apiError(400, 'Invalid chat request');
  }

  // Forward the shared Mahjong client contract. Sol uses reasoning, so the
  // client's generic temperature and top_p fields do not go upstream.
  const payload = {
    model, stream, n, reasoning_effort, max_completion_tokens,
    messages: messages.map(({ role, content }) => ({ role, content })),
  };
  if (stream) {
    payload.stream_options = { include_usage: true };
  }

  let upstream;
  try {
    upstream = await fetch(OPENAI_URL, {
      method: 'POST',
      headers: { authorization: `Bearer ${openaiKey}`, 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      redirect: 'error',
      signal: request.signal,
    });
  } catch {
    return apiError(502, 'OpenAI is unreachable');
  }

  const headers = new Headers({ 'cache-control': 'no-store, no-transform' });
  for (const name of ['x-request-id', 'retry-after']) {
    const value = upstream.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  if (!upstream.ok) {
    // Upstream error bodies can contain request or credential details.
    await upstream.body?.cancel().catch(() => {});
    return apiError(upstream.status, 'OpenAI rejected the request', headers);
  }
  const contentType = upstream.headers.get('content-type');
  if (contentType !== null) headers.set('content-type', contentType);
  return new Response(upstream.body, { status: upstream.status, headers });
}
