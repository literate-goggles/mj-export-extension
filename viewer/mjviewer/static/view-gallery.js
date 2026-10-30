// Gallery: prompt search, filters, and a server-paged grid with infinite scroll.
// The filter state lives in the hash so a filtered view can be shared or
// bookmarked; Back from a job restores the loaded list and scroll position.

import {h, icon, fill, debounce} from './dom.js';
import {hrefs, replaceHash} from './router.js';
import {getJSON, paths, loadOverview, isAbort, linkedController} from './api.js';
import {arr, str, obj, isNum, fmtInt, kindLabel, dayValue, formatDate} from './format.js';
import {jobCard, gridItem, skeletonCards, emptyState, errorState, loadingState, releaseLazyImages} from './ui.js';

const PAGE_SIZE = 60;
const FILTER_KEYS = ['q', 'kind', 'version', 'moodboard', 'profile', 'links', 'date_from', 'date_to', 'sort'];
const DEFAULTS = {q: '', kind: '', version: '', moodboard: '', profile: '', links: 'all', date_from: '', date_to: '', sort: 'newest'};
const DAY = /^\d{4}-\d{2}-\d{2}$/;

let memory = null;

export function readGalleryState(query) {
  const q = query instanceof URLSearchParams ? query : new URLSearchParams(query || '');
  const state = {...DEFAULTS};
  for (const key of FILTER_KEYS) {
    const value = q.get(key);
    if (value !== null) state[key] = value;
  }
  state.q = state.q.trim();
  if (!['all', 'explicit'].includes(state.links)) state.links = 'all';
  if (!['newest', 'oldest'].includes(state.sort)) state.sort = 'newest';
  if (state.date_from && !DAY.test(state.date_from)) state.date_from = '';
  if (state.date_to && !DAY.test(state.date_to)) state.date_to = '';
  return state;
}

/** Non-default filters in a stable order; this string is also the list's identity. */
export function galleryParams(state) {
  const p = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = state[key];
    if (value && value !== DEFAULTS[key]) p.set(key, value);
  }
  return p;
}

function profileValue(facet) {
  const id = str(facet.id);
  return facet.unresolved && id && !id.startsWith('code:') ? `code:${id}` : id;
}

