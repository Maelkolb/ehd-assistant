/* Alex launcher for host pages.

   <script src="https://alex.example.org/widget.js" defer
           data-lang="de" data-position="right"></script>

   Adds a floating button that opens Alex in an iframe panel. Options (all
   optional, as data- attributes on the script tag):
     data-src       URL of the Alex deployment (default: where this script lives)
     data-lang      "de" or "en" (default: the host page's <html lang>)
     data-position  "right" (default) or "left"
     data-label     button text (default: "Alex fragen" / "Ask Alex")
   Nothing is loaded from Alex until the visitor opens the panel. */
(function () {
  'use strict';
  var script = document.currentScript;
  if (!script || document.getElementById('alex-widget')) return;

  var source = script.getAttribute('data-src') || new URL('.', script.src).href;
  var language = (script.getAttribute('data-lang') || document.documentElement.lang || 'de')
    .toLowerCase().indexOf('en') === 0 ? 'en' : 'de';
  var side = script.getAttribute('data-position') === 'left' ? 'left' : 'right';
  var text = language === 'en'
    ? { open: 'Ask Alex', close: 'Close Alex', title: 'Alex – research assistant' }
    : { open: 'Alex fragen', close: 'Alex schließen', title: 'Alex – Rechercheassistent' };
  var label = script.getAttribute('data-label') || text.open;

  var style = document.createElement('style');
  style.textContent = [
    '#alex-widget{position:fixed;z-index:2147483000;bottom:20px;' + side + ':20px;font:16px/1.2 "PT Sans","Segoe UI",system-ui,sans-serif}',
    '#alex-widget button{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;gap:8px;cursor:pointer;',
    'padding:12px 16px;border-radius:999px;background:#ac4d00;color:#fff;font-weight:700;box-shadow:0 4px 16px rgba(0,0,0,.25)}',
    '#alex-widget button:hover{background:#8f3f00}',
    '#alex-widget button:focus-visible{outline:3px solid #1c5cab;outline-offset:3px}',
    '#alex-widget svg{width:20px;height:20px}',
    '#alex-widget-panel{position:fixed;z-index:2147483001;bottom:84px;' + side + ':20px;width:min(440px,calc(100vw - 24px));',
    'height:min(680px,calc(100dvh - 108px));border:1px solid rgba(0,0,0,.15);border-radius:14px;overflow:hidden;',
    'background:#fdfcfa;box-shadow:0 12px 48px rgba(0,0,0,.28)}',
    '#alex-widget-panel[hidden]{display:none}',
    '#alex-widget-panel iframe{display:block;width:100%;height:100%;border:0}',
    '@media (max-width:520px){#alex-widget-panel{inset:0;width:auto;height:auto;border-radius:0;border:0}',
    '#alex-widget.is-open{bottom:auto;top:8px;' + side + ':8px}',
    '#alex-widget.is-open button{padding:8px 12px;font-size:14px}}',
  ].join('');
  document.head.appendChild(style);

  var root = document.createElement('div');
  root.id = 'alex-widget';
  var button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', 'alex-widget-panel');
  button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/>'
    + '<path d="m15.3 8.7-1.9 4.7-4.7 1.9 1.9-4.7 4.7-1.9Z"/></svg><span></span>';
  var caption = button.querySelector('span');
  caption.textContent = label;
  root.appendChild(button);

  var panel = document.createElement('div');
  panel.id = 'alex-widget-panel';
  panel.hidden = true;
  var frame = null;

  function toggle(open) {
    if (open && !frame) {
      frame = document.createElement('iframe');
      frame.title = text.title;
      frame.allow = 'clipboard-write; fullscreen';
      frame.referrerPolicy = 'strict-origin-when-cross-origin';
      frame.src = source + (source.indexOf('?') < 0 ? '?' : '&') + 'embed=1&lang=' + language;
      panel.appendChild(frame);
    }
    panel.hidden = !open;
    root.classList.toggle('is-open', open);
    button.setAttribute('aria-expanded', String(open));
    caption.textContent = open ? text.close : label;
    if (open && frame) frame.focus();
  }

  button.addEventListener('click', function () { toggle(panel.hidden); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !panel.hidden) {
      toggle(false);
      button.focus();
    }
  });

  document.body.appendChild(panel);
  document.body.appendChild(root);
})();
