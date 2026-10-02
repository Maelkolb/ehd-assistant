/** Image artifact: a figure rendered by Python (inline data) or an attributed
    image from Wikimedia Commons (URL). */

import { h, download, slug } from '../dom.js';
import { t } from '../i18n.js';

const SAFE_MEDIA = /^image\/(png|jpeg|gif|webp|svg\+xml)$/;

function dataUrl(artifact) {
  const media = SAFE_MEDIA.test(artifact.media_type || '') ? artifact.media_type : 'image/png';
  return `data:${media};base64,${artifact.data}`;
}

function attributionLine(attribution) {
  const line = h('div', { class: 'image-credit' });
  const parts = [];
  if (attribution.artist) parts.push(attribution.artist);
  const licence = attribution.public_domain ? t('image.publicDomain') : attribution.license;
  if (licence) {
    parts.push(attribution.license_url && /^https?:\/\//.test(attribution.license_url)
      ? h('a', { href: attribution.license_url, target: '_blank', rel: 'noopener noreferrer', text: licence })
      : licence);
  }
  if (attribution.file_page && /^https?:\/\//.test(attribution.file_page)) {
    parts.push(h('a', { href: attribution.file_page, target: '_blank', rel: 'noopener noreferrer',
                        'data-i18n': 'image.commons', text: t('image.commons') }));
  }
  parts.forEach((part, index) => {
    if (index) line.append(' · ');
    line.append(part);
  });
  return line;
}

export function renderImage(artifact, body, foot) {
  const inline = Boolean(artifact.data);
  const source = inline ? dataUrl(artifact)
    : (/^https:\/\//.test(artifact.url || '') ? artifact.url : '');
  const image = h('img', { class: 'image-figure', alt: artifact.alt || artifact.title || '',
                           loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
  image.addEventListener('error', () => {
    image.replaceWith(h('p', { class: 'artifact-error', 'data-i18n': 'chart.failed', text: t('chart.failed') }));
  });
  if (source) image.src = source;
  body.append(image);
  if (artifact.attribution) foot.append(attributionLine(artifact.attribution));

  if (!inline) return {};
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
