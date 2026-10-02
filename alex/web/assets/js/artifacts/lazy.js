/** Load vendored libraries only when a visualisation needs them. */

const pending = new Map();

export function assetUrl(path) {
  return new URL(`assets/${path}`, document.baseURI).href;
}

export function loadScript(path) {
  const url = assetUrl(path);
  if (!pending.has(url)) {
    pending.set(url, new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url;
      script.onload = resolve;
      script.onerror = () => {
        pending.delete(url);
        reject(new Error(`Could not load ${path}`));
      };
      document.head.append(script);
    }));
  }
  return pending.get(url);
}

export function loadStyle(path) {
  const url = assetUrl(path);
  if (!pending.has(url)) {
    pending.set(url, new Promise((resolve, reject) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = url;
      link.onload = resolve;
      link.onerror = () => {
        pending.delete(url);
        reject(new Error(`Could not load ${path}`));
      };
      document.head.append(link);
    }));
  }
  return pending.get(url);
}

export async function loadLeaflet() {
  await Promise.all([loadStyle('vendor/leaflet/leaflet.css'), loadScript('vendor/leaflet/leaflet.js')]);
  return window.L;
}

export async function loadPlotly() {
  await loadScript('vendor/plotly/plotly-cartesian.min.js');
  return window.Plotly;
}

const texts = new Map();

/** Text of one of the app's own stylesheets, for standalone exports. */
export function stylesheetText(path) {
  const url = assetUrl(path);
  if (!texts.has(url)) texts.set(url, fetch(url).then((response) => response.text()));
  return texts.get(url);
}
