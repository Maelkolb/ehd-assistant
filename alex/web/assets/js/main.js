/** Alex web client: wires the page together. */

import { h, clear, append, formatNumber } from './dom.js';
import { icon } from './icons.js';
import { t, translate, getLanguage, setLanguage, formatDate } from './i18n.js';
import { themeChoice, cycleTheme } from './theme.js';
import { getConfig, streamChat, ApiError } from './api.js';
import { TurnView } from './answer.js';
import { SourcePanel } from './sources.js';
import { sharedAnswer } from './share.js';

const FALLBACK_CONFIG = {
  assistant: 'Alex', corpus: { version: '', documents: {}, entities: {} }, models: [],
  features: { python: false, feedback: false },
  limits: { max_query_chars: 2000, max_history_turns: 12 },
  links: { edition: 'https://edition-humboldt.de' }, map: {},
};
const EXAMPLE_GROUPS = [
  { key: 'people', icon: 'card' },
  { key: 'journeys', icon: 'calendar' },
  { key: 'data', icon: 'database' },
  { key: 'viz', icon: 'pin' },
];
const embed = document.documentElement.hasAttribute('data-embed');

const elements = {
  thread: document.getElementById('thread'),
  welcome: document.getElementById('welcome'),
  turns: document.getElementById('turns'),
  form: document.getElementById('composer'),
  question: document.getElementById('question'),
  send: document.getElementById('send'),
  count: document.getElementById('count'),
  model: document.getElementById('model'),
  modelOption: document.getElementById('model-option'),
  live: document.getElementById('live'),
  about: document.getElementById('about'),
  aboutBody: document.getElementById('about-body'),
  themeToggle: document.getElementById('theme-toggle'),
  languageToggle: document.getElementById('language-toggle'),
};

let config = FALLBACK_CONFIG;
let panel = null;
let stream = null;
const turns = [];

const store = {
  get(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode */ }
  },
};

function announce(key) {
  elements.live.textContent = '';
  requestAnimationFrame(() => { elements.live.textContent = t(key); });
}

function nearBottom() {
  const { scrollHeight, scrollTop, clientHeight } = elements.thread;
  return scrollHeight - scrollTop - clientHeight < 140;
}

