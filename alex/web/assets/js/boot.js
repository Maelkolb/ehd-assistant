/* Runs before first paint (classic script, no module): applies the stored
   theme and language so the page never flashes the wrong one. */
(function () {
  var root = document.documentElement;
  var parameters = new URLSearchParams(location.search);
  try {
    var theme = localStorage.getItem('alex.theme');
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    var language = parameters.get('lang') || localStorage.getItem('alex.lang') ||
      ((navigator.language || 'de').toLowerCase().indexOf('de') === 0 ? 'de' : 'en');
    root.lang = language === 'en' ? 'en' : 'de';
  } catch (error) {
    root.lang = 'de';
  }
  if (parameters.get('embed') === '1') root.setAttribute('data-embed', '');
})();
