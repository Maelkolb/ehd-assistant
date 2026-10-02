/** Escape-first markdown renderer for model answers.

    Everything is HTML-escaped before any markup is recognised, and only a
    fixed set of tags is ever emitted, so the result is safe to assign to
    innerHTML. It accepts half-written markdown (an unclosed fence, a table
    whose separator row has not arrived yet) because answers are rendered
    while they stream. Characters U+E000…U+E003 are reserved for placeholders
    the caller swaps for citation chips after rendering. */

import { escapeHtml } from './dom.js';

const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const LIST_ITEM = /^(\s*)([-*+•]|\d{1,4}[.)])\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function renderMarkdown(source) {
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  return renderBlocks(lines);
}

function renderBlocks(lines) {
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1].slice(0, 3);
      const body = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1].length <= 2 ? 3 : heading[1].length === 3 ? 4 : 5;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (RULE.test(line)) {
      html.push('<hr>');
      index += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && QUOTE.test(lines[index])) {
        quoted.push(QUOTE.exec(lines[index])[1]);
        index += 1;
      }
      html.push(`<blockquote>${renderBlocks(quoted)}</blockquote>`);
      continue;
    }

    if (isTableStart(lines, index)) {
      const rows = [];
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(lines[index]);
        index += 1;
      }
      html.push(renderTable(rows));
      continue;
    }

    if (LIST_ITEM.test(line)) {
      const block = [];
      while (index < lines.length) {
        const current = lines[index];
        if (LIST_ITEM.test(current) || /^\s{2,}\S/.test(current)) {
          block.push(current);
        } else if (!current.trim() && index + 1 < lines.length
                   && (LIST_ITEM.test(lines[index + 1]) || /^\s{2,}\S/.test(lines[index + 1]))) {
          block.push('');
        } else {
          break;
        }
        index += 1;
      }
      html.push(renderList(block));
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !startsBlock(lines, index, paragraph.length)) {
      paragraph.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${renderParagraph(paragraph)}</p>`);
  }
  return html.join('\n');
}

function startsBlock(lines, index, paragraphLength) {
  if (!paragraphLength) return false;
  const line = lines[index];
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line)
    || LIST_ITEM.test(line) || isTableStart(lines, index);
}

function isTableStart(lines, index) {
  return lines[index].includes('|') && index + 1 < lines.length
    && TABLE_SEPARATOR.test(lines[index + 1]) && lines[index + 1].includes('-');
}

function renderParagraph(lines) {
  return lines.map((line, position) => {
    const hardBreak = /( {2,}|\\)$/.test(line) && position < lines.length - 1;
    return renderInline(line.replace(/( {2,}|\\)$/, '').trim()) + (hardBreak ? '<br>' : '');
  }).join(' ');
}

function splitCells(row) {
  const cells = row.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/);
  return cells.map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

function renderTable(rows) {
  const header = splitCells(rows[0]);
  const alignment = splitCells(rows[1]).map((cell) => (
    cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : ''));
  const attribute = (column) => (alignment[column] ? ` class="align-${alignment[column]}"` : '');
  const head = header.map((cell, column) => `<th${attribute(column)}>${renderInline(cell)}</th>`).join('');
  const body = rows.slice(2).map((row) => {
    const cells = splitCells(row);
    return '<tr>' + header.map((_, column) => (
      `<td${attribute(column)}>${renderInline(cells[column] ?? '')}</td>`)).join('') + '</tr>';
  }).join('');
  return `<div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderList(lines) {
  const first = LIST_ITEM.exec(lines[0]);
  const baseIndent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  for (const line of lines) {
    const match = LIST_ITEM.exec(line);
    if (match && match[1].length <= baseIndent) {
      items.push({ lead: match[3], rest: [] });
    } else if (items.length) {
      items[items.length - 1].rest.push(line);
    }
  }
  const rendered = items.map((item) => {
    const rest = item.rest;
    while (rest.length && !rest[rest.length - 1].trim()) rest.pop();
    const indent = Math.min(...rest.filter((line) => line.trim())
      .map((line) => /^\s*/.exec(line)[0].length), 99);
    const nested = rest.length ? renderBlocks(rest.map((line) => line.slice(Math.min(indent, line.length)))) : '';
    return `<li>${renderInline(item.lead)}${nested}</li>`;
  }).join('');
  const start = ordered ? parseInt(first[2], 10) : 1;
  const tag = ordered ? 'ol' : 'ul';
  return `<${tag}${ordered && start > 1 ? ` start="${start}"` : ''}>${rendered}</${tag}>`;
}

/** Inline markup: code, links, bare URLs, bold, italic. */
export function renderInline(text) {
  const held = [];
  const hold = (html) => `\uE002${held.push(html) - 1}\uE003`;

  let working = String(text).replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (_, ticks, code) => (
    hold(`<code>${escapeHtml(code.trim())}</code>`)));
  working = escapeHtml(working);

  working = working.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => (
    hold(`<a href="${url}" target="_blank" rel="noopener noreferrer">${emphasis(label)}</a>`)));
  working = working.replace(/(^|[\s(„“"«])(https?:\/\/[^\s<>]+)/g, (_, lead, url) => {
    const trailing = /[.,;:!?)\]»“”]+$/.exec(url)?.[0] ?? '';
    const clean = trailing ? url.slice(0, -trailing.length) : url;
    return lead + hold(`<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean}</a>`) + trailing;
  });

  working = emphasis(working);
  return working.replace(/\uE002(\d+)\uE003/g, (_, position) => held[Number(position)]);
}

function emphasis(escaped) {
  return escaped
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
    .replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![*\w])/g, '$1<em>$2</em>')
    .replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>');
}
