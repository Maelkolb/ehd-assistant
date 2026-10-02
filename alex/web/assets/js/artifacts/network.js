/** Network artifact: a small force-directed graph on canvas.

    `mountNetwork` is deliberately self-contained (no imports, no closures over
    module state): the standalone HTML export embeds its source text and calls
    it with the same data, so there is exactly one copy of the drawing code. */

import { h } from '../dom.js';
import { t, kindLabel } from '../i18n.js';
import { vizColors, KIND_SLOTS } from '../theme.js';
import { exportStandalone } from './export.js';

const SHAPES = { person: 'circle', place: 'square', institution: 'diamond', plant: 'triangle',
                 glossary: 'hexagon', siglum: 'ring' };

export function mountNetwork(wrap, graph, style) {
  const canvas = document.createElement('canvas');
  canvas.className = 'network-canvas';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', style.label || '');
  const tip = document.createElement('div');
  tip.className = 'network-tip';
  tip.hidden = true;
  wrap.append(canvas, tip);

  const nodes = graph.nodes.map((node) => ({ ...node }));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = graph.edges.filter((edge) => byId.has(edge.source) && byId.has(edge.target));
  const maxWeight = Math.max(1, ...nodes.filter((node) => !node.focus).map((node) => node.weight || 1));
  const maxEdge = Math.max(1, ...edges.map((edge) => edge.weight || 1));
  const labelAll = edges.some((edge) => edge.label) && edges.length <= 12;
  const ratio = window.devicePixelRatio || 1;
  const context = canvas.getContext('2d');
  let width = 0;
  let height = 0;
  let hover = null;
  let hoverEdge = null;
  let drag = null;
  let moved = false;
  let ticks = 0;
  let running = false;
  let frame = 0;

  function size() {
    width = wrap.clientWidth || 640;
    height = wrap.clientHeight || 420;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function place() {
    nodes.forEach((node, index) => {
      node.r = node.focus ? 15 : 5 + 9 * Math.sqrt((node.weight || 1) / maxWeight);
      const angle = (2 * Math.PI * index) / Math.max(1, nodes.length - 1);
      node.x = node.focus ? width / 2 : width / 2 + Math.cos(angle) * width * 0.28 + (index % 7) * 3;
      node.y = node.focus ? height / 2 : height / 2 + Math.sin(angle) * height * 0.3 + (index % 5) * 3;
      node.vx = 0;
      node.vy = 0;
    });
  }

  function step() {
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const d2 = dx * dx + dy * dy + 0.01;
        const d = Math.sqrt(d2);
        const force = 4200 / d2;
        dx /= d;
        dy /= d;
        a.vx += dx * force;
        a.vy += dy * force;
        b.vx -= dx * force;
        b.vy -= dy * force;
      }
    }
    for (const edge of edges) {
      const a = byId.get(edge.source);
      const b = byId.get(edge.target);
      const rest = Math.min(width, height) * 0.42 - 70 * Math.sqrt((edge.weight || 1) / maxEdge);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const force = (0.02 * (d - rest)) / d;
      a.vx += dx * force;
      a.vy += dy * force;
      b.vx -= dx * force;
      b.vy -= dy * force;
    }
    for (const node of nodes) {
      node.vx += (width / 2 - node.x) * 0.004;
      node.vy += (height / 2 - node.y) * 0.006;
      if (!node.focus && node !== drag) {
        node.vx *= 0.85;
        node.vy *= 0.85;
        node.x += node.vx;
        node.y += node.vy;
      }
      node.x = Math.max(node.r + 4, Math.min(width - node.r - 4, node.x));
      node.y = Math.max(node.r + 4, Math.min(height - node.r - 18, node.y));
    }
  }

  function shapePath(node) {
    const { x, y, r } = node;
    const shape = style.shapes[node.kind] || 'circle';
    context.beginPath();
    if (shape === 'square') {
      const side = r * 1.7;
      context.roundRect(x - side / 2, y - side / 2, side, side, 3);
    } else if (shape === 'diamond') {
      context.moveTo(x, y - r * 1.25);
      context.lineTo(x + r * 1.25, y);
      context.lineTo(x, y + r * 1.25);
      context.lineTo(x - r * 1.25, y);
      context.closePath();
    } else if (shape === 'triangle') {
      context.moveTo(x, y - r * 1.25);
      context.lineTo(x + r * 1.2, y + r * 0.9);
      context.lineTo(x - r * 1.2, y + r * 0.9);
      context.closePath();
    } else if (shape === 'hexagon') {
      for (let corner = 0; corner < 6; corner += 1) {
        const angle = (Math.PI / 3) * corner;
        context.lineTo(x + r * 1.1 * Math.cos(angle), y + r * 1.1 * Math.sin(angle));
      }
      context.closePath();
    } else {
      context.arc(x, y, r, 0, Math.PI * 2);
    }
  }

  function draw() {
    context.clearRect(0, 0, width, height);
    context.lineCap = 'round';
    for (const edge of edges) {
      const a = byId.get(edge.source);
      const b = byId.get(edge.target);
      const hot = edge === hoverEdge || (hover && (a === hover || b === hover));
      context.globalAlpha = hot ? 0.9 : 0.25 + 0.35 * ((edge.weight || 1) / maxEdge);
      context.strokeStyle = hot ? style.ink : style.edge;
      context.lineWidth = (hot ? 1.5 : 0.75) + 2 * ((edge.weight || 1) / maxEdge);
      context.beginPath();
      context.moveTo(a.x, a.y);
      context.lineTo(b.x, b.y);
      context.stroke();
    }
    context.globalAlpha = 1;
    context.font = `11px ${style.font}`;
    context.textAlign = 'center';
    if (labelAll || hoverEdge) {
      for (const edge of labelAll ? edges : [hoverEdge]) {
        if (!edge || !edge.label) continue;
        const a = byId.get(edge.source);
        const b = byId.get(edge.target);
        const x = (a.x + b.x) / 2;
        const y = (a.y + b.y) / 2;
        const labelWidth = context.measureText(edge.label).width;
        context.fillStyle = style.surface;
        context.fillRect(x - labelWidth / 2 - 3, y - 9, labelWidth + 6, 14);
        context.fillStyle = style.muted;
        context.fillText(edge.label, x, y + 2);
      }
    }
    for (const node of nodes) {
      shapePath(node);
      context.fillStyle = node.color || style.colors[node.kind] || style.fallback;
      context.fill();
      context.lineWidth = 2;
      context.strokeStyle = node === hover || node.focus ? style.ink : style.surface;
      context.stroke();
    }
    const labelled = new Set(nodes.slice().sort((a, b) => b.r - a.r).slice(0, 14));
    if (hover) labelled.add(hover);
    context.font = `12px ${style.font}`;
    for (const node of labelled) {
      const text = node.label.length > 26 ? node.label.slice(0, 25) + '…' : node.label;
      const textWidth = context.measureText(text).width;
      const x = Math.max(textWidth / 2 + 4, Math.min(width - textWidth / 2 - 4, node.x));
      const y = node.y + node.r + 14;
      context.globalAlpha = 0.85;
      context.fillStyle = style.surface;
      context.fillRect(x - textWidth / 2 - 3, y - 11, textWidth + 6, 15);
      context.globalAlpha = 1;
      context.fillStyle = style.ink;
      context.fillText(text, x, y);
    }
  }

  function kick(count) {
    ticks = Math.max(ticks, count);
    if (running) return;
    running = true;
    const loop = () => {
      for (let i = 0; i < 3; i += 1) step();
      draw();
      ticks -= 1;
      if (ticks > 0) frame = requestAnimationFrame(loop);
      else running = false;
    };
    loop();
  }

  function settle() {
    for (let i = 0; i < 260; i += 1) step();
    draw();
  }

  function position(event) {
    const box = canvas.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  function nodeAt(x, y) {
    return nodes.find((node) => (node.x - x) ** 2 + (node.y - y) ** 2 < (node.r + 5) ** 2) || null;
  }

  function edgeAt(x, y) {
    return edges.find((edge) => {
      if (!edge.label) return false;
      const a = byId.get(edge.source);
      const b = byId.get(edge.target);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const along = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / (dx * dx + dy * dy || 1)));
      return Math.hypot(a.x + along * dx - x, a.y + along * dy - y) < 6;
    }) || null;
  }

  function showTip(lines, x, y) {
    tip.replaceChildren(...lines.map((line, index) => {
      const row = document.createElement(index ? 'div' : 'strong');
      row.textContent = line;
      return row;
    }));
    tip.hidden = false;
    tip.style.left = Math.min(x + 14, width - 190) + 'px';
    tip.style.top = Math.max(4, y - 30) + 'px';
  }

  canvas.addEventListener('pointermove', (event) => {
    const [x, y] = position(event);
    if (drag) {
      drag.x = x;
      drag.y = y;
      drag.vx = 0;
      drag.vy = 0;
      moved = true;
      kick(30);
      return;
    }
    hover = nodeAt(x, y);
    hoverEdge = hover || labelAll ? null : edgeAt(x, y);
    canvas.style.cursor = hover ? (hover.url ? 'pointer' : 'grab') : 'default';
    if (hover) {
      const kind = style.kindLabels[hover.kind] || hover.kind || '';
      const lines = [hover.label, hover.focus ? kind : `${kind} · ${hover.weight ?? ''}`.replace(/ · $/, '')];
      if (hover.description) lines.push(hover.description);
      showTip(lines, x, y);
    } else if (hoverEdge) {
      showTip([hoverEdge.label, `${byId.get(hoverEdge.source).label} – ${byId.get(hoverEdge.target).label}`], x, y);
    } else {
      tip.hidden = true;
    }
    if (!running) draw();
  });
  canvas.addEventListener('pointerdown', (event) => {
    const [x, y] = position(event);
    drag = nodeAt(x, y);
    moved = false;
    if (drag) {
      canvas.setPointerCapture(event.pointerId);
      canvas.style.cursor = 'grabbing';
      event.preventDefault();
    }
  });
  const release = () => {
    if (drag) {
      drag = null;
      kick(20);
    }
  };
  canvas.addEventListener('pointerup', release);
  canvas.addEventListener('pointercancel', release);
  canvas.addEventListener('pointerleave', () => {
    hover = null;
    hoverEdge = null;
    tip.hidden = true;
    if (!running) draw();
  });
  canvas.addEventListener('click', () => {
    if (hover && hover.url && !moved && /^https?:\/\//.test(hover.url)) {
      window.open(hover.url, '_blank', 'noopener');
    }
  });

  size();
  place();
  if (style.animate === false) settle();
  else kick(130);

  return {
    resize() {
      const before = [width, height];
      size();
      for (const node of nodes) {
        node.x *= width / (before[0] || width);
        node.y *= height / (before[1] || height);
      }
      kick(40);
    },
    restyle(next) {
      Object.assign(style, next);
      draw();
    },
    destroy() {
      cancelAnimationFrame(frame);
      ticks = 0;
    },
  };
}

