/** Timeline artifact: dated events on one vertical line, grouped by year. */

import { h } from '../dom.js';
import { t, shortMonthDay, formatDate } from '../i18n.js';
import { exportStandalone } from './export.js';

const yearOf = (date) => /(\d{4})/.exec(date || '')?.[1] || '';

function sortKey(date) {
  const match = /(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?/.exec(date || '');
  if (!match) return '9999-99-99';
  return [match[1], (match[2] || '00').padStart(2, '0'), (match[3] || '00').padStart(2, '0')].join('-');
}

export function renderTimeline(artifact, body) {
  const events = [...(artifact.events || [])].filter((event) => event && event.label)
    .sort((a, b) => sortKey(a.date).localeCompare(sortKey(b.date)));
  const list = h('ol', { class: 'timeline' });
  let currentYear = null;
  for (const event of events) {
    const year = yearOf(event.date);
    if (year && year !== currentYear) {
      currentYear = year;
      list.append(h('li', { class: 'timeline-year' }, h('span', { text: year })));
    }
    const isIso = /^\d{4}(-\d{2}){0,2}$/.test(String(event.date || '').trim());
    const when = isIso ? shortMonthDay(event.date) : String(event.date || '');
    const item = h('li', { class: 'timeline-event' },
      h('time', { class: 'timeline-date', datetime: isIso ? event.date : null,
                  title: isIso ? formatDate(event.date) : null, text: when }),
      h('span', { class: 'timeline-dot', 'aria-hidden': 'true' }),
      h('div', { class: 'timeline-body' },
        h('div', { class: 'timeline-label', text: event.label }),
        event.description ? h('div', { class: 'timeline-description', text: event.description }) : null,
        event.url && /^https?:\/\//.test(event.url)
          ? h('a', { class: 'timeline-link', href: event.url, target: '_blank',
                     rel: 'noopener noreferrer', 'data-i18n': 'timeline.source', text: t('timeline.source') })
          : null));
    list.append(item);
  }
  body.append(list);
  return {
    downloadLabel: 'artifact.download.html',
    download: () => exportStandalone(artifact.title || t('artifact.type.timeline'), list.outerHTML),
  };
}
