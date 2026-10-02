/** Turn a passage of the edition into DOM: paragraphs, table rows, page
    badges, Humboldt's marginal notes, note anchors and the cited span. */

import { h } from './dom.js';
import { t } from './i18n.js';

const PAGE_MARK = /⟦[^⟧]*⟧ ?/g;

/** Whitespace-collapsed copy of `text` with a map back to original offsets. */
function squash(text) {
  const characters = [];
  const map = [];
  let pendingSpace = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (/\s/.test(character)) {
      pendingSpace = characters.length > 0;
      continue;
    }
    if (pendingSpace) {
      characters.push(' ');
      map.push(index - 1);
      pendingSpace = false;
    }
    characters.push(character);
    map.push(index);
  }
  return { text: characters.join(''), map };
}

/**
 * Find a quoted span in a passage. The quote may carry ⟦page⟧ marks and
 * different whitespace; long quotes may only match at their head and tail.
 * @returns {[number, number] | null} offsets into `text`
 */
export function findQuote(text, quote) {
  const needle = squash(String(quote || '').replace(PAGE_MARK, ' ')).text;
  if (needle.length < 3) return null;
  const hay = squash(text);
  const range = (from, length) => [hay.map[from], hay.map[from + length - 1] + 1];

  let at = hay.text.indexOf(needle);
  if (at < 0) at = hay.text.toLowerCase().indexOf(needle.toLowerCase());
  if (at >= 0) return range(at, needle.length);

  if (needle.length > 90) {
    const head = hay.text.indexOf(needle.slice(0, 40));
    const tail = hay.text.indexOf(needle.slice(-40), Math.max(head, 0));
    if (head >= 0 && tail > head) return range(head, tail + 40 - head);
    if (head >= 0) return range(head, Math.min(needle.length, hay.text.length - head));
  }
  return null;
}

/** Ranges of Humboldt's own marginal additions, written ⟨…⟩ (they can nest). */
function marginalRanges(text) {
  const ranges = [];
  const open = [];
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '⟨') open.push(index);
    else if (text[index] === '⟩' && open.length) {
      const start = open.pop();
      if (!open.length) ranges.push([start, index + 1]);
    }
  }
  return ranges;
}

/**
 * @param {object} options
 * @param {string} options.text
 * @param {number} [options.offset]      document offset of text[0]
 * @param {string} [options.page]        page label in force at the start
 * @param {Array}  [options.pages]       page breaks inside the text (document offsets)
 * @param {Array}  [options.notes]       notes anchored inside the text (document offsets)
 * @param {Array<[number, number]>} [options.highlights]  ranges relative to text
 * @param {[number, number]} [options.focus]              one emphasised anchor range
 * @param {(index: number) => void} [options.onNote]
 */