function normalised(artifact) {
  const nodes = (artifact.nodes || []).filter((node) => node && node.id !== undefined).map((node) => ({
    id: String(node.id), label: String(node.label ?? node.id), kind: node.kind || node.type || 'other',
    weight: Number(node.weight) || 1, url: node.url || '', description: node.description || '',
    focus: Boolean(node.focus), color: /^#[0-9a-f]{6}$/i.test(node.color || '') ? node.color : '',
  }));
  const edges = (artifact.edges || []).filter(Boolean).map((edge) => ({
    source: String(edge.source), target: String(edge.target),
    weight: Number(edge.weight) || 1, label: edge.label || '',
  }));
  return { nodes, edges };
}

function styleFor(kinds, title) {
  const colors = vizColors();
  const kindColors = {};
  const free = [5, 8, 4, 7, 6, 3, 2, 1].filter((slot) => !kinds.some((kind) => KIND_SLOTS[kind] === slot));
  for (const kind of kinds) {
    const slot = KIND_SLOTS[kind] ?? free.pop() ?? 8;
    kindColors[kind] = colors.series[slot - 1];
  }
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  return {
    colors: kindColors, shapes: SHAPES, fallback: colors.muted, ink: colors.ink, muted: colors.muted,
    edge: colors.muted, surface: colors.surface, font: colors.fontSans, label: title,
    kindLabels: Object.fromEntries(kinds.map((kind) => [kind, kindLabel(kind)])),
    animate: !reduced,
  };
}

