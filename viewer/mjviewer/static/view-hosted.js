// Hosted start page: the exports published for this deployment. Opening one
// downloads and checks it on the server (with progress), then shows its overview.

import {h, icon, fill} from './dom.js';
import {hrefs} from './router.js';
import {loadExports, prepareExport, onPrepare, isAbort} from './api.js';
import {arr, str, obj, isNum, fmtInt, fmtBytes} from './format.js';
import {
  pageHead, section, statusBadge, supportBadge, badge, emptyState, errorState, loadingState, callout, timeEl, announce,
} from './ui.js';

/** Display name of a catalog item: its label, else its file name without .zip. */
export function itemLabel(item) {
  const it = obj(item);
  return str(it.label) || str(it.name).replace(/\.zip$/i, '') || str(it.id) || 'Published export';
}

/** {text, fraction} for an item's state; fraction is null when there is no measurable progress. */
export function stateInfo(item) {
  const it = obj(item);
  const p = obj(it.progress);
  const fraction = isNum(p.done) && isNum(p.total) && p.total > 0 ? Math.min(1, p.done / p.total) : null;
  const pct = fraction === null ? '' : ` ${Math.floor(fraction * 100)}%`;
  switch (it.state) {
    case 'absent': return {text: 'Not downloaded yet', fraction: null};
    case 'cached': return {text: 'Downloaded; ready to open', fraction: null};
    case 'starting': return {text: 'Starting…', fraction: null};
    case 'queued': return {text: 'Waiting to start…', fraction: null};
    case 'downloading':
      return {text: `Downloading${pct}${fraction === null ? '' : ` (${fmtBytes(p.done)} of ${fmtBytes(p.total)})`}…`, fraction};
    case 'verifying': return {text: `Checking the download${pct}…`, fraction};
    case 'opening': return {text: 'Reading the export…', fraction: null};
    case 'ready': return {text: 'Ready', fraction: 1};
    case 'failed': return {text: `Could not open: ${str(obj(it.error).message) || 'unknown error'}`, fraction: null};
    default: return {text: str(it.state) || 'Unknown state', fraction: null};
  }
}

/** A progress bar; indeterminate when fraction is null. */
export function progressBar(fraction, label) {
  const bar = h('div', {class: 'progress__bar'});
  const track = h('div', {class: ['progress', fraction === null && 'is-indeterminate'], role: 'progressbar',
    'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100'}, bar);
  if (fraction !== null) {
    const value = Math.round(fraction * 100);
    bar.style.setProperty('width', `${value}%`);
    track.setAttribute('aria-valuenow', String(value));
  }
  return track;
}

const BUSY = new Set(['starting', 'queued', 'downloading', 'verifying', 'opening']);

export function renderHostedHome(root, route, ctx) {
  ctx.setTitle('Exports');
  const head = pageHead({
    eyebrow: 'Shared viewer',
    title: 'Exports',
    sub: h('p', null, 'Midjourney exports published for your team. They are read in place and never modified.'),
  });
  const noteSlot = h('div', {class: 'slot'});
  const listSlot = h('div', {class: 'slot'}, loadingState('Loading exports…'));
  const list = section('Published exports', {cls: 'section--first'});
  list.body.append(noteSlot, listSlot);
  root.append(h('div', {class: 'page'}, head.el, list.el));

  const cards = new Map();
  // With a single published export, the first visit opens it and goes to its overview.
  let autoOpen = ctx.isFirst;

  const stop = onPrepare((item) => {
    const card = cards.get(obj(item).id);
    if (card) card.update(item);
  });

  async function load() {
    fill(listSlot, loadingState('Loading exports…'));
    try {
      const data = await loadExports({force: true, signal: ctx.signal});
      if (!ctx.isActive()) return;
      draw(data);
    } catch (err) {
      if (isAbort(err)) return;
      fill(listSlot, errorState(err, {retry: load, title: 'Could not list the published exports'}));
    }
  }

  function draw(data) {
    const catalog = obj(data.catalog);
    const items = arr(catalog.items).filter((i) => i && typeof i === 'object' && i.id);
    const skipped = arr(catalog.skipped);
    fill(noteSlot,
      catalog.ok === false && catalog.error
        ? callout('warn', h('p', null, `The list of published exports could not be refreshed (${str(catalog.error)}). `,
          'It shows the last list that could be read.'))
        : null,
      skipped.length
        ? callout('info', h('p', null, `${skipped.length === 1 ? 'One published entry is' : `${fmtInt(skipped.length)} published entries are`} `,
          'not valid and hidden. Whoever publishes exports can check the server log.'))
        : null);
    cards.clear();
    if (!items.length) {
      fill(listSlot, emptyState({
        iconName: 'archive',
        title: 'Nothing is published yet',
        body: h('p', null, 'Exports appear here once they are published to this viewer.'),
        actions: h('button', {type: 'button', class: 'btn', onclick: load}, icon('refresh', {size: 14}), 'Refresh'),
      }));
      return;
    }
    fill(listSlot, h('ul', {class: 'xgrid', 'aria-label': 'Published exports'}, items.map((item) => {
      const card = catalogCard(item, open);
      cards.set(item.id, card);
      return h('li', null, card.el);
    })));
    for (const item of items) if (BUSY.has(item.state)) open(item, {quiet: true});
    if (autoOpen && items.length === 1) {
      autoOpen = false;
      if (items[0].state !== 'failed') open(items[0], {go: true});
    }
    autoOpen = false;
  }

  async function open(item, {go = false, quiet = false, retry = false} = {}) {
    const card = cards.get(item.id);
    try {
      const ready = await prepareExport(item, {signal: ctx.signal, retry});
      if (!ctx.isActive()) return;
      if (card) card.update(ready);
      const exports = arr(ready.exports);
      if (!quiet) announce(`${itemLabel(ready)} is ready`);
      if (exports.length !== 1 || quiet) return;
      if (go) location.replace(hrefs.overview(exports[0].id));  // no history entry that would open it again
      else location.hash = hrefs.overview(exports[0].id);
    } catch (err) {
      if (isAbort(err) || !ctx.isActive()) return;
      // err.detail is the server's message alone: the card adds its own 'Could not open:'.
      const message = str(err.detail) || str(err.message);
      if (card) card.update({...item, state: 'failed', progress: null, error: {code: str(err.code), message}});
    }
  }

  load();
  return stop;
}

