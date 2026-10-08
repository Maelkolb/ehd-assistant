/** Inline SVG icons: 24×24, 1.75 stroke, currentColor. */

const PATHS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
  regex: '<path d="M17 3v10"/><path d="m12.7 5.5 8.6 5"/><path d="m12.7 10.5 8.6-5"/><rect x="3.5" y="15.5" width="5" height="5" rx="1"/>',
  book: '<path d="M12 6.5c-1.6-1.3-4-2-7-2v13c3 0 5.4.7 7 2 1.6-1.3 4-2 7-2v-13c-3 0-5.4.7-7 2Z"/><path d="M12 6.5v13"/>',
  list: '<path d="M8.5 6.5H20"/><path d="M8.5 12H20"/><path d="M8.5 17.5H20"/><circle cx="4.5" cy="6.5" r=".9"/><circle cx="4.5" cy="12" r=".9"/><circle cx="4.5" cy="17.5" r=".9"/>',
  card: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2.2"/><path d="M5.8 16.2c.6-1.5 1.8-2.2 3.2-2.2s2.6.7 3.2 2.2"/><path d="M14.5 10h3.5"/><path d="M14.5 13.5h3.5"/>',
  mentions: '<path d="M5 4.5h14a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-6.5L8 20.5v-4H5A1.5 1.5 0 0 1 3.5 15V6A1.5 1.5 0 0 1 5 4.5Z"/><path d="M8 9h8"/><path d="M8 12.5h5"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17"/><path d="M8 3v4"/><path d="M16 3v4"/>',
  database: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/><path d="M4.5 12v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/>',
  code: '<path d="m8.5 7.5-5 4.5 5 4.5"/><path d="m15.5 7.5 5 4.5-5 4.5"/><path d="m13.5 4.5-3 15"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11Z"/><circle cx="12" cy="10" r="2.3"/>',
  timeline: '<path d="M6 3.5v17"/><circle cx="6" cy="7" r="1.8"/><circle cx="6" cy="13" r="1.8"/><circle cx="6" cy="19" r="1.3"/><path d="M11 7h9"/><path d="M11 13h7"/><path d="M11 19h4.5"/>',
  network: '<circle cx="12" cy="5.5" r="2.3"/><circle cx="5.5" cy="17.5" r="2.3"/><circle cx="18.5" cy="17.5" r="2.3"/><path d="m10.9 7.5-4.3 8"/><path d="m13.1 7.5 4.3 8"/><path d="M7.8 17.5h8.4"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5S9.7 5.9 12 3.5Z"/>',
  tool: '<path d="M14.5 6.5a4 4 0 0 0-5.3 5.3L4 17l3 3 5.2-5.2a4 4 0 0 0 5.3-5.3l-2.7 2.7-2.5-.5-.5-2.5 2.7-2.7Z"/>',
  thought: '<path d="M12 4.5a6 6 0 0 0-3.5 10.9V17.5h7v-2.1A6 6 0 0 0 12 4.5Z"/><path d="M9.5 20.5h5"/>',
  compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.3 8.7-1.9 4.7-4.7 1.9 1.9-4.7 4.7-1.9Z"/>',
  send: '<path d="M12 19.5v-15"/><path d="m5.5 11 6.5-6.5 6.5 6.5"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
  sun: '<circle cx="12" cy="12" r="3.8"/><path d="M12 2.5v2.5"/><path d="M12 19v2.5"/><path d="M2.5 12H5"/><path d="M19 12h2.5"/><path d="m5.3 5.3 1.8 1.8"/><path d="m16.9 16.9 1.8 1.8"/><path d="m5.3 18.7 1.8-1.8"/><path d="m16.9 7.1 1.8-1.8"/>',
  moon: '<path d="M19.5 14.2A8 8 0 0 1 9.8 4.5a8 8 0 1 0 9.7 9.7Z"/>',
  monitor: '<rect x="3" y="4.5" width="18" height="12" rx="1.8"/><path d="M8.5 20.5h7"/><path d="M12 16.5v4"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".6" fill="currentColor"/>',
  close: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2"/><path d="M5.5 15.5H5A1.5 1.5 0 0 1 3.5 14V5A1.5 1.5 0 0 1 5 3.5h9A1.5 1.5 0 0 1 15.5 5v.5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  thumbUp: '<path d="M7.5 10.5 11 3.5c1.4 0 2.5 1.1 2.5 2.5v3.5h4.7a2 2 0 0 1 2 2.4l-1.2 6a2 2 0 0 1-2 1.6H7.5"/><path d="M7.5 10.5v9H5A1.5 1.5 0 0 1 3.5 18v-6A1.5 1.5 0 0 1 5 10.5h2.5Z"/>',
  thumbDown: '<path d="M16.5 13.5 13 20.5c-1.4 0-2.5-1.1-2.5-2.5v-3.5H5.8a2 2 0 0 1-2-2.4l1.2-6a2 2 0 0 1 2-1.6h9.5"/><path d="M16.5 13.5v-9H19A1.5 1.5 0 0 1 20.5 6v6a1.5 1.5 0 0 1-1.5 1.5h-2.5Z"/>',
  external: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/><path d="M17.5 13.5V18A1.5 1.5 0 0 1 16 19.5H6A1.5 1.5 0 0 1 4.5 18V8A1.5 1.5 0 0 1 6 6.5h4.5"/>',
  download: '<path d="M12 4v11"/><path d="m7 10.5 5 5 5-5"/><path d="M5 19.5h14"/>',
  maximize: '<path d="M4.5 9.5v-5h5"/><path d="M19.5 9.5v-5h-5"/><path d="M4.5 14.5v5h5"/><path d="M19.5 14.5v5h-5"/>',
  chevronDown: '<path d="m6 9.5 6 6 6-6"/>',
  chevronRight: '<path d="m9.5 6 6 6-6 6"/>',
  chevronUp: '<path d="m6 14.5 6-6 6 6"/>',
  alert: '<path d="M12 4 2.8 19.5h18.4L12 4Z"/><path d="M12 10v4.5"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="1.8"/><path d="M3.5 9.5h17"/><path d="M3.5 14.5h17"/><path d="M9.5 4.5v15"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="1.8"/><circle cx="9" cy="10" r="1.6"/><path d="m4.5 18 5-4.5 3.5 3 3-2.5 4 4"/>',
  chart: '<path d="M4 4v16h16"/><path d="M8.5 16v-5"/><path d="M12.5 16V8"/><path d="M16.5 16v-3"/>',
  retry: '<path d="M4.5 12a7.5 7.5 0 0 1 13-5.1L19.5 9"/><path d="M19.5 4.5V9H15"/><path d="M19.5 12a7.5 7.5 0 0 1-13 5.1L4.5 15"/><path d="M4.5 19.5V15H9"/>',
  facsimile: '<path d="M6 3.5h8l4.5 4.5V19A1.5 1.5 0 0 1 17 20.5H6A1.5 1.5 0 0 1 4.5 19V5A1.5 1.5 0 0 1 6 3.5Z"/><path d="M14 3.5V8h4.5"/><circle cx="11" cy="13.5" r="2.5"/><path d="m13 15.5 2 2"/>',
  quote: '<path d="M5 17.5c2.5-1 3.5-3 3.5-6V7H4v5h3"/><path d="M14 17.5c2.5-1 3.5-3 3.5-6V7H13v5h3"/>',
  chat: '<path d="M5 4.5h14A1.5 1.5 0 0 1 20.5 6v9a1.5 1.5 0 0 1-1.5 1.5h-7L7.5 20.5v-4H5A1.5 1.5 0 0 1 3.5 15V6A1.5 1.5 0 0 1 5 4.5Z"/>',
};

const TOOL_ICONS = {
  search: 'search',
  grep: 'regex',
  read_document: 'book',
  list_documents: 'list',
  lookup_entity: 'card',
  entity_mentions: 'mentions',
  chronology: 'calendar',
  query_data: 'database',
  run_python: 'code',
  create_map: 'pin',
  create_timeline: 'timeline',
  create_network: 'network',
  dated_passages: 'calendar',
  entities_in_document: 'mentions',
  itinerary: 'compass',
};

export function icon(name, className) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.75');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', 'icon' + (className ? ' ' + className : ''));
  svg.innerHTML = PATHS[name] || PATHS.tool;
  return svg;
}

export function toolIcon(toolName) {
  return icon(TOOL_ICONS[toolName] || 'tool');
}

export function artifactIcon(type) {
  return icon({ map: 'pin', timeline: 'timeline', network: 'network', chart: 'chart',
                image: 'image', table: 'table' }[type] || 'chart');
}
