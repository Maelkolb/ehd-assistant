/** Map artifact: numbered stations on a Leaflet map, optionally joined as a route. */

import { h, escapeHtml } from '../dom.js';
import { t, formatDate } from '../i18n.js';
import { vizColors } from '../theme.js';
import { loadLeaflet, assetUrl } from './lazy.js';
import { exportStandalone } from './export.js';

const yearOf = (date) => /(\d{4})/.exec(date || '')?.[1] || '';

/** Stations grouped by their `group`, else by year; groups keep first-seen order. */
function groupsOf(stations) {
  const keys = [];
  for (const station of stations) {
    const key = station.group || yearOf(station.date);
    if (key && !keys.includes(key)) keys.push(key);
  }
  return keys;
}

function colorFor(station, groups, colors) {
  if (groups.length < 2) return colors.accent;
  const position = groups.indexOf(station.group || yearOf(station.date));
  return position < 0 ? colors.muted : colors.series[Math.min(position, colors.series.length - 1)];
}

function popupContent(station, number) {
  const node = h('div', { class: 'map-popup' },
    h('strong', { text: `${number}. ${station.label || ''}` }));
  if (station.date) node.append(h('div', { class: 'map-popup-date', text: formatDate(station.date) }));
  if (station.description) node.append(h('div', { text: station.description }));
  if (station.url && /^https?:\/\//.test(station.url)) {
    node.append(h('a', { href: station.url, target: '_blank', rel: 'noopener noreferrer',
                         text: t('map.register') }));
  }
  return node;
}

