// Shared UI pieces. Every value that comes from an export is rendered as text.

import {h, icon, nextId} from './dom.js';
import {hrefs} from './router.js';
import {
  arr, str, obj, isNum, fmtInt, formatDate, dateTitle, dateAttr, kindLabel, statusInfo, supportInfo,
  linkInfo, imageStatusInfo, plural, shortId,
} from './format.js';

// --- Announcements ------------------------------------------------------------

export function announce(message) {
  const region = document.getElementById('announcer');
  if (!region) return;
  region.textContent = '';
  // A separate task makes screen readers treat repeated messages as new.
  setTimeout(() => {
    region.textContent = message;
  }, 30);
}

// --- Badges and chips -----------------------------------------------------------

export function badge(text, {tone = 'neutral', iconName = '', title = '', mono = false, cls = ''} = {}) {
  return h('span', {class: ['badge', `badge--${tone}`, mono && 'badge--mono', cls], title: title || null},
    iconName ? icon(iconName, {size: 13}) : null,
    h('span', {class: 'badge__text'}, text));
}

export function statusBadge(status) {
  const info = statusInfo(status);
  return badge(info.label, {tone: info.tone, iconName: info.icon, cls: 'badge--status'});
}

export function supportBadge(support, version) {
  const info = supportInfo(support);
  const text = version ? `v${str(version)} · ${info.label}` : info.label;
  return badge(text, {tone: info.tone, title: info.help});
}

export function kindBadge(kind, kindSource) {
  const viewer = kindSource === 'viewer';
  return badge(kindLabel(kind), {
    tone: 'kind',
    title: viewer ? 'Kind derived by the viewer; the export did not record it' : 'Job kind recorded by the exporter',
    cls: viewer ? 'badge--derived' : '',
  });
}

export function versionBadge(version) {
  const v = obj(version);
  const label = str(v.label) || 'Unknown';
  const title = v.source === 'job_type'
    ? 'Model version parsed from job_type (no --v or --niji flag)'
    : v.source === 'flag' ? 'Model version from the command flags' : 'No model version recorded';
  return badge(label, {tone: v.source === 'none' || !v.label ? 'muted' : 'version', title});
}

export function linkChip(link, {short = false} = {}) {
  const info = linkInfo(link);
  return h('span', {class: ['link-chip', `link-chip--${info.style}`], title: info.help || null},
    short ? info.short : info.label);
}

/** A chip that links a job to a moodboard or profile. */
export function relationChip(kind, {exportId, id, name, link}) {
  const info = linkInfo(link);
  const label = str(name) || str(id) || 'Unnamed';
  // Profiles have no page of their own; their chip opens the jobs that used them.
  const href = kind === 'moodboard' ? hrefs.moodboard(exportId, id) : hrefs.gallery(exportId, {profile: id});
  const tag = link === 'exporter' || link === 'explicit' ? null : h('span', {class: 'chip__tag'}, info.short);
  return h('a', {
    class: ['chip', `chip--${kind}`, `chip--${info.style}`],
    href,
    title: kind === 'moodboard'
      ? `Moodboard: ${label} (${info.label})`
      : `Profile: ${label} (${info.label}). Shows the jobs that used it.`,
  },
  icon(kind === 'moodboard' ? 'board' : 'user', {size: 13}),
  h('span', {class: 'chip__text'}, label),
  tag);
}

export function codeChip(code, {type, resolved, exportId}) {
  const label = str(code) || '(empty code)';
  const props = {
    class: ['chip', 'chip--code', resolved ? '' : 'chip--unresolved'],
    title: resolved ? `Code ${label} (${str(type) || 'unknown type'})` : `Code ${label} (${str(type) || 'unknown type'}) matches no exported moodboard or profile`,
  };
  const content = [icon('hash', {size: 13}), h('span', {class: 'chip__text mono'}, label),
    resolved ? null : h('span', {class: 'chip__tag'}, 'unmatched')];
  if (!resolved && exportId) {
    return h('a', {...props, href: hrefs.gallery(exportId, {profile: `code:${str(code)}`})}, content);
  }
  return h('span', props, content);
}

export function timeEl(value, {dateOnly = false, cls = ''} = {}) {
  const attr = dateAttr(value);
  return h('time', {class: cls || null, datetime: attr || null, title: dateTitle(value)}, formatDate(value, {dateOnly}));
}

