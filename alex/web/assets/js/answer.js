/** One question and Alex's answer to it: research trail, cited prose,
    visualisations, source list, actions and follow-up questions. */

import { h, clear, append, copyText, truncate } from './dom.js';
import { icon } from './icons.js';
import { t, docTypeLabel, formatDate } from './i18n.js';
import { renderMarkdown } from './markdown.js';
import { ActivityFeed } from './activity.js';
import { ArtifactList } from './artifacts/index.js';
import { sendFeedback } from './api.js';
import { shareUrl } from './share.js';
import { DOCTYPE_SLOTS } from './theme.js';

const MARKER = /\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]/g;
const PARTIAL_MARKER = /\[\s*(?:S\d*(?:\s*[,;]\s*S?\d*)*)?$/;
const PLACEHOLDER = /\uE000(\d+)\uE001/g;

/**
 * Markdown for a list of answer blocks, with a placeholder wherever a
 * citation chip belongs, and the citation each placeholder stands for.
 */
export function composeAnswer(blocks, numberFor, streaming = false) {
  const references = [];
  const placeholder = (reference) => `\uE000${references.push(reference) - 1}\uE001`;
  const parts = blocks.map((block, position) => {
    let text = block.text || '';
    if (streaming && position === blocks.length - 1) text = text.replace(PARTIAL_MARKER, '');
    text = text.replace(MARKER, (_, ids) => ids.split(/\s*[,;]\s*/).map((id) => (
      placeholder({ sourceId: id, n: numberFor(id), citedTexts: [], inNote: false }))).join(''));

    const grouped = new Map();
    for (const citation of block.citations || []) {
      const reference = grouped.get(citation.source_id) || {
        sourceId: citation.source_id, n: citation.n ?? numberFor(citation.source_id),
        citedTexts: [], inNote: Boolean(citation.in_note) };
      if (citation.cited_text) reference.citedTexts.push(citation.cited_text);
      grouped.set(citation.source_id, reference);
    }
    if (grouped.size && text.trim()) {
      const chips = [...grouped.values()].map(placeholder).join('');
      const tail = /(\s*\|)?\s*$/.exec(text);
      text = text.slice(0, tail.index) + chips + tail[0];
    }
    return text;
  });
  return { markdown: parts.join(''), references };
}

function chipsInto(html, references) {
  return html.replace(PLACEHOLDER, (_, position) => {
    const reference = references[Number(position)];
    if (!reference || reference.n === undefined) return '';
    return `<button type="button" class="cite" data-ref="${Number(position)}" aria-label="${
      t('cite.open', { n: reference.n })}">${reference.n}</button>`;
  });
}

/* One preview bubble for the whole page: what a citation chip points at,
   shown while the chip is hovered or focused. */
let preview = null;
let previewChip = null;

function placePreview() {
  if (!preview || preview.hidden || !previewChip) return;
  const rect = previewChip.getBoundingClientRect();
  if (rect.bottom < 0 || rect.top > window.innerHeight) {
    hidePreview();
    return;
  }
  const width = preview.offsetWidth;
  const height = preview.offsetHeight;
  const gutter = 8;
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(gutter, Math.min(left, window.innerWidth - width - gutter));
  const below = rect.bottom + 8 + height <= window.innerHeight - gutter || rect.top - height - 8 < gutter;
  const top = below ? rect.bottom + 8 : rect.top - height - 8;
  preview.style.left = `${Math.round(left)}px`;
  preview.style.top = `${Math.round(top)}px`;
  preview.classList.toggle('is-above', !below);
}

function sourceKindText(source) {
  if (source.doc_type) return docTypeLabel(source.doc_type);
  const key = { register: 'panel.register', data: 'panel.data', computation: 'panel.computation' }[source.kind];
  return key ? t(key) : '';
}

export function showPreview(chip, source, reference) {
  if (!preview) {
    preview = h('div', { class: 'cite-preview', id: 'cite-preview', role: 'tooltip', hidden: true });
    document.body.append(preview);
    window.addEventListener('scroll', placePreview, { capture: true, passive: true });
    window.addEventListener('resize', placePreview, { passive: true });
  }
  previewChip = chip;
  const quote = (reference.citedTexts || []).find((text) => text && text.trim());
  const detail = [sourceKindText(source), source.doc_type ? source.label : '',
    ['letter', 'document'].includes(source.doc_type) && source.date ? formatDate(source.date) : '']
    .filter(Boolean).join(', ');
  clear(preview);
  append(preview, [
    h('span', { class: 'cite-preview-title', text: truncate(source.title || source.label || '', 120) }),
    detail ? h('span', { class: 'cite-preview-detail', text: detail }) : null,
    quote ? h('q', { class: 'cite-preview-quote', text: truncate(quote.replace(/⟦[^⟧]*⟧ ?/g, '').replace(/\s+/g, ' ').trim(), 260) }) : null,
    h('span', { class: 'cite-preview-hint', text: t('cite.previewHint') }),
  ]);
  preview.hidden = false;
  chip.setAttribute('aria-describedby', 'cite-preview');
  placePreview();
}

export function hidePreview() {
  if (preview) preview.hidden = true;
  previewChip = null;
}

function textButton(iconName, labelKey, onClick) {
  return h('button', { type: 'button', class: 'text-button', onClick }, icon(iconName),
    h('span', { 'data-i18n': labelKey, text: t(labelKey) }));
}

export class TurnView {
  /**
   * @param {object} options
   * @param {string} options.query
   * @param {object} options.context  { config, panel, onAsk(question), onRetry(view), onUpdate() }
   */
  constructor({ query, context }) {
    this.query = query;
    this.context = context;
    this.blocks = [];
    this.sources = new Map();
    this.numbers = new Map();
    this.references = [];
    this.answer = null;
    this.requestId = null;
    this.model = null;
    this.state = 'working';
    this.frame = 0;
    this.suggestions = [];

    this.activity = new ActivityFeed({ onSource: (source) => this.openSource(source, {}) });
    this.artifacts = new ArtifactList({ config: context.config });
    this.status = h('p', { class: 'turn-status' }, h('span', { class: 'pulse', 'aria-hidden': 'true' }),
      h('span', { 'data-i18n': 'status.thinking', text: t('status.thinking') }));
    this.prose = h('div', { class: 'prose' });
    this.notice = h('div', { class: 'turn-notice', hidden: true });
    this.sourceList = h('section', { class: 'answer-sources', hidden: true });
    this.actions = h('div', { class: 'answer-actions', hidden: true });
    this.followups = h('div', { class: 'followups', hidden: true });

    this.prose.addEventListener('click', (event) => {
      const chip = event.target.closest('button.cite');
      if (!chip) return;
      hidePreview();
      const reference = this.references[Number(chip.dataset.ref)];
      const source = reference && this.sourceFor(reference.sourceId);
      if (source) this.openSource(source, reference);
    });
    for (const type of ['mouseover', 'focusin']) {
      this.prose.addEventListener(type, (event) => {
        const chip = event.target.closest('button.cite');
        if (!chip) return;
        const reference = this.references[Number(chip.dataset.ref)];
        const source = reference && this.sourceFor(reference.sourceId);
        if (source) showPreview(chip, source, reference);
      });
    }
    this.prose.addEventListener('mouseout', (event) => {
      if (event.target.closest('button.cite')) hidePreview();
    });
    this.prose.addEventListener('focusout', (event) => {
      if (event.target.closest('button.cite')) hidePreview();
    });

    this.element = h('article', { class: 'turn' },
      h('div', { class: 'question' },
        h('h2', { class: 'visually-hidden', 'data-i18n': 'turn.you', text: t('turn.you') }),
        h('p', { text: query })),
      h('div', { class: 'response' },
        h('h2', { class: 'visually-hidden', 'data-i18n': 'turn.alex', text: t('turn.alex') }),
        this.activity.element, this.status, this.prose, this.notice, this.artifacts.element,
        this.sourceList, this.actions, this.followups));
  }

  get text() {
    return this.answer?.text ?? this.blocks.map((block) => block.text).join('');
  }

  sourceFor(sourceId) {
    return this.answer?.sources?.find((source) => source.id === sourceId) || this.sources.get(sourceId);
  }

  numberFor(sourceId) {
    if (!this.numbers.has(sourceId)) this.numbers.set(sourceId, this.numbers.size + 1);
    return this.numbers.get(sourceId);
  }

  openSource(source, reference) {
    this.context.panel.open(source, { citedTexts: reference.citedTexts || [], inNote: reference.inNote });
  }

  /** Apply one stream event. */
  handle(event) {
    switch (event.type) {
      case 'start':
        this.requestId = event.request_id;
        this.model = event.model;
        break;
      case 'thinking':
        this.activity.thinking(event.text);
        break;
      case 'tool_call':
        this.activity.toolCall(event);
        this.setStatus('status.thinking');
        break;
      case 'tool_result':
        for (const source of event.sources || []) this.sources.set(source.id, source);
        this.activity.toolResult(event);
        break;
      case 'artifact':
        if (event.artifact) this.artifacts.upsert(event.artifact);
        break;
      case 'block':
        this.activity.endThought();
        this.blocks.push({ text: '', citations: [] });
        break;
      case 'text':
        this.activity.endThought();
        if (!this.blocks.length) this.blocks.push({ text: '', citations: [] });
        this.blocks[this.blocks.length - 1].text += event.text || '';
        this.setStatus('status.writing');
        this.schedule();
        break;
      case 'citation':
        if (!this.blocks.length) this.blocks.push({ text: '', citations: [] });
        this.blocks[this.blocks.length - 1].citations.push(event);
        this.schedule();
        break;
      case 'reset':
        this.blocks = [];
        this.numbers.clear();
        this.schedule();
        break;
      case 'done':
        this.complete(event.answer);
        break;
      case 'suggestions':
        this.suggestions = event.questions || [];
        this.renderFollowups();
        break;
      case 'error':
        this.fail(event.code, event.message);
        break;
      default:
        break;
    }
    this.context.onUpdate?.();
  }

  setStatus(key) {
    if (this.state !== 'working') return;
    const label = this.status.lastElementChild;
    if (label.dataset.i18n !== key) {
      label.dataset.i18n = key;
      label.textContent = t(key);
    }
    this.status.hidden = key === 'status.writing' && this.text.trim().length > 0;
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.renderProse();
      this.context.onUpdate?.();
    });
  }

  renderProse() {
    const final = Boolean(this.answer);
    const blocks = final ? this.answer.blocks || [{ text: this.answer.text || '', citations: [] }] : this.blocks;
    const { markdown, references } = composeAnswer(blocks, (id) => {
      const source = final ? this.answer.sources?.find((candidate) => candidate.id === id) : null;
      return source?.n ?? (final && this.answer.sources?.length ? undefined : this.numberFor(id));
    }, !final);
    this.references = references;
    this.prose.innerHTML = chipsInto(renderMarkdown(markdown), references);
    this.prose.classList.toggle('is-streaming', !final && this.state === 'working');
  }

  complete(answer) {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.answer = answer;
    this.state = 'done';
    this.status.hidden = true;
    if (answer.model) this.model = answer.model;
    this.activity.finish({ elapsed: answer.elapsed_s, model: this.model?.label });
    this.renderProse();
    if (!this.text.trim()) this.showNotice('answer.empty', false);
    this.artifacts.sync(answer.artifacts || []);
    this.renderSources();
    this.renderActions();
  }

  /** The user pressed stop: keep what was written. */
  stop() {
    if (this.state !== 'working') return;
    this.state = 'stopped';
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.status.hidden = true;
    this.activity.finish({ model: this.model?.label });
    this.renderProse();
    this.prose.classList.remove('is-streaming');
    this.showNotice('answer.stopped', true);
  }

  fail(code, message) {
    if (this.state === 'done') return;
    this.state = 'failed';
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.status.hidden = true;
    this.activity.finish({ model: this.model?.label, keepOpen: false });
    this.renderProse();
    this.prose.classList.remove('is-streaming');
    const key = { refused: 'error.refused', overloaded: 'error.overloaded', network: 'error.network',
                  rate: 'error.rate', unavailable: 'error.unavailable' }[code] || 'error.generic';
    const fromServer = ['rate', 'http', 'unavailable'].includes(code) && message && !/^HTTP \d+$/.test(message)
      ? message : '';
    clear(this.notice);
    append(this.notice, [
      h('div', { class: 'notice is-error', role: 'alert' }, icon('alert'),
        h('div', {},
          h('strong', { 'data-i18n': 'error.title', text: t('error.title') }),
          fromServer ? h('p', { text: fromServer }) : h('p', { 'data-i18n': key, text: t(key) })),
        code === 'refused' ? null : textButton('retry', 'answer.retry', () => this.context.onRetry(this)))]);
    this.notice.hidden = false;
  }

  showNotice(key, withRetry) {
    clear(this.notice);
    append(this.notice, [h('div', { class: 'notice' },
      h('p', { 'data-i18n': key, text: t(key) }),
      withRetry ? textButton('retry', 'answer.retry', () => this.context.onRetry(this)) : null)]);
    this.notice.hidden = false;
  }

  renderSources() {
    const sources = this.answer?.sources || [];
    clear(this.sourceList);
    this.sourceList.hidden = !sources.length;
    if (!sources.length) return;
    const items = sources.map((source) => {
      const quotes = this.references.filter((reference) => reference.sourceId === source.id)
        .flatMap((reference) => reference.citedTexts);
      const inNote = this.references.some((reference) => reference.sourceId === source.id && reference.inNote);
      const dot = h('span', { class: 'badge-dot' });
      const slot = DOCTYPE_SLOTS[source.doc_type];
      if (slot) dot.style.setProperty('--dot', `var(--viz-${slot})`);
      const kindText = source.doc_type ? docTypeLabel(source.doc_type)
        : t({ register: 'panel.register', data: 'panel.data', computation: 'panel.computation' }[source.kind]
            || 'panel.title');
      const dated = ['letter', 'document'].includes(source.doc_type) && source.date;
      const detail = [kindText, source.doc_type ? source.label : '', dated ? formatDate(source.date) : '']
        .filter(Boolean).join(', ');
      return h('li', {}, h('button', { type: 'button', class: 'source-row',
                                       onClick: () => this.openSource(source, { citedTexts: quotes, inNote }) },
        h('span', { class: 'source-number', text: String(source.n ?? '') }),
        h('span', { class: 'source-text' },
          h('span', { class: 'source-title', text: truncate(source.title || source.label || source.id, 110) }),
          h('span', { class: 'source-detail' }, dot, detail))));
    });
    append(this.sourceList, [
      h('h3', {}, h('span', { 'data-i18n': 'answer.sources', text: t('answer.sources') })),
      h('ol', { class: 'source-list' }, items)]);
  }

  renderActions() {
    clear(this.actions);
    if (!this.text.trim()) return;
    const flash = (button, key, original) => {
      const label = button.lastElementChild;
      label.textContent = t(key);
      button.classList.add('is-done');
      setTimeout(() => {
        label.textContent = t(original);
        button.classList.remove('is-done');
      }, 1800);
    };
    const copy = textButton('copy', 'answer.copy', async () => {
      if (await copyText(this.plainText())) flash(copy, 'answer.copied', 'answer.copy');
    });
    const share = textButton('link', 'answer.share', async () => {
      try {
        if (await copyText(await shareUrl(this.query, this.answer))) flash(share, 'answer.shared', 'answer.share');
      } catch (error) {
        console.warn(error);
      }
    });
    const nodes = [copy, share];
    if (this.context.config?.features?.feedback && this.requestId) {
      const thanks = h('span', { class: 'feedback-thanks', role: 'status' });
      const vote = (rating, button, other) => async () => {
        button.setAttribute('aria-pressed', 'true');
        other.setAttribute('aria-pressed', 'false');
        thanks.textContent = t('answer.feedbackThanks');
        try { await sendFeedback(this.requestId, rating); } catch { /* feedback is best effort */ }
      };
      const up = h('button', { type: 'button', class: 'icon-button', 'aria-pressed': 'false',
                               'data-i18n-title': 'answer.helpful', 'data-i18n-label': 'answer.helpful',
                               title: t('answer.helpful'), 'aria-label': t('answer.helpful') }, icon('thumbUp'));
      const down = h('button', { type: 'button', class: 'icon-button', 'aria-pressed': 'false',
                                 'data-i18n-title': 'answer.unhelpful', 'data-i18n-label': 'answer.unhelpful',
                                 title: t('answer.unhelpful'), 'aria-label': t('answer.unhelpful') }, icon('thumbDown'));
      up.addEventListener('click', vote('up', up, down));
      down.addEventListener('click', vote('down', down, up));
      nodes.push(h('span', { class: 'feedback' }, up, down, thanks));
    }
    append(this.actions, nodes);
    this.actions.hidden = false;
  }

  /** The answer as plain text with its sources, for the clipboard. */
  plainText() {
    const { markdown, references } = composeAnswer(this.answer?.blocks || this.blocks, (id) => (
      this.answer?.sources?.find((source) => source.id === id)?.n ?? this.numberFor(id)));
    const body = markdown.replace(PLACEHOLDER, (_, position) => `[${references[Number(position)].n}]`);
    const sources = (this.answer?.sources || []).map((source) => (
      `[${source.n}] ${[source.title, source.label].filter(Boolean).join(', ')}${source.url ? ` – ${source.url}` : ''}`));
    return sources.length ? `${body.trim()}\n\n${t('answer.sources')}:\n${sources.join('\n')}` : body.trim();
  }

  renderFollowups() {
    clear(this.followups);
    this.followups.hidden = !this.suggestions.length;
    if (!this.suggestions.length) return;
    append(this.followups, [
      h('h3', { 'data-i18n': 'answer.followups', text: t('answer.followups') }),
      h('ul', {}, this.suggestions.map((question) => h('li', {},
        h('button', { type: 'button', class: 'followup', onClick: () => this.context.onAsk(question) },
          icon('chevronRight'), h('span', { text: question })))))]);
  }

  /** Re-label everything after a language change. */
  refresh() {
    this.activity.refresh();
    if (this.answer) {
      this.renderProse();
      this.renderSources();
    }
  }

  destroy() {
    cancelAnimationFrame(this.frame);
    this.artifacts.destroy();
    this.element.remove();
  }
}