function scrollToEnd(smooth = false) {
  elements.thread.scrollTo({ top: elements.thread.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

// ---------------------------------------------------------------- welcome

function renderWelcome() {
  const version = config.corpus?.version;
  const documents = config.corpus?.documents || {};
  const language = getLanguage();
  const facts = ['diary', 'letter', 'document', 'chronology', 'research_article']
    .filter((type) => documents[type])
    .map((type) => `${formatNumber(documents[type], language)} ${t(`doctypes.${type}`)}`);
  clear(elements.welcome);
  append(elements.welcome, [
    h('div', { class: 'welcome-mark' }, icon('compass')),
    h('h1', { class: 'welcome-title', text: t('welcome.title') }),
    h('p', { class: 'welcome-lead', text: t('welcome.lead') }),
    h('div', { class: 'examples' }, EXAMPLE_GROUPS.map((group) => h('section', { class: 'example-group' },
      h('h2', {}, icon(group.icon), h('span', { text: t(`welcome.group.${group.key}`) })),
      h('ul', {}, [1, 2].map((number) => {
        const question = t(`welcome.ex.${group.key}.${number}`);
        return h('li', {}, h('button', { type: 'button', class: 'example', onClick: () => ask(question) },
          h('span', { text: question }), icon('chevronRight')));
      }))))),
    version ? h('p', { class: 'welcome-corpus' },
      h('span', { text: t('welcome.corpus', { version }) }),
      facts.length ? h('span', { class: 'welcome-facts', text: facts.join(', ') }) : null) : null,
  ]);
}

// ---------------------------------------------------------------- composer

const NARROW = window.matchMedia('(max-width: 520px)');

function updatePlaceholder() {
  const key = NARROW.matches || embed ? 'composer.placeholderShort' : 'composer.placeholder';
  elements.question.dataset.i18nPlaceholder = key;
  elements.question.placeholder = t(key);
}

function resizeQuestion() {
  const area = elements.question;
  area.style.height = 'auto';
  area.style.height = `${Math.min(area.scrollHeight, 220)}px`;
  const max = config.limits?.max_query_chars || 2000;
  const length = area.value.length;
  elements.count.hidden = length < max * 0.8;
  elements.count.textContent = t('composer.tooLong', { count: length, max });
  elements.count.classList.toggle('is-over', length >= max);
  updateSend();
}

function updateSend() {
  const streaming = Boolean(stream);
  const empty = !elements.question.value.trim();
  elements.send.disabled = !streaming && empty;
  elements.send.classList.toggle('is-stop', streaming);
  const key = streaming ? 'composer.stop' : 'composer.send';
  elements.send.title = t(key);
  elements.send.setAttribute('aria-label', t(key));
  elements.send.replaceChildren(icon(streaming ? 'stop' : 'send'));
}

function setupModels() {
  const models = config.models || [];
  const stored = store.get('alex.model');
  const chosen = models.find((model) => model.id === stored) || models.find((model) => model.default) || models[0];
  elements.model.replaceChildren(...models.map((model) => {
    const hint = model.tier === 'premium' ? t('model.premium') : model.tier === 'fast' ? t('model.fast')
      : model.provider === 'local' ? t('model.local') : '';
    return h('option', { value: model.id, selected: chosen && model.id === chosen.id,
                         text: hint ? `${model.label} (${hint})` : model.label });
  }));
  elements.modelOption.hidden = models.length < 2;
  elements.question.maxLength = config.limits?.max_query_chars || 2000;
}

// ---------------------------------------------------------------- conversation

function history() {
  const limit = config.limits?.max_history_turns ?? 12;
  const messages = turns.filter((turn) => turn.view.state === 'done' && turn.view.text.trim())
    .flatMap((turn) => [
      { role: 'user', content: turn.query },
      { role: 'assistant', content: turn.view.text.slice(0, 6000) },
    ]);
  const recent = messages.slice(-limit);
  return recent.length && recent[0].role === 'assistant' ? recent.slice(1) : recent;
}

function turnContext() {
  return {
    config,
    panel,
    onAsk: (question) => ask(question),
    onRetry: (view) => retry(view),
    onUpdate: () => { if (stickToEnd) scrollToEnd(); },
  };
}

let stickToEnd = true;
elements.thread.addEventListener('scroll', () => { stickToEnd = nearBottom(); }, { passive: true });

async function ask(question) {
  const query = String(question || '').trim();
  if (!query || stream) return;
  elements.welcome.hidden = true;
  const view = new TurnView({ query, context: turnContext() });
  const body = {
    query,
    history: history(),
    language: getLanguage(),
  };
  if (elements.model.value) body.model = elements.model.value;
  turns.push({ query, view });
  elements.turns.append(view.element);
  elements.question.value = '';
  resizeQuestion();
  stickToEnd = true;
  scrollToEnd();

  const controller = new AbortController();
  stream = { controller, view };
  updateSend();
  announce('status.thinking');
  // The answer is complete at `done`; follow-up suggestions may still trail in,
  // and must not keep the composer locked while they do.
  const release = () => {
    if (stream && stream.controller === controller) {
      stream = null;
      updateSend();
    }
  };
  try {
    for await (const event of streamChat(body, controller.signal)) {
      view.handle(event);
      if (event.type === 'done') {
        release();
        announce('status.done');
      }
    }
    if (view.state === 'working') view.fail('network');
  } catch (error) {
    if (error.name === 'AbortError') {
      view.stop();
      announce('status.stopped');
    } else if (error instanceof ApiError) {
      view.fail(error.code, error.message);
    } else {
      console.error(error);
      view.fail('internal');
    }
  } finally {
    release();
  }
}

function retry(view) {
  if (stream) return;
  const position = turns.findIndex((turn) => turn.view === view);
  if (position < 0) return;
  const { query } = turns[position];
  turns.splice(position, 1);
  view.destroy();
  ask(query);
}

function newConversation() {
  stream?.controller.abort();
  for (const turn of turns.splice(0)) turn.view.destroy();
  panel.close();
  clearSharedFragment();
  elements.welcome.hidden = false;
  elements.thread.scrollTop = 0;
  elements.question.focus();
}

function clearSharedFragment() {
  if (location.hash) window.history.replaceState(null, '', location.pathname + location.search);
}

async function showShared() {
  const shared = await sharedAnswer();
  if (!shared) return;
  elements.welcome.hidden = true;
  const view = new TurnView({ query: shared.query, context: turnContext() });
  view.complete({ blocks: [{ text: shared.answer.text || '', citations: [] }], sources: [], artifacts: [],
                  ...shared.answer });
  view.showNotice('shared.notice', false);
  turns.push({ query: shared.query, view });
  elements.turns.append(view.element);
}

// ---------------------------------------------------------------- chrome

function updateThemeToggle() {
  const choice = themeChoice();
  const label = `${t('header.theme')}: ${t(`theme.${choice}`)}`;
  elements.themeToggle.replaceChildren(icon({ light: 'sun', dark: 'moon', system: 'monitor' }[choice]));
  elements.themeToggle.title = label;
  elements.themeToggle.setAttribute('aria-label', label);
}

function updateLanguageToggle() {
  elements.languageToggle.textContent = t('header.languageShort');
  elements.languageToggle.title = t('header.language');
  elements.languageToggle.setAttribute('aria-label', t('header.language'));
}

function renderAbout() {
  const corpus = config.corpus || {};
  const links = config.links || {};
  const link = (href, key) => (href && /^https?:\/\//.test(href)
    ? h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, t(key), icon('external')) : null);
  const section = (title, body, extra) => h('section', {},
    h('h3', { text: t(title) }), h('p', { text: body }), extra || null);
  clear(elements.aboutBody);
  append(elements.aboutBody, [
    section('about.what.title', t('about.what.body')),
    section('about.data.title', t('about.data.body', {
      version: corpus.version || '–', date: corpus.commit_date ? formatDate(corpus.commit_date) : '–' })),
    section('about.ai.title', t('about.ai.body'),
      (config.models || []).length ? h('p', { class: 'about-models' },
        h('span', { class: 'label', text: `${t('about.models')}: ` }),
        config.models.map((model) => model.label).join(', ')) : null),
    section('about.privacy.title', t('about.privacy.body')),
    h('p', { class: 'about-links' },
      link(links.edition, 'about.links.edition'),
      link(links.imprint, links.privacy === links.imprint ? 'about.links.imprintPrivacy' : 'about.links.imprint'),
      links.privacy && links.privacy !== links.imprint ? link(links.privacy, 'about.links.privacy') : null),
  ]);
}

function onLanguage() {
  updatePlaceholder();
  updateLanguageToggle();
  updateThemeToggle();
  updateSend();
  setupModels();
  renderWelcome();
  renderAbout();
  for (const turn of turns) turn.view.refresh();
  panel.refresh();
}

function bind() {
  elements.form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (stream) stream.controller.abort();
    else ask(elements.question.value);
  });
  elements.question.addEventListener('input', resizeQuestion);
  elements.question.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!stream) ask(elements.question.value);
    }
  });
  elements.model.addEventListener('change', () => store.set('alex.model', elements.model.value));
  document.getElementById('new-conversation').addEventListener('click', newConversation);
  elements.languageToggle.addEventListener('click', () => {
    setLanguage(getLanguage() === 'de' ? 'en' : 'de');
    onLanguage();
  });
  elements.themeToggle.addEventListener('click', () => {
    cycleTheme();
    updateThemeToggle();
  });
  document.getElementById('about-open').addEventListener('click', () => elements.about.showModal());
  document.getElementById('about-close').addEventListener('click', () => elements.about.close());
  elements.about.addEventListener('click', (event) => {
    if (event.target === elements.about) elements.about.close();
  });
}

async function start() {
  panel = new SourcePanel({ embed });
  document.title = t('app.title');
  translate();
  updatePlaceholder();
  NARROW.addEventListener('change', updatePlaceholder);
  bind();
  updateLanguageToggle();
  updateThemeToggle();
  renderWelcome();
  resizeQuestion();
  try {
    config = { ...FALLBACK_CONFIG, ...(await getConfig()) };
  } catch (error) {
    console.warn('Could not load the configuration', error);
  }
  setupModels();
  renderWelcome();
  renderAbout();
  updateSend();
  await showShared();
  if (!embed && !turns.length && window.matchMedia('(min-width: 800px)').matches) elements.question.focus();
}

start();
