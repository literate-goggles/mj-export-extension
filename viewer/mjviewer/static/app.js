// MJ Export Viewer: application shell, hash router, header and theme.

import {h, icon, clear, fill, isTypingTarget} from './dom.js';
import {parseHash, hrefs, replaceHash} from './router.js';
import {loadExports, knownSummary, onSummariesChanged, tokenMissing, hosted, getJSON, paths, onPrepare} from './api.js';
import {fmtInt, str, obj, arr} from './format.js';
import {errorState, emptyState, callout, resetLazyImages} from './ui.js';
import {closeLightbox, lightboxOpen} from './lightbox.js';
import {renderHome} from './view-home.js';
import {renderHostedHome, itemLabel, stateInfo, progressBar} from './view-hosted.js';
import {renderOverview} from './view-overview.js';
import {renderGallery} from './view-gallery.js';
import {renderJob} from './view-job.js';
import {renderMoodboards, renderMoodboard} from './view-moodboards.js';
import {renderProfiles} from './view-profiles.js';
import {renderLikes} from './view-likes.js';

const APP_NAME = 'MJ Export Viewer';

const ROUTES = {
  home: hosted ? renderHostedHome : renderHome,
  overview: renderOverview,
  gallery: renderGallery,
  job: renderJob,
  moodboards: renderMoodboards,
  moodboard: renderMoodboard,
  profiles: renderProfiles,
  likes: renderLikes,
  notfound: renderNotFound,
};

const TABS = [
  {name: 'overview', label: 'Overview', iconName: 'overview', href: hrefs.overview, count: null},
  {name: 'gallery', label: 'Gallery', iconName: 'grid', href: (x) => hrefs.gallery(x), count: 'jobs', match: ['gallery', 'job']},
  {name: 'moodboards', label: 'Moodboards', iconName: 'board', href: hrefs.moodboards, count: 'moodboards', match: ['moodboards', 'moodboard']},
  {name: 'profiles', label: 'Profiles', iconName: 'user', href: hrefs.profiles, count: 'profiles'},
  {name: 'likes', label: 'Likes', iconName: 'heart', href: hrefs.likes, count: 'likes'},
];

const view = document.getElementById('view');
const main = document.getElementById('main');
const crumb = document.getElementById('export-crumb');
const tabs = document.getElementById('export-tabs');

let current = {route: null, cleanup: null, controller: null};
let navigations = 0;

function setTitle(...partsIn) {
  const parts = partsIn.filter(Boolean).map(str);
  document.title = [...parts, APP_NAME].join(' · ');
}

function exportLabel(exportId) {
  const summary = knownSummary(exportId);
  return summary ? str(summary.label) || str(summary.id) : '';
}

// --- Header ---------------------------------------------------------------------

function renderHeader(route) {
  const exportId = route && route.exportId;
  document.body.classList.toggle('has-export', Boolean(exportId));
  if (!exportId) {
    crumb.hidden = true;
    tabs.hidden = true;
    clear(crumb);
    clear(tabs);
    return;
  }
  const summary = knownSummary(exportId);
  const label = summary ? str(summary.label) || exportId : exportId;
  crumb.hidden = false;
  fill(crumb,
    h('span', {class: 'crumb__sep', 'aria-hidden': 'true'}, '/'),
    h('a', {class: 'crumb__label', href: hrefs.overview(exportId), title: label}, label));
  const counts = summary ? obj(summary.counts) : {};
  tabs.hidden = false;
  fill(tabs, TABS.map((tab) => {
    const active = (tab.match || [tab.name]).includes(route.name);
    const n = tab.count ? counts[tab.count] : null;
    return h('a', {class: 'tab', href: tab.href(exportId), 'aria-current': active ? 'page' : null},
      icon(tab.iconName, {size: 15}),
      h('span', null, tab.label),
      typeof n === 'number' ? h('span', {class: 'tab__count'}, fmtInt(n)) : null);
  }));
  // On narrow screens the tab strip scrolls; keep the current tab in view
  // without scrolling the page itself.
  const active = tabs.querySelector('[aria-current="page"]');
  if (active && tabs.scrollWidth > tabs.clientWidth) {
    tabs.scrollLeft = Math.max(0, active.offsetLeft - (tabs.clientWidth - active.offsetWidth) / 2);
  }
}

// --- Theme ------------------------------------------------------------------------

const THEMES = ['system', 'light', 'dark'];
const THEME_LABEL = {system: 'System', light: 'Light', dark: 'Dark'};
const THEME_ICON = {system: 'monitor', light: 'sun', dark: 'moon'};

function storedTheme() {
  try {
    const value = localStorage.getItem('mjv-theme');
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  try {
    if (theme === 'system') localStorage.removeItem('mjv-theme');
    else localStorage.setItem('mjv-theme', theme);
  } catch {
    // Storage may be unavailable (private mode, blocked site data); the theme still applies.
  }
  const button = document.getElementById('theme-toggle');
  if (button) {
    const nextTheme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    const text = `Theme: ${THEME_LABEL[theme]}. Switch to ${THEME_LABEL[nextTheme]}.`;
    button.setAttribute('aria-label', text);
    button.title = text;
    button.dataset.theme = theme;
    fill(button, icon(THEME_ICON[theme], {size: 17}));
  }
}

function setupTheme() {
  const button = document.getElementById('theme-toggle');
  let theme = storedTheme();
  applyTheme(theme);
  if (button) {
    button.addEventListener('click', () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      applyTheme(theme);
    });
  }
}

