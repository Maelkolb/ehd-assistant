/** HTTP client for the API described in docs/API.md. All URLs are relative to
    the page, so the app works under any path prefix. */

const BASE = new URL('api/', document.baseURI);

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function getJson(path, parameters) {
  const url = new URL(path, BASE);
  for (const [name, value] of Object.entries(parameters || {})) {
    if (value !== undefined && value !== null) url.searchParams.set(name, value);
  }
  const response = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new ApiError(await detailOf(response), response.status);
  return response.json();
}

async function detailOf(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail) && body.detail[0]?.msg) return body.detail[0].msg;
  } catch { /* not JSON */ }
  return `HTTP ${response.status}`;
}

export const getConfig = () => getJson('config');
export const getPassage = (docId, start, end) => getJson('passage', { doc_id: docId, start, end });
export const getNote = (noteId) => getJson(`notes/${encodeURIComponent(noteId)}`);
export const getEntity = (entityId) => getJson(`entities/${encodeURIComponent(entityId)}`);

export async function sendFeedback(requestId, rating, comment = '') {
  await fetch(new URL('feedback', BASE), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, rating, comment }),
  });
}

/** POST /api/chat and yield the server-sent events as parsed objects. */
export async function* streamChat(body, signal) {
  let response;
  try {
    response = await fetch(new URL('chat', BASE), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(error.message, 0, 'network');
  }
  if (!response.ok) {
    const code = response.status === 429 ? 'rate' : response.status === 503 ? 'unavailable' : 'http';
    throw new ApiError(await detailOf(response), response.status, code);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split('\n').filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart()).join('\n');
        if (!data) continue;
        try {
          yield JSON.parse(data);
        } catch {
          console.warn('Unreadable stream event', data.slice(0, 200));
        }
      }
    }
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(error.message, 0, 'network');
  } finally {
    reader.releaseLock?.();
  }
}
