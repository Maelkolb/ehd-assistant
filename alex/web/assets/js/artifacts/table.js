/** Table artifact (and the shared sortable table used in the source panel). */

import { h, download, slug, formatNumber } from '../dom.js';
import { t, getLanguage } from '../i18n.js';

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);
const isYear = (value) => Number.isInteger(value) && value >= 1000 && value <= 2100;

function display(value) {
  if (value === null || value === undefined) return '';
  if (isNumber(value)) {
    return Number.isInteger(value) && Math.abs(value) < 3000 ? String(value)
      : formatNumber(Math.round(value * 1000) / 1000, getLanguage());
  }
  return String(value);
}

export function toCsv(columns, rows) {
  const cell = (value) => {
    const text = value === null || value === undefined ? '' : String(value);
    return /[",\n;]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return '\uFEFF' + [columns, ...rows].map((row) => row.map(cell).join(',')).join('\r\n') + '\r\n';
}

/** A table whose header cells sort the rows; returns the scroll wrapper. */
export function sortableTable(columns, rows) {
  const values = (column) => rows.map((row) => row[column]).filter((value) => value !== null && value !== undefined);
  // Years are labels, not quantities: left-aligned and without digit grouping.
  const years = columns.map((_, column) => values(column).length > 0 && values(column).every(isYear));
  const numeric = columns.map((_, column) => !years[column] && values(column).length > 0
    && values(column).every(isNumber));
  const tbody = h('tbody');
  let sorted = { column: -1, direction: 1 };

  const paint = (ordered) => {
    tbody.replaceChildren(...ordered.map((row) => h('tr', {}, columns.map((_, column) => (
      h('td', { class: numeric[column] ? 'numeric' : null,
                text: years[column] ? String(row[column] ?? '') : display(row[column]) }))))));
  };

  const headers = columns.map((name, column) => {
    const label = String(name).replace(/_/g, ' ');
    const button = h('button', { type: 'button', class: 'sort-button',
                                 title: t('table.sort', { column: label }) }, label,
      h('span', { class: 'sort-mark', 'aria-hidden': 'true' }));
    const cell = h('th', { scope: 'col', class: numeric[column] ? 'numeric' : null, 'aria-sort': 'none' }, button);
    button.addEventListener('click', () => {
      const direction = sorted.column === column ? -sorted.direction : 1;
      sorted = { column, direction };
      const ordered = [...rows].sort((a, b) => {
        const left = a[column];
        const right = b[column];
        if (left === right) return 0;
        if (left === null || left === undefined) return 1;
        if (right === null || right === undefined) return -1;
        if (isNumber(left) && isNumber(right)) return (left - right) * direction;
        return String(left).localeCompare(String(right), getLanguage(), { numeric: true }) * direction;
      });
      headers.forEach((other) => other.setAttribute('aria-sort', 'none'));
      cell.setAttribute('aria-sort', direction > 0 ? 'ascending' : 'descending');
      paint(ordered);
    });
    return cell;
  });

  paint(rows);
  return h('div', { class: 'table-scroll', tabindex: '0' },
    h('table', { class: 'data-table' }, h('thead', {}, h('tr', {}, headers)), tbody));
}

export function renderTable(artifact, body, foot) {
  const columns = artifact.columns || [];
  const rows = artifact.rows || [];
  body.append(sortableTable(columns, rows));
  foot.append(h('span', { class: 'table-count' },
    t('table.rows', { count: formatNumber(rows.length, getLanguage()) }),
    artifact.truncated ? ` · ${t('table.truncated')}` : ''));
  return {
    downloadLabel: 'artifact.download.csv',
    download: () => download(`${slug(artifact.title || 'table')}.csv`, toCsv(columns, rows),
      'text/csv;charset=utf-8'),
  };
}