export function renderPassage({ text, offset = 0, page = '', pages = [], notes = [],
                                highlights = [], focus = null, onNote = null }) {
  const container = h('div', { class: 'passage' });
  const marks = [
    ...highlights.map(([start, end]) => ({ start, end, kind: 'hl' })),
    ...marginalRanges(text).map(([start, end]) => ({ start, end, kind: 'marginal' })),
    ...notes.map((note, index) => ({
      start: note.anchor_start - offset, end: note.anchor_end - offset, kind: 'anchor', index }))
      .filter((mark) => mark.end > mark.start && mark.start >= 0 && mark.end <= text.length),
  ];
  if (focus) marks.push({ start: focus[0], end: focus[1], kind: 'focus' });

  const points = [];
  if (page) points.push({ at: 0, kind: 'page', label: page });
  for (const pageBreak of pages) {
    let at = pageBreak.start - offset;
    while (at > 0 && at < text.length && /\s/.test(text[at])) at += 1;
    if (at > 0 && at < text.length) {
      points.push({ at, kind: 'page', label: pageBreak.label, facsimile: pageBreak.facsimile });
    }
  }
  notes.forEach((note, index) => {
    const at = Math.max(note.anchor_end, note.anchor_start) - offset;
    if (at >= 0 && at <= text.length) points.push({ at, kind: 'note', index });
  });
  points.sort((a, b) => a.at - b.at || (a.kind === 'note' ? -1 : 1));

  function pointNode(point) {
    if (point.kind === 'page') {
      const label = t('panel.page', { page: point.label });
      if (point.facsimile && /^https?:\/\//.test(point.facsimile)) {
        return h('a', { class: 'page-mark', href: point.facsimile, target: '_blank',
                        rel: 'noopener noreferrer', title: `${label} – ${t('panel.facsimile')}`,
                        text: point.label });
      }
      return h('span', { class: 'page-mark', title: label, text: point.label });
    }
    return h('button', { type: 'button', class: 'note-ref', text: String(point.index + 1),
                         'aria-label': `${t('panel.note.editorial')} ${point.index + 1}`,
                         onClick: () => onNote?.(point.index) });
  }

  /** Nodes for text[from:to] with marks and points applied. */
  function inline(from, to, isLast) {
    const nodes = [];
    const edges = new Set([from, to]);
    for (const mark of marks) {
      if (mark.start > from && mark.start < to) edges.add(mark.start);
      if (mark.end > from && mark.end < to) edges.add(mark.end);
    }
    const inside = points.filter((point) => (
      point.at >= from && (point.at < to || (point.at === to && (point.kind === 'note' || isLast)))));
    for (const point of inside) edges.add(Math.min(point.at, to));
    const sorted = [...edges].sort((a, b) => a - b);
    for (let index = 0; index < sorted.length; index += 1) {
      const start = sorted[index];
      for (const point of inside) if (point.at === start) nodes.push(pointNode(point));
      if (index === sorted.length - 1) break;
      const end = sorted[index + 1];
      const piece = text.slice(start, end);
      if (!piece) continue;
      const active = marks.filter((mark) => mark.start <= start && mark.end >= end).map((mark) => mark.kind);
      if (!active.length) {
        nodes.push(document.createTextNode(piece));
        continue;
      }
      const classes = active.filter((kind) => kind !== 'hl').map((kind) => `p-${kind}`).join(' ');
      nodes.push(h(active.includes('hl') ? 'mark' : 'span', { class: classes || null, text: piece }));
    }
    return nodes;
  }

  const paragraphPattern = /[^\n]+(?:\n(?!\n)[^\n]*)*/g;
  const blocks = [...text.matchAll(paragraphPattern)];
  blocks.forEach((match, blockIndex) => {
    const blockStart = match.index;
    const isLastBlock = blockIndex === blocks.length - 1;
    const lines = [];
    let cursor = blockStart;
    for (const line of match[0].split('\n')) {
      lines.push({ start: cursor, end: cursor + line.length, text: line });
      cursor += line.length + 1;
    }
    const tabular = lines.length > 1 && lines.filter((line) => line.text.includes(' | ')).length >= lines.length * 0.6;
    if (tabular) {
      const table = h('table', { class: 'passage-table' });
      lines.forEach((line, lineIndex) => {
        const row = h('tr');
        let cellStart = line.start;
        let rowText = line.text;
        if (rowText.startsWith('| ')) {
          // the edition's row begins with an empty cell
          row.append(h('td'));
          rowText = rowText.slice(2);
          cellStart += 2;
        }
        const cells = rowText.split(' | ');
        cells.forEach((cell, cellIndex) => {
          const cellEnd = cellStart + cell.length;
          const trailing = cellIndex === cells.length - 1;
          const content = trailing ? cell.replace(/\s*\|\s*$/, '') : cell;
          if (!(trailing && !content.trim() && cells.length > 1)) {
            row.append(h('td', {}, inline(cellStart, cellStart + content.length,
              isLastBlock && lineIndex === lines.length - 1 && trailing)));
          }
          cellStart = cellEnd + 3;
        });
        table.append(row);
      });
      container.append(h('div', { class: 'table-scroll' }, table));
    } else {
      const paragraph = h('p');
      lines.forEach((line, lineIndex) => {
        if (lineIndex) paragraph.append(h('br'));
        paragraph.append(...inline(line.start, line.end, isLastBlock && lineIndex === lines.length - 1));
      });
      container.append(paragraph);
    }
  });
  return container;
}