function countItem(label, value) {
  return h('div', {class: 'xcard__count'}, h('dt', null, label), h('dd', null, value));
}

function catalogCard(initial, open) {
  const el = h('article', {class: 'xcard xcard--catalog'});
  let current = obj(initial);

  function update(next) {
    const it = obj(next);
    current = it;
    const summary = obj(it.summary);
    const exports = arr(it.exports);
    const single = it.state === 'ready' && exports.length === 1 ? obj(exports[0]) : null;
    const counts = single ? obj(single.counts) : {};
    const title = itemLabel(it);
    const exportTime = single ? single.export_time : summary.export_time;
    const jobs = single ? counts.jobs : summary.jobs;
    const images = single && isNum(counts.images_expected)
      ? `${fmtInt(counts.images_present)} / ${fmtInt(counts.images_expected)}`
      : fmtInt(summary.images);
    fill(el,
      h('div', {class: 'xcard__top'},
        statusBadge(single ? single.status : summary.status),
        single
          ? supportBadge(single.version_support, single.extension_version)
          : summary.extension_version ? badge(`v${str(summary.extension_version)}`, {tone: 'neutral'}) : null),
      h('h3', {class: 'xcard__title'}, single ? h('a', {class: 'xcard__link', href: hrefs.overview(single.id)}, title) : title),
      h('p', {class: 'xcard__source', title: str(it.name)},
        icon('archive', {size: 14}),
        h('span', {class: 'xcard__source-type'}, 'Published ZIP'),
        h('span', {class: 'mono truncate'}, str(it.name) || '—'),
        isNum(it.size) ? h('span', {class: 'muted nowrap'}, fmtBytes(it.size)) : null),
      h('p', {class: 'xcard__meta'},
        exportTime ? h('span', null, 'Exported ', timeEl(exportTime)) : h('span', {class: 'muted'}, 'Export time not recorded'),
        it.published_at
          ? h('span', null, 'Published ', timeEl(it.published_at, {dateOnly: true}), it.published_by ? ` by ${str(it.published_by)}` : '')
          : null),
      h('dl', {class: 'xcard__counts'},
        countItem('Jobs', fmtInt(jobs)),
        countItem('Images', images),
        countItem('Boards', fmtInt(single ? counts.moodboards : summary.moodboards)),
        countItem('Profiles', fmtInt(single ? counts.profiles : summary.profiles))),
      stateRow(it, single, exports),
      failedRoots(it));
  }

  /** Export roots of the ZIP that could not be read while the others opened. */
  function failedRoots(it) {
    const failed = arr(it.failed_roots).map(obj);
    if (it.state !== 'ready' || !failed.length) return null;
    return h('ul', {class: 'notes xcard__failed', role: 'note'}, failed.map((f) =>
      h('li', null, h('span', {class: 'mono'}, str(f.root) || '(top of the ZIP)'), `: ${str(f.message) || 'could not be read'}`)));
  }

  function stateRow(it, single, exports) {
    if (single) {
      const warnings = isNum(single.warnings) ? single.warnings : 0;
      const errors = isNum(single.errors) ? single.errors : 0;
      return h('p', {class: 'xcard__issues'},
        errors ? badge(`${fmtInt(errors)} ${errors === 1 ? 'error' : 'errors'}`, {tone: 'err', iconName: 'alert-circle'}) : null,
        warnings ? badge(`${fmtInt(warnings)} ${warnings === 1 ? 'warning' : 'warnings'}`, {tone: 'warn', iconName: 'alert-triangle'}) : null,
        errors || warnings ? null : badge('Ready', {tone: 'ok', iconName: 'check-circle'}));
    }
    if (it.state === 'ready') {
      return h('div', {class: 'xcard__state'},
        h('p', {class: 'xcard__state-text'}, `${fmtInt(exports.length)} exports in this ZIP:`),
        h('ul', {class: 'xcard__roots'}, exports.map((e) => h('li', null,
          h('a', {href: hrefs.overview(str(obj(e).id))}, str(obj(e).label) || str(obj(e).id))))));
    }
    const info = stateInfo(it);
    if (BUSY.has(it.state)) {
      return h('div', {class: 'xcard__state', role: 'status'},
        h('p', {class: 'xcard__state-text'}, info.text),
        h('div', {class: 'xcard__progress'}, progressBar(info.fraction, `Opening ${itemLabel(it)}`)));
    }
    const failed = it.state === 'failed';
    const button = h('button', {type: 'button', class: ['btn', 'btn--sm', failed ? '' : 'btn--primary']},
      icon(failed ? 'refresh' : 'archive', {size: 14}), failed ? 'Try again' : 'Open');
    button.addEventListener('click', () => {
      button.disabled = true;
      update({...current, state: 'starting', progress: null, error: null});
      open(current, {retry: true});  // a click may start a failed preparation again
    });
    return h('div', {class: 'xcard__state'},
      button,
      h('p', {class: ['xcard__state-text', failed && 'is-err'], role: failed ? 'alert' : null}, info.text));
  }

  update(initial);
  return {el, update};
}