// --- Copy -------------------------------------------------------------------------

export async function copyText(text) {
  const value = str(text);
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall back to the selection-based copy below.
  }
  const area = h('textarea', {class: 'copy-buffer', readonly: true, 'aria-hidden': 'true', tabindex: '-1'});
  area.value = value;
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

export function copyButton(text, {label = 'Copy', what = '', small = true, iconOnly = false} = {}) {
  const name = what ? `Copy ${what}` : label;
  const textNode = h('span', {class: 'copy-btn__text'}, label);
  const btn = h('button', {
    type: 'button',
    class: ['btn', 'btn--ghost', 'copy-btn', small && 'btn--sm', iconOnly && 'btn--icon'],
    'aria-label': name,
    title: name,
  }, icon('copy', {size: 14}), iconOnly ? null : textNode);
  let timer = 0;
  btn.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const ok = await copyText(typeof text === 'function' ? text() : text);
    btn.classList.toggle('is-copied', ok);
    textNode.textContent = ok ? 'Copied' : 'Copy failed';
    announce(ok ? 'Copied to clipboard' : 'Copy failed');
    clearTimeout(timer);
    timer = setTimeout(() => {
      btn.classList.remove('is-copied');
      textNode.textContent = label;
    }, 1600);
  });
  return btn;
}

/** An identifier in monospace with an optional copy button. */
export function idValue(id, {copy = true, what = 'id', cls = ''} = {}) {
  const value = str(id);
  if (!value) return h('span', {class: 'muted'}, '—');
  return h('span', {class: ['id-value', cls]},
    h('span', {class: 'mono id-value__text'}, value),
    copy ? copyButton(value, {what, iconOnly: true}) : null);
}

// --- Images -----------------------------------------------------------------------

/** Only same-origin /files/ URLs from the API are ever used as image sources. */
export function safeFileUrl(url) {
  return typeof url === 'string' && /^\/files\/[^/]/.test(url) && !url.includes('..') ? url : null;
}

// Lazy images. A thumbnail gets its src only when it enters the viewport, so a
// page never requests images below the fold before the user scrolls. Once the
// user scrolls, images within PREFETCH of the viewport load ahead of time so
// they are ready when they come into view. (Native loading="lazy" alone loads
// everything within about 1,250-2,500 px, which is most of a small export.)
const PREFETCH = '600px 0px';
const lazyPending = new Set();
let lazyObserver = null;
let lazyScrolled = false;
let lazyListening = false;

function revealImage(img) {
  lazyPending.delete(img);
  const src = img.dataset.src;
  if (!src) return;
  delete img.dataset.src;
  img.src = src;
}

function makeLazyObserver() {
  lazyObserver = new IntersectionObserver((entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      revealImage(entry.target);
    }
  }, {rootMargin: lazyScrolled ? PREFETCH : '0px'});
  // Images are observed before they are inserted, so a detached one is usually
  // about to be shown; keep it. Discarded views are released or reset instead.
  for (const img of lazyPending) lazyObserver.observe(img);
}

function onLazyScroll() {
  // A scroll back to the top (a route change) is not the user scrolling down.
  if (lazyScrolled || window.scrollY <= 0) return;
  lazyScrolled = true;
  if (lazyObserver) {
    lazyObserver.disconnect();
    makeLazyObserver();
  }
}

function lazyObserve(img) {
  if (!('IntersectionObserver' in window)) {
    revealImage(img);
    return;
  }
  if (!lazyListening) {
    window.addEventListener('scroll', onLazyScroll, {passive: true});
    lazyListening = true;
  }
  if (!lazyObserver) makeLazyObserver();
  lazyPending.add(img);
  lazyObserver.observe(img);
}

/** Stop watching the lazy images inside a container that is being discarded. */
export function releaseLazyImages(container) {
  if (!container) return;
  for (const img of container.querySelectorAll('img[data-src]')) {
    lazyPending.delete(img);
    if (lazyObserver) lazyObserver.unobserve(img);
  }
}

/** Drop every pending lazy image (called when the whole view is replaced). */
export function resetLazyImages() {
  if (lazyObserver) lazyObserver.disconnect();
  lazyObserver = null;
  lazyPending.clear();
  lazyScrolled = false;
}

