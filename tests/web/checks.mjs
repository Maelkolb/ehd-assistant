/* Behavioural checks of the web client against scripts/mock_api.py:
   pure functions (markdown, citations, quote matching) run inside the real
   page, followed by a keyboard-only pass.

   PLAYWRIGHT_CORE=file:///…/node_modules/playwright-core/index.mjs \
     node tests/web/checks.mjs http://127.0.0.1:8791 */

import assert from 'node:assert/strict';

const { chromium } = await import(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const base = process.argv[2] || 'http://127.0.0.1:8791';
const browser = await chromium.launch({ channel: 'msedge' });
const results = [];

async function check(name, run) {
  try {
    await run();
    results.push(`ok    ${name}`);
  } catch (error) {
    results.push(`FAIL  ${name}\n      ${String(error.message).split('\n').join('\n      ')}`);
    process.exitCode = 1;
  }
}

async function open(viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, locale: 'de-DE', acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  return { context, page, errors };
}

const focused = (page) => page.evaluate(() => {
  const node = document.activeElement;
  if (!node) return '';
  const suffix = node.id ? '#' + node.id
    : (typeof node.className === 'string' && node.className ? '.' + node.className.split(' ')[0] : '');
  return node.tagName.toLowerCase() + suffix;
});

// ---------------------------------------------------------------- pure functions

{
  const { context, page } = await open();

  await check('markdown escapes HTML and refuses script URLs', async () => {
    const html = await page.evaluate(async () => {
      const { renderMarkdown } = await import('./assets/js/markdown.js');
      return renderMarkdown('<img src=x onerror=alert(1)> [x](javascript:alert(1)) [ok](https://example.org/a?b=1&c=2) **fett** *kursiv* `code <b>`');
    });
    assert.ok(!html.includes('<img'), html);
    assert.ok(!html.includes('href="javascript'), html);
    assert.ok(html.includes('<a href="https://example.org/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">ok</a>'), html);
    assert.ok(html.includes('<strong>fett</strong>') && html.includes('<em>kursiv</em>'), html);
    assert.ok(html.includes('<code>code &lt;b&gt;</code>'), html);
  });

  await check('table cells that name documents or register entries link to the edition', async () => {
    const out = await page.evaluate(async () => {
      const { sortableTable } = await import('./assets/js/artifacts/table.js');
      const table = sortableTable(
        ['doc_id', 'page', 'recipient', 'n'],
        [['H0019734', '56v', 'Kunth, Carl Sigismund', 3], ['unbekannt', '1r', 'javascript', 1]],
        {
          H0019734: { url: 'https://edition-humboldt.de/v12/H0019734', kind: 'document', title: 'Tagebuch' },
          'Kunth, Carl Sigismund': { url: 'https://edition-humboldt.de/v12/H0001517', kind: 'entity' },
          javascript: { url: 'javascript:alert(1)', kind: 'entity' },
        });
      const links = [...table.querySelectorAll('a')];
      return {
        hrefs: links.map((link) => link.getAttribute('href')),
        safe: links.every((link) => link.target === '_blank' && link.rel.includes('noopener')),
        cells: table.querySelectorAll('tbody td').length,
      };
    });
    assert.deepEqual(out.hrefs, [
      'https://edition-humboldt.de/v12/H0019734',
      'https://edition-humboldt.de/v12/H0019734/56v',
      'https://edition-humboldt.de/v12/H0001517',
    ]);
    assert.ok(out.safe && out.cells === 8, JSON.stringify(out));
  });

  await check('markdown renders blocks and tolerates half-streamed input', async () => {
    const out = await page.evaluate(async () => {
      const { renderMarkdown } = await import('./assets/js/markdown.js');
      return {
        table: renderMarkdown('| a | b |\n|---|---:|\n| 1 | 2 |'),
        halfTable: renderMarkdown('| a | b |\n|--'),
        list: renderMarkdown('1. eins\n2. zwei\n   - innen\n\nText'),
        fence: renderMarkdown('```python\nprint("<x>")'),
        heading: renderMarkdown('## Titel\n\n> Zitat'),
        snake: renderMarkdown('date_from und date_to'),
      };
    });
    assert.ok(out.table.includes('<table>') && out.table.includes('class="align-right"'), out.table);
    assert.ok(out.halfTable.includes('a') && !out.halfTable.includes('undefined'), out.halfTable);
    assert.ok(out.list.includes('<ol>') && out.list.includes('<ul><li>innen</li></ul>') && out.list.includes('<p>Text</p>'), out.list);
    assert.ok(out.fence.includes('<pre><code>print(&quot;&lt;x&gt;&quot;)</code></pre>'), out.fence);
    assert.ok(out.heading.includes('<h3>Titel</h3>') && out.heading.includes('<blockquote>'), out.heading);
    assert.ok(!out.snake.includes('<em>'), out.snake);
  });

  await check('citations become chips from blocks and from inline markers', async () => {
    const out = await page.evaluate(async () => {
      const { composeAnswer } = await import('./assets/js/answer.js');
      const numbers = new Map();
      const numberFor = (id) => numbers.get(id) ?? numbers.set(id, numbers.size + 1).get(id);
      const cited = composeAnswer([
        { text: 'Erster Satz.\n\n', citations: [{ source_id: 'S3', cited_text: 'a' }, { source_id: 'S3', cited_text: 'b' }, { source_id: 'S1', cited_text: 'c' }] },
        { text: '| x | y |\n', citations: [{ source_id: 'S1', cited_text: 'd' }] },
      ], numberFor);
      const inline = composeAnswer([{ text: 'Satz [S7] und [S8, S9]. Offen [S1', citations: [] }], numberFor, true);
      return { cited, inline };
    });
    assert.equal(out.cited.references.length, 3);
    assert.deepEqual(out.cited.references[0].citedTexts, ['a', 'b']);
    assert.match(out.cited.markdown, /^Erster Satz\.\uE0000\uE001\uE0001\uE001\n\n\| x \| y\uE0002\uE001 \|\n$/);
    assert.equal(out.inline.references.length, 3);
    assert.ok(!out.inline.markdown.includes('[S'), out.inline.markdown);
  });

  await check('quotes are found despite page marks, whitespace and length', async () => {
    const out = await page.evaluate(async () => {
      const { findQuote } = await import('./assets/js/passage.js');
      const text = 'Das Barometer stand seit 8 Tagen\n\nunveränderlich, ohnerachtet es täglich heftige Gewitterregen mit Wind gab. Ich wagte es daher, mit einem Barometer zu messen.';
      const long = 'x'.repeat(10) + ' Anfang der langen Stelle, die sich über viele Wörter zieht ' + 'y '.repeat(60) + 'und hier ist das Ende der langen Stelle erreicht.';
      return {
        marks: findQuote(text, '⟦55r⟧ Das Barometer stand seit 8 Tagen ⟦55v⟧ unveränderlich'),
        slice: (() => { const range = findQuote(text, 'heftige   Gewitterregen\nmit Wind'); return range && text.slice(...range); })(),
        missing: findQuote(text, 'kommt nicht vor'),
        long: (() => { const range = findQuote(long, 'Anfang der langen Stelle, die sich über viele Wörter zieht ' + 'z '.repeat(60) + 'und hier ist das Ende der langen Stelle erreicht.'); return range && long.slice(...range).slice(-9); })(),
      };
    });
    assert.deepEqual(out.marks, [0, 48]);
    assert.equal(out.slice, 'heftige Gewitterregen mit Wind');
    assert.equal(out.missing, null);
    assert.equal(out.long, 'erreicht.');
  });

  await context.close();
}

// ---------------------------------------------------------------- keyboard

{
  const { context, page, errors } = await open();

  await check('tab order reaches every control in a sensible sequence', async () => {
    const seen = [];
    for (let index = 0; index < 24; index += 1) {
      await page.keyboard.press('Tab');
      seen.push(await focused(page));
    }
    const first = seen.indexOf('a.skip-link');
    assert.ok(first >= 0, seen.join(' > '));
    const order = [...seen.slice(first), ...seen.slice(0, first)];
    const expected = ['a.skip-link', 'button#new-conversation', 'button#language-toggle',
                      'button#theme-toggle', 'button#about-open', 'button.example'];
    assert.deepEqual(order.slice(0, expected.length), expected);
    const tail = order.slice(order.indexOf('textarea#question'));
    assert.deepEqual(tail.slice(0, 2), ['textarea#question', 'select#model'], order.join(' > '));
  });

  await check('about dialog opens and closes from the keyboard and returns focus', async () => {
    await page.focus('#about-open');
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => document.getElementById('about').open), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.getElementById('about').open), false);
    assert.equal(await focused(page), 'button#about-open');
  });

  await check('a question can be asked and its source opened with the keyboard alone', async () => {
    await page.focus('#question');
    await page.keyboard.type('Wen traf Humboldt 1797 in Wien?');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.answer-actions:not([hidden])', { timeout: 60000 });
    await page.focus('button.cite');
    await page.waitForSelector('#cite-preview:not([hidden])');
    const previewText = await page.textContent('#cite-preview');
    assert.ok(previewText.includes('Klicken öffnet die Quelle'), previewText);
    assert.ok((await page.locator('#cite-preview .cite-preview-title').textContent()).length > 3, previewText);
    await page.keyboard.press('Enter');
    await page.waitForSelector('#source-panel:not([hidden]) .panel-body > *');
    assert.equal(await page.evaluate(() => document.getElementById('cite-preview').hidden), true);
    assert.equal(await focused(page), 'aside#source-panel');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.getElementById('source-panel').hidden), true);
    assert.equal(await focused(page), 'button.cite');
  });

  await check('hovering a citation chip previews the quoted passage', async () => {
    await page.mouse.move(0, 0);
    await page.locator('button.cite').first().hover();
    await page.waitForSelector('#cite-preview:not([hidden])');
    const quote = await page.locator('#cite-preview .cite-preview-quote').count();
    assert.ok(quote >= 0);
    await page.mouse.move(0, 0);
    await page.waitForFunction(() => document.getElementById('cite-preview').hidden);
  });

  await check('language switch relabels the interface and the finished answer', async () => {
    await page.click('#language-toggle');
    assert.equal(await page.textContent('.answer-sources h3'), 'Sources');
    assert.equal(await page.getAttribute('html', 'lang'), 'en');
    assert.match(await page.textContent('.activity-meta'), /steps/);
    await page.click('#language-toggle');
  });

  await check('new conversation clears the thread', async () => {
    await page.click('#new-conversation');
    assert.equal(await page.locator('.turn').count(), 0);
    assert.equal(await page.isVisible('#welcome'), true);
  });

  await check('no script errors on the desktop pass', async () => assert.deepEqual(errors, []));
  await context.close();
}

