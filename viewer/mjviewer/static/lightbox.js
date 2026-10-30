// Full-size image viewer: a modal <dialog> with arrow-key navigation, Esc to
// close, a Tab focus trap, and focus returned to the element that opened it.

import {h, icon} from './dom.js';
import {safeFileUrl} from './ui.js';
import {isNum, str} from './format.js';

let parts = null;
let state = null;

function build() {
  const title = h('p', {class: 'lightbox__title', id: 'lightbox-title'});
  const meta = h('p', {class: 'lightbox__meta'});
  const original = h('a', {class: 'lightbox__btn', target: '_blank', rel: 'noopener noreferrer', title: 'Open the original file in a new tab'},
    icon('external', {size: 16}), h('span', {class: 'lightbox__btn-text'}, 'Original'));
  const close = h('button', {type: 'button', class: 'lightbox__btn', 'aria-label': 'Close image viewer', title: 'Close (Esc)'},
    icon('x', {size: 18}), h('span', {class: 'lightbox__btn-text'}, 'Close'));
  const prev = h('button', {type: 'button', class: 'lightbox__nav lightbox__nav--prev', 'aria-label': 'Previous image', title: 'Previous (←)'},
    icon('chevron-left', {size: 26}));
  const next = h('button', {type: 'button', class: 'lightbox__nav lightbox__nav--next', 'aria-label': 'Next image', title: 'Next (→)'},
    icon('chevron-right', {size: 26}));
  const img = h('img', {class: 'lightbox__img', alt: '', decoding: 'async'});
  const status = h('p', {class: 'lightbox__status', role: 'status'});
  const stage = h('div', {class: 'lightbox__stage'}, img, status, prev, next);
  const dialog = h('dialog', {class: 'lightbox', 'aria-labelledby': 'lightbox-title'},
    h('div', {class: 'lightbox__bar'},
      h('div', {class: 'lightbox__heading'}, title, meta),
      h('div', {class: 'lightbox__actions'}, original, close)),
    stage);
  document.body.append(dialog);

  close.addEventListener('click', () => closeLightbox());
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeLightbox();
  });
  dialog.addEventListener('keydown', onKeyDown);
  stage.addEventListener('click', (event) => {
    if (event.target === stage) closeLightbox();
  });
  let startX = null;
  stage.addEventListener('pointerdown', (event) => {
    startX = event.pointerType === 'mouse' ? null : event.clientX;
  });
  stage.addEventListener('pointerup', (event) => {
    if (startX === null) return;
    const dx = event.clientX - startX;
    startX = null;
    if (Math.abs(dx) > 48) step(dx < 0 ? 1 : -1);
  });
  parts = {dialog, title, meta, original, close, prev, next, img, status};
}

function focusables() {
  return [...parts.dialog.querySelectorAll('button, a[href]')]
    .filter((el) => !el.hidden && !el.disabled && el.offsetParent !== null);
}

function onKeyDown(event) {
  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    step(-1);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    step(1);
  } else if (event.key === 'Home') {
    event.preventDefault();
    show(0);
  } else if (event.key === 'End') {
    event.preventDefault();
    show(state.items.length - 1);
  } else if (event.key === 'Tab') {
    const list = focusables();
    if (!list.length) return;
    const first = list[0];
    const last = list[list.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !parts.dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !parts.dialog.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }
}

function step(delta) {
  if (!state || state.items.length < 2) return;
  const n = state.items.length;
  show((state.index + delta + n) % n);
}

function show(index) {
  const item = state.items[index];
  if (!item) return;
  state.index = index;
  const n = state.items.length;
  const {img, title, meta, original, status, prev, next} = parts;
  const label = item.label || `Image ${index + 1}`;
  // "Image 2 of 4" when the label's number is the position; otherwise keep both,
  // e.g. "Image 4 · 3 of 3" when an earlier image is missing.
  const numbered = /^(.*\D)(\d+)$/.exec(label);
  if (n < 2) title.textContent = label;
  else if (numbered && Number(numbered[2]) === index + 1) title.textContent = `${label} of ${n}`;
  else title.textContent = `${label} · ${index + 1} of ${n}`;
  const bits = [];
  if (isNum(item.width) && isNum(item.height)) bits.push(`${item.width} × ${item.height}`);
  if (item.file) bits.push(str(item.file));
  meta.textContent = bits.join(' · ');
  original.href = item.url;
  prev.hidden = n < 2;
  next.hidden = n < 2;
  status.textContent = '';
  img.alt = state.caption ? `${label}: ${state.caption}` : label;
  if (isNum(item.width) && isNum(item.height)) {
    img.width = item.width;
    img.height = item.height;
  } else {
    img.removeAttribute('width');
    img.removeAttribute('height');
  }
  const token = {};
  state.token = token;
  img.classList.add('is-loading');
  img.src = item.thumb || item.url;
  if (item.thumb && item.thumb !== item.url) {
    const full = new Image();
    full.decoding = 'async';
    full.addEventListener('load', () => {
      if (state && state.token === token) {
        img.src = item.url;
        img.classList.remove('is-loading');
      }
    });
    full.addEventListener('error', () => {
      if (state && state.token === token) {
        img.classList.remove('is-loading');
        status.textContent = 'The full-size file could not be loaded; showing the preview.';
      }
    });
    full.src = item.url;
  } else {
    img.addEventListener('load', () => img.classList.remove('is-loading'), {once: true});
    img.addEventListener('error', () => {
      if (state && state.token === token) status.textContent = 'This image could not be loaded.';
    }, {once: true});
  }
}

/**
 * items: [{url, thumb_url?, width?, height?, file?, label?}] — only present images.
 * Returns false when nothing could be shown.
 */
export function openLightbox(items, index = 0, {opener = null, caption = ''} = {}) {
  const list = (Array.isArray(items) ? items : [])
    .map((it) => ({...it, url: safeFileUrl(it.url), thumb: safeFileUrl(it.thumb_url)}))
    .filter((it) => it.url);
  if (!list.length) return false;
  if (!parts) build();
  const start = Math.min(Math.max(0, index), list.length - 1);
  state = {items: list, index: start, opener: opener || document.activeElement, caption: str(caption)};
  show(start);
  if (!parts.dialog.open) {
    document.documentElement.classList.add('is-locked');
    try {
      parts.dialog.showModal();
    } catch {
      parts.dialog.setAttribute('open', '');
    }
  }
  parts.close.focus();
  return true;
}

export function closeLightbox() {
  if (!parts || !parts.dialog.open) return;
  parts.dialog.close();
  document.documentElement.classList.remove('is-locked');
  parts.img.removeAttribute('src');
  const opener = state && state.opener;
  state = null;
  if (opener && typeof opener.focus === 'function' && opener.isConnected) opener.focus();
}

export function lightboxOpen() {
  return Boolean(parts && parts.dialog.open);
}