export function placeholder(status, {compact = false, file = '', reason = ''} = {}) {
  const info = imageStatusInfo(status);
  const title = [info.label, reason ? str(reason) : '', file ? str(file) : ''].filter(Boolean).join(' · ');
  return h('div', {class: ['ph', `ph--${str(status) || 'unknown'}`, compact && 'ph--compact'], role: 'img', 'aria-label': title, title},
    icon(info.icon, {size: compact ? 18 : 22}),
    h('span', {class: 'ph__label'}, info.short));
}

function thumbWidth(ref) {
  const w = ref.width;
  const hgt = ref.height;
  if (!isNum(w) || !isNum(hgt) || w <= 0 || hgt <= 0) return 0;
  return Math.max(1, Math.round((512 * w) / Math.max(w, hgt)));
}

/**
 * <img> for an ImageRef, or a placeholder when it is not present.
 * mode 'thumb' uses thumb_url; 'responsive' offers thumb and original via srcset.
 */
export function imageEl(ref, {mode = 'thumb', alt = '', eager = false, sizes = '', compact = false} = {}) {
  const r = obj(ref);
  const url = safeFileUrl(r.url);
  const thumb = safeFileUrl(r.thumb_url) || url;
  if (r.status !== 'present' || !url) {
    return placeholder(r.status === 'present' ? 'failed' : r.status, {compact, file: str(r.file), reason: str(r.reason)});
  }
  const img = h('img', {
    class: 'img fade',
    alt,
    loading: eager ? 'eager' : 'lazy',
    decoding: 'async',
    draggable: 'false',
  });
  if (isNum(r.width) && isNum(r.height) && r.width > 0 && r.height > 0) {
    img.width = r.width;
    img.height = r.height;
  }
  img.addEventListener('load', () => img.classList.add('is-loaded'), {once: true});
  let retried = false;
  img.addEventListener('error', () => {
    // One retry covers a request the browser aborted because the network changed.
    if (!retried) {
      retried = true;
      const src = img.getAttribute('src');
      setTimeout(() => {
        if (img.isConnected && src) img.src = src;
      }, 600);
      return;
    }
    img.replaceWith(placeholder('failed', {compact, file: str(r.file)}));
  });
  if (mode === 'full') {
    img.src = url;
  } else if (mode === 'responsive') {
    const tw = thumbWidth(r);
    if (tw && thumb !== url && isNum(r.width) && r.width > tw) {
      img.srcset = `${thumb} ${tw}w, ${url} ${r.width}w`;
      img.sizes = sizes || '100vw';
      img.src = thumb;
    } else {
      img.src = url;
    }
  } else if (eager) {
    img.src = thumb;
  } else {
    img.dataset.src = thumb;
    lazyObserve(img);
  }
  return img;
}

const MIN_RATIO = 0.7;
const MAX_RATIO = 2.2;

function ratioOf(width, height) {
  return isNum(width) && isNum(height) && width > 0 && height > 0 ? width / height : null;
}

/**
 * Aspect ratio of a job's media block in grids, from the job's width/height.
 * Clamped so very tall or wide formats crop slightly instead of becoming slivers;
 * the job page always shows the uncropped images.
 */
export function jobRatio(job) {
  const j = obj(job);
  const images = arr(j.images);
  const first = obj(images[0]);
  const r = ratioOf(j.width, j.height) || ratioOf(first.width, first.height) || 1;
  const raw = images.length === 2 ? r * 2 : r;
  return Math.min(MAX_RATIO, Math.max(MIN_RATIO, raw));
}

/**
 * Media block for a job: one image, a pair, or a 2x2 mosaic. The box size is
 * reserved from width/height, so the grid does not move while images load.
 */
export function jobMedia(job) {
  const images = arr(job.images);
  const media = h('div', {class: 'media'});
  media.style.setProperty('--ar', String(jobRatio(job)));
  if (images.length <= 1) {
    media.classList.add('media--single');
    media.append(images.length
      ? cell(images[0], false)
      : placeholder(Array.isArray(job.images) ? (job.video ? 'unsupported' : 'not_exported') : 'unknown'));
    return media;
  }
  const shown = images.slice(0, 4);
  const pair = shown.length === 2;
  media.classList.add(pair ? 'media--pair' : 'media--mosaic');
  for (let i = 0; i < (pair ? 2 : 4); i += 1) {
    const c = shown[i] ? cell(shown[i], true) : h('div', {class: 'media__cell media__cell--empty'});
    if (i === 3 && images.length > 4) c.append(h('span', {class: 'media__more'}, `+${images.length - 4}`));
    media.append(c);
  }
  return media;
}