export function renderGallery(root, route, ctx) {
  const {exportId} = route;
  const state = readGalleryState(route.query);
  ctx.setTitle('Gallery');

  // --- Controls ---
  const heading = h('h1', {class: 'toolbar__title', tabindex: '-1'}, 'Gallery');
  const countEl = h('p', {class: 'toolbar__count', 'aria-live': 'polite', 'aria-atomic': 'true'}, 'Loading…');
  const search = h('input', {
    type: 'search', class: 'search__input', placeholder: 'Search prompts', 'aria-label': 'Search prompts',
    autocomplete: 'off', spellcheck: 'false', 'data-search': '', enterkeyhint: 'search',
  });
  search.value = state.q;
  const searchBox = h('div', {class: 'search'}, icon('search', {size: 16, cls: 'search__icon'}), search,
    h('kbd', {class: 'search__key', title: 'Press / to search'}, '/'));

  const kind = selectField('kind', 'Kind', 'All kinds');
  const version = selectField('version', 'Model version', 'All versions');
  const moodboard = selectField('moodboard', 'Moodboard', 'All moodboards');
  const profile = selectField('profile', 'Profile or code', 'All profiles');
  const sort = selectField('sort', 'Sort order', null, [
    {value: 'newest', label: 'Newest first'}, {value: 'oldest', label: 'Oldest first'}]);
  const dateFrom = h('input', {type: 'date', class: 'input input--date', 'aria-label': 'Created on or after'});
  const dateTo = h('input', {type: 'date', class: 'input input--date', 'aria-label': 'Created on or before'});
  const links = h('input', {type: 'checkbox', class: 'toggle__input'});
  const linksToggle = h('label', {class: 'toggle', title: 'When filtering by moodboard or profile, ignore links inferred only from a code match'},
    links, h('span', {class: 'toggle__track', 'aria-hidden': 'true'}), h('span', {class: 'toggle__label'}, 'Explicit links only'));
  const clearBtn = h('button', {type: 'button', class: 'btn btn--ghost btn--sm toolbar__clear'}, icon('x', {size: 14}), 'Clear all');
  const filterToggle = h('button', {type: 'button', class: 'btn toolbar__toggle', 'aria-expanded': 'false', 'aria-controls': 'gallery-filters'},
    icon('sliders', {size: 15}), 'Filters', h('span', {class: 'toolbar__toggle-count'}));
  const facetNote = h('p', {class: 'toolbar__note', role: 'status'});
  const filters = h('div', {class: 'toolbar__filters', id: 'gallery-filters'},
    kind.el, version.el, moodboard.el, profile.el,
    h('div', {class: 'daterange', role: 'group', 'aria-label': 'Creation date range (UTC calendar dates)',
      title: 'Creation date range. Dates are compared as UTC calendar dates.'},
      h('span', {class: 'daterange__label', 'aria-hidden': 'true'}, icon('calendar', {size: 14})),
      dateFrom, h('span', {class: 'daterange__sep', 'aria-hidden': 'true'}, '–'), dateTo),
    linksToggle, clearBtn, facetNote);
  const activeChips = h('div', {class: 'active-filters', 'aria-label': 'Active filters'});

  const toolbar = h('div', {class: 'toolbar-wrap'},
    h('div', {class: 'toolbar'},
      h('div', {class: 'toolbar__row toolbar__row--head'},
        h('div', {class: 'toolbar__heading'}, heading, countEl),
        sort.el),
      h('div', {class: 'toolbar__row'}, searchBox, filterToggle),
      filters,
      activeChips));

  const grid = h('ul', {class: 'grid', 'aria-label': 'Jobs'});
  const sentinel = h('div', {class: 'sentinel', 'aria-hidden': 'true'});
  const footer = h('div', {class: 'grid-footer'});
  const results = h('div', {class: 'results'});
  root.append(toolbar, h('div', {class: 'page page--wide page--gallery'}, results, grid, sentinel, footer));

  // --- State ---
  let facets = null;
  let overallJobs = null;
  let items = [];
  let total = null;
  let done = false;
  let loading = false;
  let error = null;
  let seq = 0;
  let pageController = null;

  const key = () => `${exportId}|${galleryParams(state).toString()}`;

  function syncControls() {
    if (document.activeElement !== search) search.value = state.q;
    const known = Boolean(facets);
    setOptions(kind, known ? arr(facets.kinds).map((k) => ({value: str(k.value), label: `${kindLabel(k.value)} (${fmtInt(k.count)})`})) : [], state.kind, known);
    setOptions(version, known ? arr(facets.versions).map((v) => ({value: str(v.value), label: `${str(v.label) || str(v.value) || 'Unknown'} (${fmtInt(v.count)})`})) : [], state.version, known);
    setOptions(moodboard, known ? arr(facets.moodboards).map((m) => ({value: str(m.id), label: `${str(m.label) || str(m.id)} (${fmtInt(m.count)})`})) : [], state.moodboard, known);
    setOptions(profile, known ? arr(facets.profiles).map((p) => ({
      value: profileValue(p),
      label: `${str(p.label) || str(p.id)} (${fmtInt(p.count)})`,
      group: p.unresolved ? 'Unmatched codes' : 'Profiles',
    })) : [], state.profile, known);
    sort.select.value = state.sort;
    dateFrom.value = state.date_from;
    dateTo.value = state.date_to;
    if (facets) {
      const min = dayValue(facets.date_min);
      const max = dayValue(facets.date_max);
      for (const input of [dateFrom, dateTo]) {
        if (min) input.min = min;
        if (max) input.max = max;
      }
    }
    dateFrom.classList.toggle('is-active', Boolean(state.date_from));
    dateTo.classList.toggle('is-active', Boolean(state.date_to));
    links.checked = state.links === 'explicit';
    const active = activeFilters();
    clearBtn.disabled = active.length === 0 && state.sort === 'newest';
    const countBadge = filterToggle.querySelector('.toolbar__toggle-count');
    countBadge.textContent = active.length ? String(active.length) : '';
    filterToggle.classList.toggle('has-active', active.length > 0);
    fill(activeChips, active.map((f) => h('button', {
      type: 'button', class: 'filter-chip', 'aria-label': `Remove filter ${f.label}: ${f.value}`,
      onclick: () => update({[f.key]: DEFAULTS[f.key]}),
    }, h('span', {class: 'filter-chip__label'}, f.label), h('span', {class: 'filter-chip__value'}, f.value), icon('x', {size: 13}))));
    activeChips.hidden = active.length === 0;
  }

  function facetLabel(list, value, getValue, getLabel) {
    const found = arr(list).find((f) => getValue(f) === value);
    return found ? getLabel(found) : value;
  }

  function activeFilters() {
    const out = [];
    const f = obj(facets);
    if (state.q) out.push({key: 'q', label: 'Search', value: `“${state.q}”`});
    if (state.kind) out.push({key: 'kind', label: 'Kind', value: kindLabel(state.kind)});
    if (state.version) out.push({key: 'version', label: 'Version', value: facetLabel(f.versions, state.version, (v) => str(v.value), (v) => str(v.label) || str(v.value))});
    if (state.moodboard) out.push({key: 'moodboard', label: 'Moodboard', value: facetLabel(f.moodboards, state.moodboard, (m) => str(m.id), (m) => str(m.label) || str(m.id))});
    if (state.profile) out.push({key: 'profile', label: state.profile.startsWith('code:') ? 'Code' : 'Profile', value: facetLabel(f.profiles, state.profile, profileValue, (p) => str(p.label) || str(p.id))});
    if (state.date_from) out.push({key: 'date_from', label: 'From', value: formatDate(state.date_from, {dateOnly: true})});
    if (state.date_to) out.push({key: 'date_to', label: 'To', value: formatDate(state.date_to, {dateOnly: true})});
    if (state.links === 'explicit') out.push({key: 'links', label: 'Links', value: 'explicit only'});
    return out;
  }

  function update(patch) {
    let changed = false;
    for (const [k, v] of Object.entries(patch)) {
      if (state[k] !== v) {
        state[k] = v;
        changed = true;
      }
    }
    if (!changed) return;
    replaceHash(hrefs.gallery(exportId, galleryParams(state)));
    syncControls();
    if (grid.getBoundingClientRect().top < 0) window.scrollTo(0, 0);
    reset();
  }

  // --- Paging ---
  function renderCount() {
    if (error && total === null) {
      countEl.textContent = 'Could not load jobs';
      return;
    }
    if (total === null) {
      countEl.textContent = 'Loading…';
      return;
    }
    const filtered = activeFilters().length > 0;
    let text = `${fmtInt(total)} ${total === 1 ? 'job' : 'jobs'}`;
    if (filtered && isNum(overallJobs)) text += ` of ${fmtInt(overallJobs)}`;
    countEl.textContent = text;
  }

  function renderFooter() {
    if (loading && items.length) {
      fill(footer, loadingState('Loading more jobs…'));
    } else if (error && items.length) {
      fill(footer, h('div', {class: 'grid-footer__error', role: 'alert'},
        icon('alert-circle', {size: 16}), h('span', null, `Could not load more jobs: ${str(error.message)}`),
        h('button', {type: 'button', class: 'btn btn--sm', onclick: () => {
          error = null;
          loadPage();
        }}, 'Retry')));
    } else if (!done && items.length) {
      fill(footer, h('button', {type: 'button', class: 'btn', onclick: () => loadPage()},
        `Load more (${fmtInt(Math.max(0, (total || 0) - items.length))} left)`));
    } else if (done && items.length) {
      fill(footer, h('p', {class: 'grid-footer__end'}, `End of results · ${fmtInt(items.length)} ${items.length === 1 ? 'job' : 'jobs'}`));
    } else {
      fill(footer);
    }
  }

  function renderEmptyOrError() {
    if (error && !items.length) {
      fill(results, errorState(error, {retry: () => reset(), title: 'Could not load jobs', home: error.status === 404}));
      grid.hidden = true;
      return;
    }
    if (!loading && done && !items.length) {
      const filtered = activeFilters().length > 0;
      fill(results, filtered
        ? emptyState({
          iconName: 'search',
          title: 'No jobs match these filters',
          body: h('p', null, 'Try a shorter search or remove a filter.'),
          actions: h('button', {type: 'button', class: 'btn', onclick: () => clearAll()}, 'Clear all filters'),
        })
        : emptyState({
          iconName: 'image',
          title: 'This export has no jobs',
          body: h('p', null, 'jobs.jsonl is empty or absent. The overview lists what the export contains.'),
          actions: h('a', {class: 'btn', href: hrefs.overview(exportId)}, 'Open overview'),
        }));
      grid.hidden = true;
      return;
    }
    fill(results);
    grid.hidden = false;
  }

  function appendCards(page, startIndex) {
    const from = galleryParams(state).toString();
    const fragment = document.createDocumentFragment();
    page.forEach((job, i) => {
      const j = obj(job);
      if (!j.job_id) return;
      const href = hrefs.job(exportId, j.job_id, {in: from, pos: startIndex + i});
      fragment.append(gridItem(j, jobCard(j, {exportId, href})));
    });
    if (startIndex === 0) {
      releaseLazyImages(grid);
      grid.replaceChildren(fragment);
    } else {
      grid.append(fragment);
    }
  }

  async function loadPage() {
    if (loading || done || error) return;
    loading = true;
    const mySeq = seq;
    renderFooter();
    pageController = linkedController(ctx.signal);
    const params = galleryParams(state);
    params.set('offset', String(items.length));
    params.set('limit', String(PAGE_SIZE));
    try {
      const data = await getJSON(paths.jobs(exportId, params), {signal: pageController.signal});
      if (mySeq !== seq) return;
      const page = arr(data.items);
      total = isNum(data.total) ? data.total : items.length + page.length;
      appendCards(page, items.length);
      items = items.concat(page);
      done = items.length >= total || page.length === 0;
    } catch (err) {
      if (isAbort(err) || mySeq !== seq) return;
      error = err;
      if (!items.length) {
        releaseLazyImages(grid);
        grid.replaceChildren();
      }
    } finally {
      if (mySeq === seq) {
        loading = false;
        renderCount();
        renderFooter();
        renderEmptyOrError();
        recheck();
      }
    }
  }

  function reset() {
    seq += 1;
    if (pageController) pageController.abort();
    items = [];
    total = null;
    done = false;
    loading = false;
    error = null;
    grid.hidden = false;
    fill(results);
    releaseLazyImages(grid);
    grid.replaceChildren(...skeletonCards(12));
    renderCount();
    renderFooter();
    loadPage();
  }

  function clearAll() {
    update({...DEFAULTS});
  }

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadPage();
  }, {rootMargin: '0px 0px 1200px 0px'});
  observer.observe(sentinel);
  function recheck() {
    // Re-observing delivers a fresh callback, so a sentinel that is still in
    // range after a short page triggers the next one.
    if (!done && !loading && !error) {
      observer.unobserve(sentinel);
      observer.observe(sentinel);
    }
  }

  // --- Events ---
  const onSearch = debounce(() => update({q: search.value.trim()}), 250);
  search.addEventListener('input', onSearch);
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      onSearch.cancel();
      update({q: search.value.trim()});
    } else if (event.key === 'Escape' && search.value) {
      event.preventDefault();
      search.value = '';
      onSearch.cancel();
      update({q: ''});
    }
  });
  kind.select.addEventListener('change', () => update({kind: kind.select.value}));
  version.select.addEventListener('change', () => update({version: version.select.value}));
  moodboard.select.addEventListener('change', () => update({moodboard: moodboard.select.value}));
  profile.select.addEventListener('change', () => update({profile: profile.select.value}));
  sort.select.addEventListener('change', () => update({sort: sort.select.value}));
  dateFrom.addEventListener('change', () => update({date_from: DAY.test(dateFrom.value) ? dateFrom.value : ''}));
  dateTo.addEventListener('change', () => update({date_to: DAY.test(dateTo.value) ? dateTo.value : ''}));
  links.addEventListener('change', () => update({links: links.checked ? 'explicit' : 'all'}));
  clearBtn.addEventListener('click', () => {
    clearAll();
    search.focus();
  });
  filterToggle.addEventListener('click', () => {
    const open = filters.classList.toggle('is-open');
    filterToggle.setAttribute('aria-expanded', String(open));
  });

  // --- Facets ---
  loadOverview(exportId, {signal: ctx.signal}).then((data) => {
    if (!ctx.isActive()) return;
    const d = obj(data);
    facets = obj(d.facets);
    const observed = obj(obj(d.counts).observed);
    overallJobs = isNum(observed.jobs) ? observed.jobs : null;
    fill(facetNote);
    syncControls();
    renderCount();
  }).catch((err) => {
    if (isAbort(err)) return;
    fill(facetNote, icon('alert-triangle', {size: 14}), `Filter options unavailable: ${str(err.message)}`);
  });

  syncControls();

  // --- Initial list: restore after Back, or load fresh ---
  if (memory && memory.key === key() && memory.items.length) {
    items = memory.items;
    total = memory.total;
    done = memory.done;
    appendCards(items, 0);
    renderCount();
    renderFooter();
    const y = memory.scrollY;
    requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, y)));
    recheck();
  } else {
    reset();
  }

  return () => {
    onSearch.cancel();
    observer.disconnect();
    if (pageController) pageController.abort();
    memory = items.length && !error ? {key: key(), items, total, done, scrollY: window.scrollY} : null;
  };
}

