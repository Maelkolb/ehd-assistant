/** Figure cards for the visualisations an answer produces. */

import { h } from '../dom.js';
import { icon, artifactIcon } from '../icons.js';
import { t } from '../i18n.js';
import { onThemeChange } from '../theme.js';
import { renderMap } from './map.js';
import { renderTimeline } from './timeline.js';
import { renderNetwork } from './network.js';
import { renderChart } from './chart.js';
import { renderImage } from './image.js';
import { renderTable } from './table.js';

const RENDERERS = { map: renderMap, timeline: renderTimeline, network: renderNetwork,
                    chart: renderChart, image: renderImage, table: renderTable };
const FULLSCREEN_TYPES = new Set(['map', 'network', 'chart', 'image', 'table']);

export class ArtifactCard {
  constructor(artifact, context) {
    this.artifact = artifact;
    this.context = context;
    const type = RENDERERS[artifact.type] ? artifact.type : 'table';
    this.body = h('div', { class: 'artifact-body' });
    this.foot = h('div', { class: 'artifact-foot' });
    this.actions = h('div', { class: 'artifact-actions' });
    this.element = h('figure', { class: `artifact artifact-${type}`, dataset: { id: artifact.id || '' } },
      h('figcaption', { class: 'artifact-head' },
        h('span', { class: 'artifact-kind' }, artifactIcon(type),
          h('span', { class: 'visually-hidden', 'data-i18n': `artifact.type.${type}`,
                      text: t(`artifact.type.${type}`) })),
        h('span', { class: 'artifact-title', text: artifact.title || t(`artifact.type.${type}`) }),
        this.actions),
      this.body, this.foot);

    try {
      this.controller = (RENDERERS[artifact.type] || renderTable)(artifact, this.body, this.foot, context) || {};
    } catch (error) {
      console.error(error);
      this.controller = {};
      this.body.replaceChildren(h('p', { class: 'artifact-error', text: t('chart.failed') }));
    }
    if (!this.foot.childNodes.length) this.foot.hidden = true;

    if (this.controller.download) {
      this.actions.append(this.button('download', this.controller.downloadLabel || 'artifact.download',
        () => this.controller.download()));
    }
    if (FULLSCREEN_TYPES.has(type) && document.fullscreenEnabled) {
      this.actions.append(this.button('maximize', 'artifact.fullscreen', () => this.toggleFullscreen()));
      this.onFullscreen = () => setTimeout(() => this.controller.resize?.(), 120);
      this.element.addEventListener('fullscreenchange', this.onFullscreen);
    }
    this.stopTheme = onThemeChange(() => this.controller.retheme?.());
    if (typeof ResizeObserver === 'function' && this.controller.resize) {
      let width = 0;
      this.observer = new ResizeObserver((entries) => {
        const next = Math.round(entries[0].contentRect.width);
        if (width && next !== width) this.controller.resize();
        width = next;
      });
      this.observer.observe(this.body);
    }
  }

  button(iconName, labelKey, onClick) {
    return h('button', { type: 'button', class: 'icon-button', 'data-i18n-title': labelKey,
                         'data-i18n-label': labelKey, title: t(labelKey), 'aria-label': t(labelKey),
                         onClick }, icon(iconName));
  }

  toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else this.element.requestFullscreen?.().catch(() => {});
  }

  destroy() {
    this.stopTheme?.();
    this.observer?.disconnect();
    this.controller.destroy?.();
    this.element.remove();
  }
}

/** Keeps one card per artifact id inside a container; a repeated id replaces its card. */
export class ArtifactList {
  constructor(context) {
    this.context = context;
    this.cards = new Map();
    this.element = h('div', { class: 'artifacts' });
  }

  upsert(artifact) {
    const key = artifact.id || `${artifact.type}:${artifact.title || this.cards.size}`;
    const card = new ArtifactCard(artifact, this.context);
    const previous = this.cards.get(key);
    if (previous) {
      previous.element.replaceWith(card.element);
      previous.destroy();
    } else {
      this.element.append(card.element);
    }
    this.cards.set(key, card);
    return card;
  }

  /** Make the cards match `artifacts` exactly, reusing cards whose data is unchanged. */
  sync(artifacts) {
    const wanted = new Set();
    for (const artifact of artifacts || []) {
      const key = artifact.id || `${artifact.type}:${artifact.title || wanted.size}`;
      wanted.add(key);
      const existing = this.cards.get(key);
      if (!existing || JSON.stringify(existing.artifact) !== JSON.stringify(artifact)) this.upsert(artifact);
    }
    for (const [key, card] of this.cards) {
      if (!wanted.has(key)) {
        card.destroy();
        this.cards.delete(key);
      }
    }
  }

  destroy() {
    for (const card of this.cards.values()) card.destroy();
    this.cards.clear();
  }
}