function legendNodes(kinds, style) {
  return kinds.map((kind) => {
    const swatch = h('span', { class: `legend-swatch legend-shape-${SHAPES[kind] || 'circle'}` });
    swatch.style.setProperty('--swatch', style.colors[kind]);
    return h('span', { class: 'legend-item' }, swatch, style.kindLabels[kind] || kind);
  });
}

export function renderNetwork(artifact, body, foot) {
  const graph = normalised(artifact);
  const kinds = [...new Set(graph.nodes.map((node) => node.kind))];
  const wrap = h('div', { class: 'network-wrap' });
  body.append(wrap);
  let style = styleFor(kinds, artifact.title || '');
  const legend = h('div', { class: 'legend' }, legendNodes(kinds, style));
  const rows = [...graph.nodes].sort((a, b) => b.weight - a.weight).map((node) => h('tr', {},
    h('td', {}, node.url && /^https?:\/\//.test(node.url)
      ? h('a', { href: node.url, target: '_blank', rel: 'noopener noreferrer', text: node.label })
      : node.label),
    h('td', { text: style.kindLabels[node.kind] || node.kind }),
    h('td', { class: 'numeric', text: String(node.weight) })));
  foot.append(legend,
    h('details', { class: 'artifact-table' },
      h('summary', { 'data-i18n': 'artifact.tableView', text: t('artifact.tableView') }),
      h('div', { class: 'table-scroll' }, h('table', {},
        h('thead', {}, h('tr', {},
          h('th', { 'data-i18n': 'network.node', text: t('network.node') }),
          h('th', { 'data-i18n': 'network.kind', text: t('network.kind') }),
          h('th', { class: 'numeric', 'data-i18n': 'network.weight', text: t('network.weight') }))),
        h('tbody', {}, rows)))));

  let controller = null;
  const start = () => {
    if (!controller && wrap.clientWidth) controller = mountNetwork(wrap, graph, style);
  };
  requestAnimationFrame(start);

  return {
    resize: () => (controller ? controller.resize() : start()),
    retheme: () => {
      style = styleFor(kinds, artifact.title || '');
      legend.replaceChildren(...legendNodes(kinds, style));
      controller?.restyle(style);
    },
    destroy: () => controller?.destroy(),
    downloadLabel: 'artifact.download.html',
    download: () => exportStandalone(artifact.title || t('artifact.type.network'),
      `<div id="network" class="network-wrap network-wrap-export"></div><div class="artifact-foot">${legend.outerHTML}</div>`, {
        data: { graph, style: { ...style, animate: true } },
        script: `${mountNetwork.toString()}\nconst wrap = document.getElementById('network');\n`
          + 'const controller = mountNetwork(wrap, DATA.graph, DATA.style);\n'
          + "window.addEventListener('resize', () => controller.resize());",
      }),
  };
}