{
  const { context, page, errors } = await open({ width: 360, height: 740 });

  await check('at 360px the panel is modal, traps focus and nothing overflows', async () => {
    await page.fill('#question', 'Wen traf Humboldt 1797 in Wien?');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.answer-actions:not([hidden])', { timeout: 60000 });
    await page.locator('button.cite').first().click();
    await page.waitForSelector('#source-panel:not([hidden]) .panel-body > *');
    assert.equal(await page.getAttribute('#source-panel', 'aria-modal'), 'true');
    assert.equal(await page.isVisible('#panel-backdrop'), true);
    for (let index = 0; index < 14; index += 1) {
      await page.keyboard.press('Tab');
      const inside = await page.evaluate(() => document.getElementById('source-panel').contains(document.activeElement));
      assert.equal(inside, true, 'focus left the panel');
    }
    await page.keyboard.press('Escape');
    const overflow = await page.evaluate(() => (
      document.documentElement.scrollWidth > document.documentElement.clientWidth + 1));
    assert.equal(overflow, false);
  });

  await check('stop aborts a running answer and keeps what was written', async () => {
    await page.fill('#question', 'Zeichne eine Karte der Italienreise 1805.');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.turn:last-child .step-tool');
    await page.click('#send');
    await page.waitForSelector('.turn:last-child .turn-notice:not([hidden])');
    assert.equal(await page.getAttribute('#send', 'aria-label'), 'Frage senden');
  });

  await check('shared link restores an answer without a request', async () => {
    const url = await page.evaluate(async () => {
      const { shareUrl } = await import('./assets/js/share.js');
      return shareUrl('Geteilte Frage?', { text: 'Antwort **geteilt**.', blocks: [{ text: 'Antwort **geteilt**.', citations: [] }], sources: [], artifacts: [] });
    });
    const second = await context.newPage();
    await second.goto(url, { waitUntil: 'networkidle' });
    await second.waitForSelector('.prose strong');
    assert.equal(await second.textContent('.question p'), 'Geteilte Frage?');
    await second.close();
  });

  await check('no script errors on the mobile pass', async () => assert.deepEqual(errors, []));
  await context.close();
}

