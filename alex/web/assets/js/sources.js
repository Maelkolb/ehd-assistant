/** The source panel: shows what a citation points at — the passage in the
    edition with the cited span highlighted, a note, a register entry, a data
    query, a computation or an external Wikipedia extract. */

import { h, clear, append, copyText, trapFocus, truncate, formatNumber } from './dom.js';
import { icon } from './icons.js';
import { t, docTypeLabel, kindLabel, formatDate, getLanguage } from './i18n.js';
import { getPassage, getNote, getEntity } from './api.js';
import { renderPassage, findQuote } from './passage.js';
import { sortableTable } from './artifacts/table.js';
import { DOCTYPE_SLOTS, KIND_SLOTS } from './theme.js';

const WIDE = window.matchMedia('(min-width: 1180px)');
const NOTE_KINDS = new Set(['editorial', 'footnote', 'introduction']);

function badge(label, slot) {
  const node = h('span', { class: 'badge' }, h('span', { class: 'badge-dot' }), label);
  if (slot) node.style.setProperty('--dot', `var(--viz-${slot})`);
  return node;
}

function externalLink(href, label, className = 'panel-link') {
  if (!href || !/^https?:\/\//.test(href)) return null;
  return h('a', { class: className, href, target: '_blank', rel: 'noopener noreferrer' },
    label, icon('external'));
}

function copyButton(labelKey, text) {
  const button = h('button', { type: 'button', class: 'text-button' }, icon('copy'),
    h('span', { 'data-i18n': labelKey, text: t(labelKey) }));
  button.addEventListener('click', async () => {
    if (await copyText(text)) {
      button.classList.add('is-done');
      setTimeout(() => button.classList.remove('is-done'), 1600);
    }
  });
  return button;
}

export class SourcePanel {
  constructor({ embed = false } = {}) {
    this.embed = embed;
    this.element = document.getElementById('source-panel');
    this.backdrop = document.getElementById('panel-backdrop');
    this.heading = document.getElementById('panel-heading');
    this.kicker = document.getElementById('panel-kicker');
    this.meta = document.getElementById('panel-meta');
    this.body = document.getElementById('panel-body');
    this.foot = document.getElementById('panel-foot');
    this.shell = document.querySelector('.shell');
    this.opener = null;
    this.releaseFocus = null;
    this.current = null;
    this.token = 0;

    document.getElementById('panel-close').addEventListener('click', () => this.close());
    this.backdrop.addEventListener('click', () => this.close());
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.isOpen && !document.querySelector('dialog[open]')) {
        event.preventDefault();
        this.close();
      }
    });
    WIDE.addEventListener('change', () => this.isOpen && this.applyMode());
  }

  get isOpen() {
    return !this.element.hidden;
  }

  get docked() {
    return WIDE.matches && !this.embed;
  }

  applyMode() {
    this.shell.classList.toggle('has-panel', this.isOpen && this.docked);
    this.backdrop.hidden = !this.isOpen || this.docked;
    this.element.setAttribute('aria-modal', this.docked ? 'false' : 'true');
    this.releaseFocus?.();
    this.releaseFocus = this.isOpen && !this.docked ? trapFocus(this.element) : null;
  }

  /**
   * @param {object} source  a Source from docs/API.md
   * @param {{citedTexts?: string[], inNote?: boolean}} citation
   */
  open(source, citation = {}) {
    if (!this.isOpen) this.opener = document.activeElement;
    this.current = { source, citation };
    this.element.hidden = false;
    this.applyMode();
    this.render();
    this.element.focus({ preventScroll: true });
  }

  close() {
    if (!this.isOpen) return;
    this.element.hidden = true;
    this.current = null;
    this.token += 1;
    this.applyMode();
    if (this.opener && document.contains(this.opener)) this.opener.focus({ preventScroll: true });
    this.opener = null;
  }

  /** Re-render after a language change. */
  refresh() {
    if (this.isOpen && this.current) this.render();
  }

  setHeader(kicker, title, meta) {
    clear(this.kicker).append(kicker || '');
    this.heading.textContent = title || t('panel.title');
    this.meta.textContent = meta || '';
    this.meta.hidden = !meta;
  }

  loading() {
    this.fill(h('p', { class: 'panel-status', 'data-i18n': 'panel.loading', text: t('panel.loading') }));
    clear(this.foot);
    this.foot.hidden = true;
  }

  failed() {
    this.fill(h('p', { class: 'panel-status is-error' }, icon('alert'),
      h('span', { 'data-i18n': 'panel.failed', text: t('panel.failed') })));
  }

  fill(...nodes) {
    append(clear(this.body), nodes);
  }

  setFoot(...nodes) {
    const present = nodes.flat().filter(Boolean);
    clear(this.foot).append(...present);
    this.foot.hidden = !present.length;
  }

  async render() {
    const { source, citation } = this.current;
    const token = ++this.token;
    const stale = () => token !== this.token;
    this.body.scrollTop = 0;
    try {
      if (source.kind === 'data') this.renderData(source);
      else if (source.kind === 'computation') this.renderComputation(source);
      else if (source.kind === 'wikipedia') this.renderWikipedia(source, citation);
      else if (source.kind === 'register') await this.renderRegister(source, stale);
      else if (NOTE_KINDS.has(source.kind) && source.note_id !== undefined && source.note_id !== null) {
        await this.renderNote(source, citation, stale);
      } else await this.renderText(source, citation, stale);
    } catch (error) {
      if (stale()) return;
      console.error(error);
      this.failed();
    }
  }

  documentHeader(source, document, pageLabel) {
    const docType = document?.doc_type || source.doc_type;
    const title = document?.title || source.title || source.doc_id || '';
    const date = document ? [document.date_from, document.date_to].filter(Boolean) : [];
    const dateText = date.length ? formatDate(date[0] === date[1] || !date[1] ? date[0] : date.join('/'))
      : (source.date ? formatDate(source.date) : '');
    const page = pageLabel || source.page;
    const dated = docType === 'letter' || docType === 'document';
    const meta = [page ? t('panel.page', { page }) : '', dated ? dateText : '']
      .filter(Boolean).join(' · ');
    this.setHeader(badge(docTypeLabel(docType), DOCTYPE_SLOTS[docType]), title, meta);
  }

  citationBlock(document) {
    if (!document?.citation) return null;
    return h('section', { class: 'panel-section panel-citation' },
      h('h3', { 'data-i18n': 'panel.citation', text: t('panel.citation') }),
      h('p', { text: document.citation }),
      copyButton('panel.copyCitation', document.citation));
  }

  shelfmark(document) {
    const parts = [document?.repository, document?.shelfmark].filter(Boolean);
    return parts.length ? h('p', { class: 'panel-shelfmark', text: parts.join(', ') }) : null;
  }

  notesSection(notes, highlightQuotes, open) {
    if (!notes.length) return { section: null, focusNote: () => {} };
    const footnotesOnly = notes.every((note) => note.kind === 'footnote');
    const items = notes.map((note, index) => {
      const ranges = (highlightQuotes || []).map((quote) => findQuote(note.text, quote)).filter(Boolean);
      const item = h('li', { class: 'note', id: `panel-note-${index + 1}`, tabindex: '-1' },
        h('span', { class: 'note-number', text: String(index + 1) }),
        h('div', { class: 'note-text' },
          renderPassage({ text: note.text, highlights: ranges }).childNodes));
      return item;
    });
    const key = footnotesOnly ? 'panel.footnotes' : 'panel.notes';
    const section = h('details', { class: 'panel-section panel-notes', open },
      h('summary', {}, h('span', { 'data-i18n': key, text: t(key) }), ` (${notes.length})`),
      h('ol', { class: 'note-list' }, items));
    return {
      section,
      focusNote: (index) => {
        section.open = true;
        items[index]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        items[index]?.focus({ preventScroll: true });
      },
    };
  }

  async renderText(source, citation, stale) {
    this.documentHeader(source, null, source.page);
    this.loading();
    let window = { start: source.start, end: source.end };
    const quotes = citation.citedTexts || [];

    const show = async (keepScroll) => {
      const passage = await getPassage(source.doc_id, window.start, window.end);
      if (stale()) return;
      this.documentHeader(source, passage.document, passage.page);
      const inNote = Boolean(citation.inNote);
      const highlights = inNote ? [] : quotes.map((quote) => findQuote(passage.text, quote)).filter(Boolean);
      const notes = this.notesSection(passage.notes || [], inNote ? quotes : [],
        inNote || (passage.notes || []).length <= 3);
      const text = renderPassage({
        text: passage.text, offset: passage.start, page: passage.page, pages: passage.pages || [],
        notes: passage.notes || [], highlights, onNote: notes.focusNote,
      });
      const more = (direction, offset) => {
        if (offset === null || offset === undefined) return null;
        const key = `panel.more.${direction}`;
        return h('button', { type: 'button', class: 'more-button', onClick: async () => {
          window = direction === 'before' ? { start: offset, end: passage.end } : { start: passage.start, end: offset };
          await show(true).catch(() => this.failed());
        } }, icon(direction === 'before' ? 'chevronUp' : 'chevronDown'),
        h('span', { 'data-i18n': key, text: t(key) }));
      };
      const scroll = this.body.scrollTop;
      this.fill(
        highlights.length || (inNote && quotes.length)
          ? h('p', { class: 'cited-hint' }, h('span', { class: 'cited-swatch' }),
            h('span', { 'data-i18n': 'panel.cited', text: t('panel.cited') }))
          : null,
        more('before', passage.before),
        h('article', { class: 'paper', lang: 'mul' }, text),
        more('after', passage.after),
        notes.section,
        this.shelfmark(passage.document),
        this.citationBlock(passage.document));
      const facsimile = passage.facsimile
        || (passage.pages || []).find((page) => page.facsimile)?.facsimile;
      this.setFoot(
        externalLink(passage.url || source.url, t('panel.open'), 'button-link is-primary'),
        externalLink(facsimile, t('panel.facsimile'), 'button-link'));
      if (keepScroll) this.body.scrollTop = scroll;
      else this.body.querySelector('mark')?.scrollIntoView({ block: 'center' });
    };
    await show(false);
  }

  async renderNote(source, citation, stale) {
    this.documentHeader(source, null, source.page);
    this.loading();
    const { note, passage } = await getNote(source.note_id);
    if (stale()) return;
    this.documentHeader(source, passage.document, passage.page);
    const kindKey = `panel.note.${note.kind}`;
    const ranges = (citation.citedTexts || []).map((quote) => findQuote(note.text, quote)).filter(Boolean);
    const anchored = note.anchor_end > note.anchor_start;
    const context = renderPassage({
      text: passage.text, offset: passage.start, page: passage.page, pages: passage.pages || [],
      focus: anchored ? [note.anchor_start - passage.start, note.anchor_end - passage.start] : null,
    });
    this.fill(
      h('section', { class: 'note-card' },
        h('h3', { 'data-i18n': kindKey, text: t(kindKey) }),
        h('div', { class: 'note-card-text' }, renderPassage({ text: note.text, highlights: ranges }).childNodes)),
      note.kind === 'introduction' ? null : h('section', { class: 'panel-section' },
        h('h3', { 'data-i18n': 'panel.note.context', text: t('panel.note.context') }),
        h('article', { class: 'paper is-context', lang: 'mul' }, context)),
      this.shelfmark(passage.document),
      this.citationBlock(passage.document));
    this.setFoot(externalLink(passage.url || source.url, t('panel.open'), 'button-link is-primary'));
  }

  async renderRegister(source, stale) {
    this.setHeader(badge(t('panel.register')), source.title || '', '');
    this.loading();
    const entity = await getEntity(source.entity_id);
    if (stale()) return;
    const life = [entity.born, entity.died].some(Boolean) ? `${entity.born || '?'}–${entity.died || '?'}` : '';
    this.setHeader(badge(kindLabel(entity.kind), KIND_SLOTS[entity.kind]), entity.name, life);
    const authorities = [
      entity.gnd && ['GND', `https://d-nb.info/gnd/${entity.gnd}`],
      entity.viaf && ['VIAF', `https://viaf.org/viaf/${entity.viaf}`],
      entity.geonames && ['GeoNames', `https://www.geonames.org/${entity.geonames}`],
      entity.wikidata && ['Wikidata', `https://www.wikidata.org/wiki/${entity.wikidata}`],
      ...Object.entries(entity.links || {}).filter(([name]) => name !== 'Wikidata'),
    ].filter(Boolean);
    const language = getLanguage();
    this.fill(
      entity.description ? h('p', { class: 'entity-description', text: entity.description }) : null,
      (entity.alternative_names || []).length ? h('p', { class: 'entity-also' },
        h('span', { class: 'label', 'data-i18n': 'panel.register.also', text: t('panel.register.also') }),
        ' ', entity.alternative_names.slice(0, 12).join(' · ')) : null,
      Number.isFinite(entity.lat) && Number.isFinite(entity.lon) ? h('p', { class: 'entity-also' },
        h('span', { class: 'label', 'data-i18n': 'panel.register.coordinates', text: t('panel.register.coordinates') }),
        ` ${entity.lat.toFixed(3)}, ${entity.lon.toFixed(3)}`) : null,
      h('p', { class: 'entity-count', text: t('panel.register.mentions', {
        mentions: formatNumber(entity.mention_count || 0, language),
        documents: formatNumber(entity.document_count || 0, language) }) }),
      entity.in_register === 0 ? h('p', { class: 'panel-note', 'data-i18n': 'panel.register.unregistered',
                                         text: t('panel.register.unregistered') }) : null,
      authorities.length ? h('ul', { class: 'authority-list' }, authorities.map(([name, url]) => (
        h('li', {}, externalLink(url, name))))) : null);
    this.setFoot(externalLink(entity.url || source.url, t('panel.open'), 'button-link is-primary'));
  }

  renderData(source) {
    const data = source.data || {};
    this.setHeader(badge(t('panel.data'), 7), source.title || t('panel.data'), '');
    this.fill(
      h('p', { class: 'panel-note', 'data-i18n': 'panel.data.note', text: t('panel.data.note') }),
      sortableTable(data.columns || [], data.rows || [], data.links),
      data.truncated ? h('p', { class: 'panel-note', 'data-i18n': 'panel.data.truncated',
                                text: t('panel.data.truncated') }) : null,
      data.sql ? h('details', { class: 'panel-section', open: true },
        h('summary', { 'data-i18n': 'activity.sql', text: t('activity.sql') }),
        h('pre', {}, h('code', { text: data.sql }))) : null);
    this.setFoot(data.sql ? copyButton('panel.copySql', data.sql) : null);
  }

  renderComputation(source) {
    const computation = source.computation || {};
    this.setHeader(badge(t('panel.computation'), 7), source.title || t('panel.computation'), '');
    this.fill(
      computation.output ? h('section', { class: 'panel-section' },
        h('h3', { 'data-i18n': 'panel.computation.output', text: t('panel.computation.output') }),
        h('pre', { class: 'output' }, h('code', { text: computation.output }))) : null,
      computation.code ? h('details', { class: 'panel-section', open: !computation.output },
        h('summary', { 'data-i18n': 'activity.code', text: t('activity.code') }),
        h('pre', {}, h('code', { text: computation.code }))) : null);
    this.setFoot(computation.code ? copyButton('panel.copyCode', computation.code) : null);
  }

  renderWikipedia(source, citation) {
    const wiki = source.wiki || {};
    this.setHeader(badge(t('panel.wikipedia')), wiki.title || source.title || '',
      truncate(wiki.description || '', 120));
    const extract = wiki.extract || '';
    const ranges = (citation.citedTexts || []).map((quote) => findQuote(extract, quote)).filter(Boolean);
    this.fill(
      h('p', { class: 'panel-note', 'data-i18n': 'panel.wikipedia.note', text: t('panel.wikipedia.note') }),
      h('article', { class: 'paper is-external', lang: wiki.lang || null },
        renderPassage({ text: extract, highlights: ranges })));
    this.setFoot(externalLink(wiki.url || source.url, t('panel.wikipedia.open'), 'button-link is-primary'));
  }
}
