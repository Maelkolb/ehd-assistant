/** Light / dark / system colour scheme. CSS does the work; this stores the
    choice and tells canvas- and Plotly-based visualisations to repaint. */

const ORDER = ['system', 'light', 'dark'];
const media = window.matchMedia('(prefers-color-scheme: dark)');
const listeners = new Set();

export function themeChoice() {
  const stored = document.documentElement.getAttribute('data-theme');
  return stored === 'light' || stored === 'dark' ? stored : 'system';
}

export function resolvedTheme() {
  const choice = themeChoice();
  return choice === 'system' ? (media.matches ? 'dark' : 'light') : choice;
}

export function setTheme(choice) {
  if (choice === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', choice);
  try {
    if (choice === 'system') localStorage.removeItem('alex.theme');
    else localStorage.setItem('alex.theme', choice);
  } catch { /* private mode */ }
  notify();
}

export function cycleTheme() {
  const next = ORDER[(ORDER.indexOf(themeChoice()) + 1) % ORDER.length];
  setTheme(next);
  return next;
}

export function onThemeChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify() {
  for (const listener of listeners) listener(resolvedTheme());
}

media.addEventListener('change', () => {
  if (themeChoice() === 'system') notify();
});

/** Colours for canvas and Plotly, read from the CSS custom properties in force. */
export function vizColors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name) => style.getPropertyValue(name).trim();
  return {
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((slot) => read(`--viz-${slot}`)),
    surface: read('--surface'),
    ink: read('--ink'),
    ink2: read('--ink-2'),
    muted: read('--muted'),
    grid: read('--viz-grid'),
    axis: read('--viz-axis'),
    accent: read('--accent'),
    fontSans: read('--font-sans'),
  };
}

export const KIND_SLOTS = { person: 1, place: 2, institution: 3, glossary: 4, plant: 6, siglum: 7 };
export const DOCTYPE_SLOTS = { diary: 1, letter: 2, document: 3, chronology: 4,
                               research_article: 5, about: 6 };