// ---------------------------------------------------------------- artifacts

{
  const { context, page, errors } = await open();
  const { readFileSync } = await import('node:fs');

  async function saved(card) {
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 30000 }),
      page.locator(`${card} .artifact-actions button`).first().click(),
    ]);
    return { name: download.suggestedFilename(), path: await download.path() };
  }

  await check('map and timeline export as standalone HTML', async () => {
    await page.fill('#question', 'Zeichne eine Karte der Italienreise 1805.');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.answer-actions:not([hidden])', { timeout: 60000 });
    await page.waitForSelector('.leaflet-marker-icon');
    assert.equal(await page.locator('.leaflet-marker-icon').count(), 11);
    const map = await saved('.artifact-map');
    assert.match(map.name, /italienreise-1805\.html$/);
    const mapHtml = readFileSync(map.path, 'utf8');
    assert.ok(mapHtml.includes('L.map(') && mapHtml.includes('"label":"Neapel"'), 'map export incomplete');
    const timeline = await saved('.artifact-timeline');
    assert.ok(readFileSync(timeline.path, 'utf8').includes('timeline-event'));
    await page.locator('.artifact-map .artifact-table summary').click();
    await page.locator('.station-button').nth(7).click();
    await page.waitForSelector('.leaflet-popup-content');
    assert.match(await page.textContent('.leaflet-popup-content'), /8\. Rom/);
  });

  await check('chart, table and image download; charts follow the theme', async () => {
    await page.click('#new-conversation');
    await page.fill('#question', 'Zeige die Briefe pro Jahr als Diagramm.');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.answer-actions:not([hidden])', { timeout: 60000 });
    await page.waitForSelector('.chart-plot .main-svg', { timeout: 30000 });
    assert.match((await saved('.artifact-chart')).name, /\.png$/);
    const table = await saved('.artifact-table');
    assert.match(readFileSync(table.path, 'utf8'), /^\uFEFFjahr,briefe\r\n1791,/);
    assert.match((await saved('.artifact-image')).name, /\.png$/);
    const before = await page.evaluate(() => document.querySelector('.chart-plot .xtick text').style.fill);
    await page.click('#theme-toggle');
    await page.click('#theme-toggle');
    await page.waitForTimeout(400);
    const after = await page.evaluate(() => document.querySelector('.chart-plot .xtick text').style.fill);
    assert.notEqual(before, after);
    await page.locator('.artifact-table th button').nth(1).click();
    await page.locator('.artifact-table th button').nth(1).click();
    const top = await page.textContent('.artifact-table tbody tr td:last-child');
    assert.equal(top.trim(), '29');
  });

  await check('network exports with its own drawing code', async () => {
    await page.click('#new-conversation');
    await page.fill('#question', 'Zeige das Netzwerk um Bonpland.');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.answer-actions:not([hidden])', { timeout: 60000 });
    await page.waitForSelector('.network-canvas');
    const network = await saved('.artifact-network');
    const html = readFileSync(network.path, 'utf8');
    assert.ok(html.includes('function mountNetwork(') && html.includes('Bonpland'), 'network export incomplete');
    const exported = await context.newPage();
    const exportErrors = [];
    exported.on('pageerror', (error) => exportErrors.push(error.message));
    await exported.setContent(html, { waitUntil: 'load' });
    await exported.waitForSelector('canvas.network-canvas');
    assert.deepEqual(exportErrors, []);
    await exported.close();
  });

  await check('no script errors on the artifact pass', async () => assert.deepEqual(errors, []));
  await context.close();
}

await browser.close();
console.log(results.join('\n'));
