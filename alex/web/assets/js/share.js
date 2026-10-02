/** Share links carry the whole answer in the URL fragment (gzip + base64url),
    so nothing is stored on the server and the fragment never leaves the browser. */

const MAX_INLINE_IMAGE = 200_000;

async function gzip(text) {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream('gzip'));
  const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function gunzip(encoded) {
  const binary = atob(encoded.replace(/-/g, '+').replace(/_/g, '/'));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}

export async function shareUrl(query, answer) {
  const artifacts = (answer.artifacts || []).filter((artifact) => (
    !(artifact.type === 'image' && artifact.data && artifact.data.length > MAX_INLINE_IMAGE)));
  const payload = {
    q: query,
    a: { text: answer.text, blocks: answer.blocks, sources: answer.sources, artifacts,
         model: answer.model, elapsed_s: answer.elapsed_s },
  };
  const url = new URL(location.href);
  url.hash = 'a=' + await gzip(JSON.stringify(payload));
  return url.toString();
}

export async function sharedAnswer() {
  if (!location.hash.startsWith('#a=')) return null;
  try {
    const payload = JSON.parse(await gunzip(location.hash.slice(3)));
    if (payload && payload.a && typeof payload.q === 'string') return { query: payload.q, answer: payload.a };
  } catch (error) {
    console.warn('Could not read the shared answer', error);
  }
  return null;
}