function cell(ref, compact) {
  return h('div', {class: 'media__cell'}, imageEl(ref, {mode: 'thumb', compact}));
}

/** Grid list item whose flex basis follows the media ratio (justified rows). */
export function gridItem(job, card) {
  const li = h('li', {class: 'grid__item'}, card);
  li.style.setProperty('--ar', String(jobRatio(job)));
  return li;
}

// --- Job cards --------------------------------------------------------------------

export function jobCard(job, {exportId, href = '', top = null, footer = null, cls = ''} = {}) {
  const j = obj(job);
  const prompt = str(j.prompt).trim();
  const link = href || hrefs.job(exportId, j.job_id);
  const main = h('a', {class: 'card__main', href: link},
    jobMedia(j),
    h('p', {class: ['card__prompt', prompt ? '' : 'is-empty']}, prompt || 'No prompt text'));

  const badges = h('div', {class: 'card__row'},
    kindBadge(j.kind, j.kind_source),
    versionBadge(j.version),
    j.liked ? h('span', {class: 'card__liked', title: 'Has likes'}, icon('heart', {size: 14}), h('span', {class: 'sr-only'}, 'Liked')) : null,
    timeEl(j.created_at, {dateOnly: true, cls: 'card__date'}));

  const relations = [];
  for (const m of arr(j.moodboards)) relations.push(relationChip('moodboard', {exportId, id: m.id, name: m.name, link: m.link}));
  for (const p of arr(j.profiles)) relations.push(relationChip('profile', {exportId, id: p.id, name: p.name, link: p.link}));
  for (const c of arr(j.codes)) if (c && c.resolved === false) relations.push(codeChip(c.code, {type: c.type, resolved: false, exportId}));

  const lineage = [];
  if (j.parent_status === 'in_export' && j.parent_id) {
    lineage.push(h('a', {class: 'hint', href: hrefs.job(exportId, j.parent_id), title: 'Open the parent job'},
      icon('corner-up', {size: 13}), 'Parent'));
  } else if (j.parent_status === 'missing') {
    lineage.push(h('span', {class: 'hint hint--warn', title: 'The parent job is not part of this export'},
      icon('unlink', {size: 13}), 'Parent not exported'));
  } else if (j.parent_status === 'left_out') {
    lineage.push(h('span', {class: 'hint', title: 'The designer chose not to export the parent job'},
      icon('unlink', {size: 13}), 'Parent left out'));
  }
  if (isNum(j.child_count) && j.child_count > 0) {
    lineage.push(h('span', {class: 'hint', title: 'Jobs in this export made from this one'},
      icon('corner-down', {size: 13}), plural(j.child_count, 'child', 'children')));
  }

  return h('article', {class: ['card', cls]},
    top,
    main,
    h('div', {class: 'card__body'},
      badges,
      relations.length ? h('div', {class: 'card__row card__relations'}, relations) : null,
      lineage.length ? h('div', {class: 'card__row card__lineage'}, lineage) : null,
      footer));
}

/** Compact job row used in lineage trails and lists. */
export function miniJob(job, {exportId, current = false, note = '', href = ''} = {}) {
  const j = obj(job);
  const first = arr(j.images).find((r) => r && r.status === 'present') || arr(j.images)[0];
  const thumb = h('span', {class: 'mini__thumb'});
  if (first) thumb.append(imageEl(first, {mode: 'thumb', compact: true}));
  else thumb.append(placeholder('not_exported', {compact: true}));
  const prompt = str(j.prompt).trim();
  const body = h('span', {class: 'mini__text'},
    h('span', {class: ['mini__title', prompt ? '' : 'is-empty'], title: prompt || null}, prompt || 'No prompt text'),
    h('span', {class: 'mini__meta'}, kindLabel(j.kind), ' · ', formatDate(j.created_at, {dateOnly: true})),
    note ? h('span', {class: 'mini__note'}, note) : null);
  if (current) {
    return h('span', {class: 'mini is-current', 'aria-current': 'page'}, thumb, body);
  }
  return h('a', {class: 'mini', href: href || hrefs.job(exportId, j.job_id)}, thumb, body);
}