export function renderMap(artifact, body, foot, context) {
  const stations = (artifact.stations || []).filter((station) => (
    Number.isFinite(station.lat) && Number.isFinite(station.lon)));
  const canvas = h('div', { class: 'map-canvas', role: 'application',
                            'aria-label': artifact.title || t('artifact.type.map') },
    h('p', { class: 'artifact-loading', 'data-i18n': 'map.loading', text: t('map.loading') }));
  body.append(canvas);

  const groups = groupsOf(stations);
  let map = null;
  let markers = [];
  let route = null;
  let bounds = null;

  const listItems = stations.map((station, index) => {
    const button = h('button', { type: 'button', class: 'station-button' },
      h('span', { class: 'station-number', text: String(index + 1) }),
      h('span', { class: 'station-label', text: station.label || '' }),
      station.date ? h('span', { class: 'station-date', text: formatDate(station.date) }) : null);
    button.addEventListener('click', () => {
      if (!map || !markers[index]) return;
      map.setView([station.lat, station.lon], Math.max(map.getZoom(), 6), { animate: false });
      markers[index].openPopup();
    });
    return h('li', {}, button);
  });
  const legend = h('div', { class: 'legend' });
  foot.append(legend, h('details', { class: 'artifact-table' },
    h('summary', {}, h('span', { 'data-i18n': 'map.stations', text: t('map.stations') }),
      ` (${stations.length})`),
    h('ol', { class: 'station-list' }, listItems)));

  function paint() {
    const colors = vizColors();
    legend.replaceChildren(...(groups.length > 1 ? groups.map((group, position) => {
      const swatch = h('span', { class: 'legend-swatch' });
      swatch.style.setProperty('--swatch', colors.series[Math.min(position, colors.series.length - 1)]);
      return h('span', { class: 'legend-item' }, swatch, group);
    }) : []));
    markers.forEach((marker, index) => {
      marker.getElement()?.style.setProperty('--marker', colorFor(stations[index], groups, colors));
    });
    if (route) route.setStyle({ color: colors.ink2 });
  }

  function fit() {
    if (!map) return;
    map.invalidateSize();
    if (stations.length === 1) map.setView([stations[0].lat, stations[0].lon], 7);
    else if (bounds) map.fitBounds(bounds, { padding: [32, 32], maxZoom: 10 });
  }

  const ready = loadLeaflet().then((L) => {
    canvas.replaceChildren();
    const tiles = context.config?.map || {};
    map = L.map(canvas, { scrollWheelZoom: false, worldCopyJump: true });
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
    L.tileLayer(tiles.tile_url || 'https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png', {
      attribution: escapeHtml(tiles.attribution || '© OpenStreetMap'),
      maxZoom: tiles.max_zoom || 18,
      referrerPolicy: 'strict-origin-when-cross-origin',
    }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    map.on('click focus', () => map.scrollWheelZoom.enable());

    const points = stations.map((station) => [station.lat, station.lon]);
    if (artifact.connect_route !== false && points.length > 1) {
      route = L.polyline(points, { weight: 2, opacity: 0.8, dashArray: '2 7', lineCap: 'round' }).addTo(map);
    }
    markers = stations.map((station, index) => {
      const marker = L.marker([station.lat, station.lon], {
        icon: L.divIcon({ className: 'map-marker', html: String(index + 1), iconSize: [24, 24],
                          iconAnchor: [12, 12], popupAnchor: [0, -12] }),
        title: station.label || '',
        alt: `${index + 1}. ${station.label || ''}`,
        keyboard: true,
      }).addTo(map);
      marker.bindPopup(() => popupContent(station, index + 1));
      return marker;
    });
    bounds = points.length ? L.latLngBounds(points) : null;
    paint();
    fit();
    if (!points.length) map.setView([20, 0], 2);
  }).catch(() => {
    canvas.replaceChildren(h('p', { class: 'artifact-error', text: t('chart.failed') }));
  });

  return {
    ready,
    resize: fit,
    retheme: paint,
    destroy: () => {
      if (!map) return;
      map.stop();
      map.closePopup();
      map.remove();
      map = null;
    },
    downloadLabel: 'artifact.download.html',
    download: () => exportMap(artifact, stations, groups, context),
  };
}

async function exportMap(artifact, stations, groups, context) {
  const colors = vizColors();
  const tiles = context.config?.map || {};
  const data = {
    connect: artifact.connect_route !== false,
    tileUrl: tiles.tile_url || 'https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png',
    attribution: tiles.attribution || '© OpenStreetMap',
    maxZoom: tiles.max_zoom || 18,
    route: colors.ink2,
    registerLabel: t('map.register'),
    stations: stations.map((station) => ({
      lat: station.lat, lon: station.lon, label: station.label || '',
      date: station.date ? formatDate(station.date) : '', description: station.description || '',
      url: /^https?:\/\//.test(station.url || '') ? station.url : '',
      color: colorFor(station, groups, colors),
    })),
  };
  const script = `
const map = L.map('map');
L.tileLayer(DATA.tileUrl, { attribution: DATA.attribution, maxZoom: DATA.maxZoom }).addTo(map);
L.control.scale({ imperial: false }).addTo(map);
const points = DATA.stations.map((station) => [station.lat, station.lon]);
if (DATA.connect && points.length > 1) {
  L.polyline(points, { color: DATA.route, weight: 2, opacity: 0.8, dashArray: '2 7' }).addTo(map);
}
DATA.stations.forEach((station, index) => {
  const marker = L.marker([station.lat, station.lon], {
    icon: L.divIcon({ className: 'map-marker', html: String(index + 1), iconSize: [24, 24], iconAnchor: [12, 12] }),
  }).addTo(map);
  marker.getElement().style.setProperty('--marker', station.color);
  const popup = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = (index + 1) + '. ' + station.label;
  popup.append(title);
  for (const line of [station.date, station.description]) {
    if (!line) continue;
    const row = document.createElement('div');
    row.textContent = line;
    popup.append(row);
  }
  if (station.url) {
    const link = document.createElement('a');
    link.href = station.url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = DATA.registerLabel;
    popup.append(link);
  }
  marker.bindPopup(popup);
});
if (points.length === 1) map.setView(points[0], 7);
else if (points.length) map.fitBounds(points, { padding: [32, 32], maxZoom: 10 });
`;
  await exportStandalone(artifact.title || t('artifact.type.map'),
    '<div id="map" class="map-canvas map-canvas-export"></div>', {
      head: `<link rel="stylesheet" href="${assetUrl('vendor/leaflet/leaflet.css')}">
<script src="${assetUrl('vendor/leaflet/leaflet.js')}"><\/script>`,
      script, data,
    });
}
