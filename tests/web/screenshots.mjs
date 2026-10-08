/* Visual check of the web client against scripts/mock_api.py.

   MOCK_SPEED=0.25 python scripts/mock_api.py &
   PLAYWRIGHT_CORE=file:///…/node_modules/playwright-core/index.mjs \
     node tests/web/screenshots.mjs http://127.0.0.1:8791 out-dir [filter]

   Drives the installed Microsoft Edge. Writes one PNG per state, viewport and
   theme, and exits non-zero if the page logged an error. */

import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');

const base = process.argv[2] || 'http://127.0.0.1:8791';
const out = process.argv[3] || 'screenshots';
const filter = process.argv[4] || '';
mkdirSync(out, { recursive: true });

const VIEWPORTS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };
const THEMES = ['light', 'dark'];
const problems = [];

async function ask(page, question) {
  await page.fill('#question', question);
  await page.press('#question', 'Enter');
}

async function done(page) {
  await page.waitForSelector('.answer-actions:not([hidden]), .notice.is-error', { timeout: 60000 });
  await page.waitForTimeout(700);
}

async function reveal(page, selector) {
  await page.locator(selector).first().scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
}

const STATES = {
  welcome: async () => {},
  streaming: async (page, shot) => {
    await ask(page, 'Wen traf Humboldt 1797 in Wien?');
    await page.waitForSelector('.step-tool', { timeout: 30000 });
    await page.waitForTimeout(250);
    await shot('research');
    await page.waitForSelector('.prose p', { timeout: 30000 });
    await page.waitForTimeout(150);
  },
  answer: async (page, shot) => {
    await ask(page, 'Wen traf Humboldt 1797 in Wien?');
    await done(page);
    await page.evaluate(() => document.getElementById('thread').scrollTo(0, 0));
    await shot('top');
    await page.locator('.activity > summary').click();
    await page.waitForTimeout(300);
    await shot('trail');
    await page.locator('.activity > summary').click();
    await reveal(page, '.followups');
  },
  panel: async (page, shot) => {
    await ask(page, 'Wen traf Humboldt 1797 in Wien?');
    await done(page);
    await page.locator('button.cite').nth(2).click();
    await page.waitForSelector('.paper', { timeout: 15000 });
    await page.waitForTimeout(600);
    await shot('text');
    await page.keyboard.press('Escape');
    await page.locator('.source-row').first().click();
    await page.waitForTimeout(900);
    await shot('first-source');
    for (const [name, pattern] of [['register', /Register/], ['data', /Datenabfrage/], ['note', /Anmerkung/]]) {
      const row = page.locator('.source-row', { hasText: pattern }).first();
      if (await row.count()) {
        await page.keyboard.press('Escape');
        await row.click();
        await page.waitForTimeout(900);
        await shot(name);
      }
    }
  },
  map: async (page, shot) => {
    await ask(page, 'Zeichne eine Karte der Italienreise 1805.');
    await done(page);
    await page.waitForSelector('.leaflet-marker-icon', { timeout: 20000 });
    await reveal(page, '.artifact-map');
    await page.waitForTimeout(1500);
    await shot('map');
    await reveal(page, '.artifact-timeline');
  },
  python: async (page, shot) => {
    await ask(page, 'Zeige die Briefe pro Jahr als Diagramm.');
    await done(page);
    await page.waitForSelector('.chart-plot .main-svg', { timeout: 30000 });
    await page.evaluate(() => document.getElementById('thread').scrollTo(0, 0));
    await shot('top');
    await reveal(page, '.artifact-chart');
    await shot('chart');
    await reveal(page, '.artifact-table');
    await shot('table');
    await reveal(page, '.artifact-image');
    await shot('image');
    await page.locator('.source-row').first().click();
    await page.waitForTimeout(600);
  },
  network: async (page) => {
    await ask(page, 'Zeige das Netzwerk um Bonpland.');
    await done(page);
    await reveal(page, '.artifact-network');
    await page.waitForTimeout(2500);
  },
  markers: async (page) => {
    await page.selectOption('#model', 'local');
    await ask(page, 'Wen traf Humboldt 1797 in Wien?');
    await done(page);
    await page.evaluate(() => document.getElementById('thread').scrollTo(0, 0));
  },
  error: async (page) => {
    await ask(page, 'Fehler bitte');
    await done(page);
  },
  ratelimit: async (page) => {
    await ask(page, 'limit');
    await page.waitForSelector('.notice.is-error', { timeout: 15000 });
  },
  about: async (page) => {
    await page.click('#about-open');
    await page.waitForTimeout(300);
  },
  english: async (page) => {
    await page.click('#language-toggle');
    await page.waitForTimeout(300);
  },
};

const browser = await chromium.launch({ channel: 'msedge' });
for (const [viewportName, viewport] of Object.entries(VIEWPORTS)) {
  for (const theme of THEMES) {
    for (const [state, run] of Object.entries(STATES)) {
      const name = `${state}-${viewportName}-${theme}`;
      if (filter && !name.includes(filter)) continue;
      const context = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: 'de-DE',
                                                 colorScheme: theme });
      await context.addInitScript((value) => {
        localStorage.setItem('alex.theme', value);
        localStorage.setItem('alex.lang', 'de');
      }, theme);
      const page = await context.newPage();
      page.on('console', (message) => {
        const expected = state === 'ratelimit' && message.text().includes('429');
        if (message.type() === 'error' && !expected) problems.push(`${name}: ${message.text()}`);
      });
      page.on('pageerror', (error) => problems.push(`${name}: ${error.message}`));
      const shot = (suffix) => page.screenshot({ path: join(out, `${name}${suffix ? '-' + suffix : ''}.png`) });
      try {
        await page.goto(base + '/', { waitUntil: 'networkidle' });
        await run(page, shot);
        await shot('');
        const overflow = await page.evaluate(() => (
          document.documentElement.scrollWidth > document.documentElement.clientWidth + 1));
        if (overflow) problems.push(`${name}: horizontal overflow`);
      } catch (error) {
        problems.push(`${name}: ${error.message.split('\n')[0]}`);
        await shot('failed').catch(() => {});
      }
      await context.close();
    }
  }
}

if (!filter || 'embed'.includes(filter) || filter.includes('embed')) {
  const context = await browser.newContext({ viewport: { width: 440, height: 680 }, locale: 'de-DE' });
  const page = await context.newPage();
  page.on('pageerror', (error) => problems.push(`embed: ${error.message}`));
  await page.goto(base + '/?embed=1&lang=de', { waitUntil: 'networkidle' });
  await page.screenshot({ path: join(out, 'embed-welcome.png') });
  await ask(page, 'Wen traf Humboldt 1797 in Wien?');
  await done(page);
  await page.screenshot({ path: join(out, 'embed-answer.png') });
  await page.locator('button.cite').first().click();
  await page.waitForTimeout(900);
  await page.screenshot({ path: join(out, 'embed-panel.png') });
  await context.close();
}

await browser.close();
if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log('no console errors, no horizontal overflow');
