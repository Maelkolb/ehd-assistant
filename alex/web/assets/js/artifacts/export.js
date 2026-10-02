/** Standalone HTML exports of visualisations: one self-describing file that
    carries the app's own styles and, where needed, the drawing code. */

import { escapeHtml, download, slug } from '../dom.js';
import { getLanguage, t } from '../i18n.js';
import { assetUrl, stylesheetText } from './lazy.js';

const LINE_SEPARATORS = new RegExp('[' + String.fromCharCode(0x2028, 0x2029) + ']', 'g');

function safeJson(value) {
  return JSON.stringify(value).replace(/</g, '\\u003c').replace(LINE_SEPARATORS, ' ');
}

/**
 * @param {string} title
 * @param {string} bodyHtml  trusted markup built by the app (text already escaped)
 * @param {{head?: string, script?: string, data?: unknown}} extra
 */
export async function exportStandalone(title, bodyHtml, extra = {}) {
  const [tokens, artifacts] = await Promise.all([
    stylesheetText('css/tokens.css'), stylesheetText('css/artifacts.css')]);
  const script = extra.script
    ? `<script>const DATA = ${safeJson(extra.data ?? null)};\n${extra.script}<\/script>` : '';
  const html = `<!DOCTYPE html>
<html lang="${getLanguage()}" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<link rel="stylesheet" href="${assetUrl('css/fonts.css')}">
${extra.head || ''}
<style>
${tokens}
${artifacts}
html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--ink); font-family: var(--font-sans); }
.export { max-width: 60rem; margin: 0 auto; padding: 1.5rem 1rem 2rem; }
.export h1 { font-family: var(--font-serif); font-size: 1.5rem; font-weight: 700; margin: 0 0 1rem; }
.export-credit { margin-top: 1rem; color: var(--muted); font-size: .8125rem; }
.export .artifact-body { border: 1px solid var(--border); border-radius: 10px; background: var(--surface); overflow: hidden; }
</style>
</head>
<body>
<main class="export">
<h1>${escapeHtml(title)}</h1>
<div class="artifact-body">${bodyHtml}</div>
<p class="export-credit">${escapeHtml(t('artifact.credit'))} · ${new Date().toISOString().slice(0, 10)}</p>
</main>
${script}
</body>
</html>`;
  download(`${slug(title)}.html`, html, 'text/html;charset=utf-8');
}
