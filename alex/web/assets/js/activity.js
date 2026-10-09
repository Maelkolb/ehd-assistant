/** The research trail of one answer: what Alex thought and which tools it
    called, shown live and folded away once the answer is complete. */

import { h, clear, truncate } from './dom.js';
import { icon, toolIcon } from './icons.js';
import { t, toolLabel, docTypeLabel, kindLabel } from './i18n.js';

const pick = (input, ...names) => {
  for (const name of names) {
    if (input && input[name] !== undefined && input[name] !== null && input[name] !== '') return input[name];
  }
  return undefined;
};

const asList = (value) => {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed;
    } catch { /* plain string */ }
    return value ? [value] : [];
  }
  return [];
};

function dateRange(input) {
  const from = pick(input, 'date_from');
  const to = pick(input, 'date_to');
  if (!from && !to) return '';
  return from && to && from !== to ? `${from} – ${to}` : String(from || to);
}

function filters(input) {
  const parts = [];
  const types = asList(pick(input, 'doc_types', 'doc_type'));
  if (types.length) parts.push(types.map(docTypeLabel).join(', '));
  const range = dateRange(input);
  if (range) parts.push(range);
  const docId = pick(input, 'doc_id');
  if (docId) parts.push(String(docId));
  return parts;
}

/** A short human-readable line for a tool call's arguments. */
export function summarizeInput(name, input = {}) {
  const quoted = (value) => `„${truncate(value, 80)}“`;
  switch (name) {
    case 'search':
      return [quoted(pick(input, 'query') ?? ''), ...filters(input)].join(', ');
    case 'grep':
      return [`/${truncate(pick(input, 'pattern') ?? '', 80)}/`, ...filters(input)].join(', ');
    case 'read_document': {
      const parts = [String(pick(input, 'doc_id') ?? '')];
      const from = pick(input, 'page_from', 'from_page', 'page');
      const to = pick(input, 'page_to', 'to_page');
      if (from) parts.push(to && to !== from ? `${from}–${to}` : String(from));
      const section = pick(input, 'section');
      if (section) parts.push(truncate(section, 40));
      const start = pick(input, 'start', 'offset');
      if (start !== undefined && !from) parts.push(`ab ${start}`);
      return parts.join(', ');
    }
    case 'list_documents': {
      const parts = filters(input);
      const person = pick(input, 'person', 'correspondent');
      if (person) parts.push(String(person));
      const title = pick(input, 'title_contains', 'title');
      if (title) parts.push(quoted(title));
      return parts.join(', ');
    }
    case 'lookup_entity': {
      const kind = pick(input, 'kind', 'entity_type', 'type');
      return quoted(pick(input, 'name', 'query') ?? '') + (kind ? ` (${kindLabel(kind)})` : '');
    }
    case 'entity_mentions':
      return [String(pick(input, 'entity_id', 'entity') ?? ''), ...filters(input)].join(', ');
    case 'chronology':
      return dateRange(input);
    case 'query_data':
      return truncate(String(pick(input, 'sql', 'query') ?? '').replace(/\s+/g, ' '), 90);
    case 'run_python': {
      const code = String(pick(input, 'code', 'source') ?? '');
      const lead = code.split('\n').map((line) => line.trim()).find((line) => line);
      return truncate((lead || '').replace(/^#\s*/, ''), 90);
    }
    case 'create_map':
      return `${quoted(pick(input, 'title') ?? '')} (${asList(pick(input, 'stations')).length})`;
    case 'create_timeline':
      return `${quoted(pick(input, 'title') ?? '')} (${asList(pick(input, 'events')).length})`;
    case 'create_network': {
      const nodes = asList(pick(input, 'nodes'));
      const focus = pick(input, 'focus', 'entity_id');
      if (nodes.length) return `${quoted(pick(input, 'title') ?? '')} (${nodes.length})`;
      return focus ? quoted(focus) : quoted(pick(input, 'title') ?? '');
    }
    case 'dated_passages':
      return [dateRange(input), ...filters({ doc_types: pick(input, 'doc_types'), doc_id: pick(input, 'doc_id') })]
        .filter(Boolean).join(', ');
    case 'entities_in_document': {
      const parts = [String(pick(input, 'doc_id') ?? '')];
      const from = pick(input, 'page_from');
      const to = pick(input, 'page_to');
      if (from) parts.push(to && to !== from ? `${from}–${to}` : String(from));
      const kinds = asList(pick(input, 'kinds'));
      if (kinds.length) parts.push(kinds.map(kindLabel).join(', '));
      return parts.join(', ');
    }
    case 'itinerary':
      return pick(input, 'doc_id') ? String(pick(input, 'doc_id')) : dateRange(input);
    default:
      return truncate(JSON.stringify(input), 90);
  }
}

function inputDetail(name, input) {
  if (name === 'run_python') return { label: 'activity.code', text: pick(input, 'code', 'source') };
  if (name === 'query_data') return { label: 'activity.sql', text: pick(input, 'sql', 'query') };
  return null;
}

export class ActivityFeed {
  constructor({ onSource }) {
    this.onSource = onSource;
    this.steps = new Map();
    this.stepCount = 0;
    this.currentThought = null;
    this.result = null;
    this.title = h('span', { class: 'activity-title', 'data-i18n': 'activity.title', text: t('activity.title') });
    this.meta = h('span', { class: 'activity-meta' });
    this.list = h('ol', { class: 'steps' });
    this.element = h('details', { class: 'activity', open: true, hidden: true },
      h('summary', {}, icon('chevronRight', 'activity-caret'), this.title, this.meta),
      this.list);
    this.updateMeta();
  }

  get isEmpty() {
    return !this.list.childElementCount;
  }

  reveal() {
    this.element.hidden = false;
  }

  thinking(text) {
    if (!text) return;
    this.reveal();
    if (!this.currentThought) {
      const body = h('p', { class: 'thought-text' });
      const item = h('li', { class: 'step step-thought' },
        h('span', { class: 'step-icon' }, icon('thought')),
        h('div', { class: 'step-main' }, body));
      body.addEventListener('click', () => body.classList.toggle('is-open'));
      this.list.append(item);
      this.currentThought = body;
    }
    this.currentThought.textContent += text;
  }

  endThought() {
    if (this.currentThought && !this.currentThought.textContent.trim()) {
      this.currentThought.closest('li').remove();
    }
    this.currentThought = null;
  }

  toolCall(event) {
    this.reveal();
    this.endThought();
    this.stepCount += 1;
    const name = h('span', { class: 'step-name', text: toolLabel(event.name) });
    const args = h('span', { class: 'step-args', text: summarizeInput(event.name, event.input) });
    const result = h('div', { class: 'step-result', text: t('activity.running') });
    const main = h('div', { class: 'step-main' }, h('div', { class: 'step-line' }, name, args), result);
    const detail = inputDetail(event.name, event.input);
    if (detail && detail.text) {
      main.append(h('details', { class: 'step-detail' },
        h('summary', { 'data-i18n': detail.label, text: t(detail.label) }),
        h('pre', {}, h('code', { text: String(detail.text) }))));
    }
    const item = h('li', { class: 'step step-tool is-running' },
      h('span', { class: 'step-icon' }, toolIcon(event.name)), main);
    this.list.append(item);
    this.steps.set(event.id, { item, name, args, result, main, toolName: event.name, input: event.input, done: false });
    this.updateMeta();
  }

  toolResult(event) {
    const step = this.steps.get(event.id);
    if (!step) return;
    step.done = true;
    step.ok = event.ok !== false;
    step.summary = event.summary || '';
    step.item.classList.remove('is-running');
    step.item.classList.toggle('is-failed', !step.ok);
    step.result.textContent = step.summary || (step.ok ? '' : t('activity.failed'));
    step.result.hidden = !step.result.textContent;
    const sources = event.sources || [];
    if (sources.length) {
      const list = h('ul', { class: 'step-sources' });
      for (const source of sources.slice(0, 8)) {
        const label = [source.label, truncate(source.title || '', 46)].filter(Boolean).join(', ');
        list.append(h('li', {}, h('button', {
          type: 'button', class: 'step-source', text: label || source.id,
          onClick: () => this.onSource(source),
        })));
      }
      if (sources.length > 8) list.append(h('li', { class: 'step-more', text: `+${sources.length - 8}` }));
      step.main.append(list);
    }
  }

  /** Mark the trail finished and fold it away. */
  finish({ elapsed, model, keepOpen = false } = {}) {
    this.endThought();
    for (const step of this.steps.values()) {
      if (!step.done) {
        step.item.classList.remove('is-running');
        step.result.hidden = true;
      }
    }
    this.result = { elapsed, model };
    this.updateMeta();
    if (!keepOpen) this.element.open = false;
    if (this.isEmpty) this.element.hidden = true;
  }

  updateMeta() {
    const count = this.stepCount;
    const steps = count === 1 ? t('activity.stepsOne') : t('activity.stepsMany', { steps: count });
    if (this.result && this.result.elapsed !== undefined && this.result.elapsed !== null) {
      this.meta.textContent = t('activity.summary', {
        steps, seconds: Math.round(this.result.elapsed), model: this.result.model || '' })
        .replace(/, $/, '');
    } else {
      this.meta.textContent = count ? steps : '';
    }
  }

  refresh() {
    for (const step of this.steps.values()) {
      step.name.textContent = toolLabel(step.toolName);
      step.args.textContent = summarizeInput(step.toolName, step.input);
    }
    this.updateMeta();
  }

  destroy() {
    clear(this.list);
    this.steps.clear();
  }
}