// --- Layout -----------------------------------------------------------------------

export function pageHead({eyebrow = null, title = '', sub = null, actions = null, titleCls = ''} = {}) {
  const heading = h('h1', {class: ['page-title', titleCls], tabindex: '-1'}, title);
  const el = h('header', {class: 'page-head'},
    h('div', {class: 'page-head__text'},
      eyebrow ? h('p', {class: 'eyebrow'}, eyebrow) : null,
      heading,
      sub ? h('div', {class: 'page-sub'}, sub) : null),
    actions ? h('div', {class: 'page-head__actions'}, actions) : null);
  return {el, heading};
}

export function section(title, {count = null, actions = null, intro = null, cls = '', iconName = '', level = 2} = {}) {
  const id = nextId('sec');
  const body = h('div', {class: 'section__body'});
  const el = h('section', {class: ['section', cls], 'aria-labelledby': id},
    h('div', {class: 'section__head'},
      h(`h${level}`, {class: 'section__title', id},
        iconName ? icon(iconName, {size: 18}) : null,
        h('span', null, title),
        count !== null && count !== undefined ? h('span', {class: 'count'}, isNum(count) ? fmtInt(count) : count) : null),
      actions ? h('div', {class: 'section__actions'}, actions) : null),
    intro ? h('p', {class: 'section__intro'}, intro) : null,
    body);
  return {el, body};
}

export function callout(tone, ...content) {
  const icons = {info: 'info', warn: 'alert-triangle', err: 'alert-circle', ok: 'check-circle', neutral: 'info'};
  return h('div', {class: ['callout', `callout--${tone}`], role: tone === 'err' ? 'alert' : null},
    icon(icons[tone] || 'info', {size: 18}),
    h('div', {class: 'callout__body'}, content));
}

export function emptyState({iconName = 'info', title = '', body = null, actions = null, cls = ''} = {}) {
  return h('div', {class: ['state', cls]},
    h('span', {class: 'state__icon'}, icon(iconName, {size: 20})),
    h('div', {class: 'state__text'},
      h('h2', {class: 'state__title'}, title),
      body ? h('div', {class: 'state__body'}, body) : null),
    actions ? h('div', {class: 'state__actions'}, actions) : null);
}

export function errorState(err, {retry = null, title = 'Could not load this view', home = false} = {}) {
  const status = err && isNum(err.status) && err.status ? `HTTP ${err.status}` : '';
  const code = err && err.code ? str(err.code) : '';
  const message = err && err.message ? str(err.message) : 'Something went wrong.';
  const notFound = err && err.status === 404;
  return h('div', {class: 'state state--error', role: 'alert'},
    h('span', {class: 'state__icon'}, icon(notFound ? 'help' : 'alert-circle', {size: 20})),
    h('div', {class: 'state__text'},
      h('h2', {class: 'state__title'}, notFound ? 'Not found' : title),
      h('p', {class: 'state__body'}, message),
      status || code ? h('p', {class: 'state__code mono'}, [status, code].filter(Boolean).join(' · ')) : null),
    h('div', {class: 'state__actions'},
      retry ? h('button', {type: 'button', class: 'btn', onclick: () => {
        // A failed hosted preparation starts again only on request: this click is one.
        if (err && typeof err.retryPrepare === 'function') err.retryPrepare();
        retry();
      }}, icon('refresh', {size: 14}), 'Retry') : null,
      home || notFound ? h('a', {class: 'btn btn--ghost', href: hrefs.home()}, 'All exports') : null));
}

export function loadingState(text = 'Loading…') {
  return h('div', {class: 'loading', role: 'status'}, h('span', {class: 'spinner', 'aria-hidden': 'true'}), h('span', null, text));
}

export function skeletonCards(n = 8) {
  const items = [];
  for (let i = 0; i < n; i += 1) {
    items.push(h('li', {class: 'grid__item grid__item--skeleton', 'aria-hidden': 'true'},
      h('div', {class: 'card card--skeleton'},
        h('div', {class: 'skeleton skeleton--media'}),
        h('div', {class: 'card__body'},
          h('div', {class: 'skeleton skeleton--line'}),
          h('div', {class: 'skeleton skeleton--line skeleton--short'})))));
  }
  return items;
}