function selectField(name, label, allLabel, fixed = null) {
  const select = h('select', {class: 'select', name, 'aria-label': label});
  const el = h('label', {class: ['select-field', `select-field--${name}`]}, h('span', {class: 'sr-only'}, label), select);
  const field = {el, select, allLabel, fixed, signature: ''};
  if (fixed) setOptions(field, fixed, fixed[0].value);
  return field;
}

function setOptions(field, options, value, known = true) {
  const list = field.fixed || options;
  const values = new Set(list.map((o) => o.value));
  // A value from the address that the facets do not list (yet): say so once they have loaded.
  const extra = value && !values.has(value) ? [{value, label: known ? `${value} (not in this export)` : value, group: ''}] : [];
  const signature = JSON.stringify([list, extra]);
  if (signature !== field.signature) {
    field.signature = signature;
    const nodes = [];
    if (field.allLabel !== null) nodes.push(h('option', {value: ''}, field.allLabel));
    const groups = new Map();
    for (const o of [...list, ...extra]) {
      const option = h('option', {value: o.value}, o.label);
      if (o.group) {
        if (!groups.has(o.group)) {
          const group = h('optgroup', {label: o.group});
          groups.set(o.group, group);
          nodes.push(group);
        }
        groups.get(o.group).append(option);
      } else {
        nodes.push(option);
      }
    }
    field.select.replaceChildren(...nodes);
  }
  field.select.value = value || (field.allLabel !== null ? '' : list[0] && list[0].value);
  field.select.classList.toggle('is-active', Boolean(value) && field.allLabel !== null);
  field.select.disabled = field.allLabel !== null && list.length === 0 && !value;
}
