/** Image artifact: a figure rendered by Python. */

import { h, download, slug } from '../dom.js';
import { t } from '../i18n.js';

const SAFE_MEDIA = /^image\/(png|jpeg|gif|webp|svg\+xml)$/;

function dataUrl(artifact) {
  const media = SAFE_MEDIA.test(artifact.media_type || '') ? artifact.media_type : 'image/png';
  return `data:${media};base64,${artifact.data}`;
}

export function renderImage(artifact, body) {
  const image = h('img', { class: 'image-figure', alt: artifact.alt || artifact.title || '',
                           loading: 'lazy', decoding: 'async' });
  image.addEventListener('error', () => {
    image.replaceWith(h('p', { class: 'artifact-error', 'data-i18n': 'chart.failed', text: t('chart.failed') }));
  });
  if (artifact.data) image.src = dataUrl(artifact);
  body.append(image);
  if (!artifact.data) return {};
  return {
    downloadLabel: 'artifact.download.image',
    download: () => {
      const media = SAFE_MEDIA.test(artifact.media_type || '') ? artifact.media_type : 'image/png';
      const bytes = Uint8Array.from(atob(artifact.data), (character) => character.charCodeAt(0));
      const extension = media.split('/')[1].replace('svg+xml', 'svg').replace('jpeg', 'jpg');
      download(`${slug(artifact.title || 'figure')}.${extension}`, new Blob([bytes], { type: media }));
    },
  };
}