/** <dl> with label/value rows; rows whose value is undefined are skipped. */
export function kvList(rows, {cls = ''} = {}) {
  const dl = h('dl', {class: ['kv', cls]});
  for (const row of rows) {
    if (!row) continue;
    const [label, value, title] = row;
    if (value === undefined) continue;
    dl.append(h('div', {class: 'kv__row'},
      h('dt', {title: title || null}, label),
      h('dd', null, value === null || value === '' ? h('span', {class: 'muted'}, '—') : value)));
  }
  return dl;
}

/** Scrollable table. columns: [{label, cls}], rows: arrays of cell content. */
export function dataTable(columns, rows, {caption = '', cls = ''} = {}) {
  const table = h('table', {class: ['table', cls]},
    caption ? h('caption', {class: 'sr-only'}, caption) : null,
    h('thead', null, h('tr', null, columns.map((c) => h('th', {scope: 'col', class: c.cls || null}, c.label)))),
    h('tbody', null, rows.map((row) => {
      const cells = Array.isArray(row) ? row : row.cells;
      return h('tr', {class: Array.isArray(row) ? null : row.cls || null},
        cells.map((value, i) => h('td', {class: columns[i] && columns[i].cls ? columns[i].cls : null}, value)));
    })));
  return h('div', {class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': caption || 'Table'}, table);
}

export function rawBlock(value, {summary = 'Raw record', open = false} = {}) {
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  if (text === undefined) text = 'null';
  const details = h('details', {class: 'raw'},
    h('summary', null, icon('chevron-right', {size: 14, cls: 'raw__chev'}), h('span', null, summary)),
    h('div', {class: 'code code--raw'},
      h('pre', {class: 'code__pre', tabindex: '0'}, text),
      h('div', {class: 'code__actions'}, copyButton(text, {what: summary.toLowerCase()}))));
  if (open) details.open = true;
  return details;
}

/** Generic, text-only rendering for values whose shape the contract leaves open. */
export function renderValue(value, {exportId = null} = {}) {
  if (value === null || value === undefined || value === '') return h('span', {class: 'muted'}, '—');
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return h('span', {class: 'value'}, String(value));
  }
  if (Array.isArray(value)) {
    if (!value.length) return h('span', {class: 'muted'}, 'none');
    if (value.every((v) => v === null || ['string', 'number', 'boolean'].includes(typeof v))) {
      return h('span', {class: 'value'}, value.map((v) => String(v)).join(', '));
    }
    return h('ul', {class: 'value-list'}, value.slice(0, 50).map((v) => h('li', null, renderValue(v, {exportId}))),
      value.length > 50 ? h('li', {class: 'muted'}, `and ${fmtInt(value.length - 50)} more`) : null);
  }
  const entries = Object.entries(value);
  if (!entries.length) return h('span', {class: 'muted'}, '{}');
  return h('span', {class: 'value-obj'}, entries.map(([k, v]) => h('span', {class: 'value-obj__pair'},
    h('span', {class: 'value-obj__key'}, k), ' ',
    jobLinkOr(k, v, exportId))));
}

function jobLinkOr(key, v, exportId) {
  if (exportId && typeof v === 'string' && v && (key === 'job_id' || key === 'parent_id_in_export')) {
    return h('a', {class: 'mono', href: hrefs.job(exportId, v)}, v);
  }
  if (exportId && typeof v === 'string' && v && (key === 'board_id' || key === 'moodboard_id')) {
    return h('a', {class: 'mono', href: hrefs.moodboard(exportId, v)}, v);
  }
  if (v !== null && typeof v === 'object') {
    let text;
    try {
      text = JSON.stringify(v);
    } catch {
      text = String(v);
    }
    return h('span', {class: 'mono'}, text);
  }
  return h('span', {class: typeof v === 'string' && /^[\w./:-]{6,}$/.test(v) ? 'mono' : null}, str(v));
}

/** Link to a job by id, as monospace text; short shows the first id segment. */
export function jobLink(exportId, jobId, {label = '', short = false} = {}) {
  const id = str(jobId);
  if (!id) return h('span', {class: 'muted'}, '—');
  const text = label || (short ? shortId(id) : id);
  return h('a', {
    class: ['mono', 'job-link', short && 'nowrap'], href: hrefs.job(exportId, id), title: id,
    'aria-label': text === id ? null : `Job ${id}`,
  }, text);
}