// --- Hosted mode: account and preparation notice --------------------------------

async function setupAccount() {
  const slot = document.getElementById('account');
  if (!hosted || !slot) return;
  const signOut = h('a', {class: 'btn btn--ghost btn--sm account__out', href: '/auth/logout'}, 'Sign out');
  fill(slot, signOut);
  slot.hidden = false;
  try {
    const user = str((await getJSON(paths.health())).user);
    if (user) {
      signOut.title = `Signed in as ${user}`;
      fill(slot, h('span', {class: 'account__user', title: `Signed in as ${user}`},
        icon('user', {size: 14}), h('span', {class: 'account__email'}, user)), signOut);
    }
  } catch {
    // An ended session redirects to sign-in; otherwise the header simply shows no address.
  }
}

/** While a view waits for a published export to open, say so above it. */
function setupPrepareNotice() {
  if (!hosted) return;
  const notices = document.getElementById('notices');
  const box = h('div', {class: 'prepare-notice'});
  onPrepare((item) => {
    const it = obj(item);
    if (it.state === 'ready' || it.state === 'failed' || !current.route || current.route.name === 'home') {
      box.remove();
      return;
    }
    const info = stateInfo(it);
    fill(box, callout('info', h('p', {role: 'status'}, h('strong', null, itemLabel(it)), `: ${info.text}`),
      progressBar(info.fraction, `Opening ${itemLabel(it)}`)));
    if (!box.isConnected) notices.append(box);
  });
}

// --- Routing ----------------------------------------------------------------------

function renderNotFound(root, route, ctx) {
  ctx.setTitle('Not found');
  root.append(h('div', {class: 'page'},
    h('h1', {class: 'sr-only', tabindex: '-1'}, 'Page not found'),
    emptyState({
      iconName: 'help',
      title: 'This address does not match a view',
      body: h('p', null, 'The link may be incomplete. Start from the list of opened exports.'),
      actions: h('a', {class: 'btn btn--primary', href: hrefs.home()}, 'All exports'),
    })));
}

function render() {
  const route = parseHash(location.hash);
  if (current.cleanup) {
    try {
      current.cleanup();
    } catch (err) {
      console.error(err);
    }
  }
  if (current.controller) current.controller.abort();
  closeLightbox();
  resetLazyImages();
  const controller = new AbortController();
  current = {route, cleanup: null, controller};
  navigations += 1;
  const isFirst = navigations === 1;

  renderHeader(route);
  clear(view);
  window.scrollTo(0, 0);
  setTitle();

  const ctx = {
    signal: controller.signal,
    route,
    isFirst,
    setTitle: (...parts) => setTitle(...parts, route.exportId ? exportLabel(route.exportId) : ''),
    isActive: () => current.controller === controller,
  };
  const renderer = ROUTES[route.name] || renderNotFound;
  try {
    const cleanup = renderer(view, route, ctx);
    current.cleanup = typeof cleanup === 'function' ? cleanup : null;
  } catch (err) {
    console.error(err);
    clear(view);
    view.append(h('div', {class: 'page'}, errorState(err, {title: 'This view failed to render', home: true})));
  }
  if (!isFirst) {
    const heading = view.querySelector('h1');
    if (heading) heading.focus({preventScroll: true});
  }
}

function ensureSummary(route) {
  if (!route.exportId || knownSummary(route.exportId)) return;
  loadExports({force: true}).catch(() => {});
}

function setupShortcuts() {
  document.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === '/' && !isTypingTarget(event.target) && !lightboxOpen()) {
      const search = document.querySelector('[data-search]');
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    }
  });
  const skip = document.querySelector('.skip-link');
  if (skip) {
    skip.addEventListener('click', (event) => {
      event.preventDefault();
      main.focus();
    });
  }
}

async function start() {
  setupTheme();
  setupShortcuts();
  setupAccount();
  setupPrepareNotice();
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  if (tokenMissing) {
    document.getElementById('notices').append(callout('warn',
      h('p', null, 'This page was not served by the viewer server, so requests will be refused. Start it with ',
        h('code', null, 'python3 -m mjviewer'), ' and open the address it prints.')));
  }
  onSummariesChanged(() => {
    if (current.route) renderHeader(current.route);
  });
  window.addEventListener('hashchange', () => {
    const route = parseHash(location.hash);
    ensureSummary(route);
    render();
  });

  const initialHash = location.hash.replace(/^#\/?/, '');
  let summariesLoaded = false;
  try {
    await loadExports();
    summariesLoaded = true;
  } catch {
    // Views show their own errors; the header falls back to ids.
  }
  if (!initialHash && summariesLoaded) {
    const state = await loadExports();
    const list = state.exports;
    if (hosted) {
      // One published export that is already open: straight to it (the start page opens it otherwise).
      const items = arr(obj(state.catalog).items);
      const only = items.length === 1 ? arr(obj(items[0]).exports) : [];
      if (obj(items[0]).state === 'ready' && only.length === 1) replaceHash(hrefs.overview(only[0].id));
    } else if (list.length === 1) {
      replaceHash(hrefs.overview(list[0].id));
    }
  }
  render();
}

start();
