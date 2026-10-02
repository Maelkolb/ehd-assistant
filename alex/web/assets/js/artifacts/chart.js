/** Chart artifact: a Plotly figure produced by the agent's Python code,
    restyled here so it follows the interface theme. */

import { h, slug } from '../dom.js';
import { t, getLanguage } from '../i18n.js';
import { vizColors } from '../theme.js';
import { loadPlotly } from './lazy.js';

function template(colors, forExport = false) {
  const axis = {
    gridcolor: colors.grid, linecolor: colors.axis, zerolinecolor: colors.axis, zerolinewidth: 1,
    tickcolor: colors.axis, ticks: 'outside', ticklen: 4, automargin: true,
    tickfont: { color: colors.muted, size: 12 },
    title: { font: { color: colors.ink2, size: 13 }, standoff: 10 },
  };
  return {
    layout: {
      paper_bgcolor: forExport ? colors.surface : 'rgba(0,0,0,0)',
      plot_bgcolor: forExport ? colors.surface : 'rgba(0,0,0,0)',
      font: { family: colors.fontSans, color: colors.ink2, size: 13 },
      colorway: colors.series,
      xaxis: axis,
      yaxis: axis,
      legend: { orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0, font: { color: colors.ink2 },
                bgcolor: 'rgba(0,0,0,0)' },
      hoverlabel: { bgcolor: colors.surface, bordercolor: colors.axis,
                    font: { family: colors.fontSans, color: colors.ink, size: 13 } },
      hovermode: 'closest',
      bargap: 0.35,
      margin: { l: 56, r: 20, t: 28, b: 52 },
      title: { font: { family: colors.fontSans, color: colors.ink, size: 15 }, x: 0, xanchor: 'left' },
      colorscale: { sequential: [[0, '#cde2fb'], [0.5, '#3987e5'], [1, '#0d366b']] },
    },
    data: {
      bar: [{ marker: { line: { width: 0 } } }],
      scatter: [{ line: { width: 2 }, marker: { size: 8 } }],
    },
  };
}

/** The agent's layout without the styling this page owns. */
function cleanedLayout(figure, cardTitle) {
  const layout = structuredClone(figure.layout || {});
  delete layout.template;
  delete layout.paper_bgcolor;
  delete layout.plot_bgcolor;
  delete layout.font;
  delete layout.width;
  delete layout.height;
  delete layout.colorway;
  const title = typeof layout.title === 'string' ? layout.title : layout.title?.text;
  if (title && cardTitle && title.trim() === cardTitle.trim()) delete layout.title;
  layout.autosize = true;
  return layout;
}

export function renderChart(artifact, body) {
  const plot = h('div', { class: 'chart-plot' },
    h('p', { class: 'artifact-loading', 'data-i18n': 'chart.loading', text: t('chart.loading') }));
  body.append(plot);
  const figure = artifact.figure || {};
  const layout = cleanedLayout(figure, artifact.title);
  let plotly = null;

  const draw = () => plotly.react(plot, figure.data || [], { ...layout, template: template(vizColors()) }, {
    displaylogo: false, responsive: true, displayModeBar: 'hover',
    modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d', 'toImage'],
    locale: getLanguage(),
  });

  const ready = loadPlotly().then((library) => {
    plotly = library;
    plot.replaceChildren();
    return draw();
  }).catch((error) => {
    console.error(error);
    plot.replaceChildren(h('p', { class: 'artifact-error', 'data-i18n': 'chart.failed', text: t('chart.failed') }));
  });

  return {
    ready,
    resize: () => plotly?.Plots.resize(plot),
    retheme: () => plotly && draw(),
    destroy: () => plotly?.purge(plot),
    downloadLabel: 'artifact.download.png',
    download: async () => {
      if (!plotly) return;
      const url = await plotly.toImage({
        data: figure.data || [],
        layout: { ...layout, template: template(vizColors(), true), autosize: false },
      }, { format: 'png', width: 1100, height: 620, scale: 2 });
      const link = h('a', { href: url, download: `${slug(artifact.title || 'chart')}.png` });
      document.body.append(link);
      link.click();
      link.remove();
    },
  };
}
