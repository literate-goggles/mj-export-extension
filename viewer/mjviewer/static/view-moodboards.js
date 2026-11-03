// Moodboards: the board list, and one board with its REFERENCE images (the
// board's own inputs) kept visibly apart from GENERATIONS (jobs made using it).
// In hosted mode the export's managers can hide a board, with its generations,
// from everyone else, and see a hidden board's page with a banner.

import {h, icon, fill} from './dom.js';
import {hrefs} from './router.js';
import {getJSON, paths, isAbort, ApiError} from './api.js';
import {arr, str, obj, isNum, fmtInt, linkInfo, plural, recordedDate, isNaiveDate} from './format.js';
import {
  pageHead, section, linkChip, badge, timeEl, copyButton, idValue, imageEl, placeholder, jobCard, gridItem, rawBlock,
  callout, emptyState, errorState, loadingState, safeFileUrl,
} from './ui.js';
import {openLightbox} from './lightbox.js';
import {closeConfirm} from './confirm.js';
import {managerView, hideButton, hiddenBanner, confirmHide, confirmShow, runChange} from './hide.js';

const LINK_ORDER = ['exporter', 'derived', 'inferred_code'];
// generated_left_out is one number for both causes: generations left out, and generations made after choosing.
const NOT_EXPORTED_HELP = 'Generations made with this moodboard that are not in this export because of the designer’s choice: left out, or made after choosing';

function notExportedText(n) {
  return `${plural(n, 'generation')} made with this moodboard ${n === 1 ? 'is' : 'are'} not in this export because of the designer’s choice: left out, or made after choosing.`;
}

function notesBlock(notes) {
  const list = arr(notes).filter(Boolean);
  if (!list.length) return null;
  return callout('info', list.length === 1
    ? h('p', null, str(list[0]))
    : h('ul', {class: 'notes'}, list.map((n) => h('li', null, str(n)))));
}

function codeTags(codes, code) {
  const all = [...new Set([...(code ? [str(code)] : []), ...arr(codes).map(str)].filter(Boolean))];
  if (!all.length) return h('span', {class: 'muted small'}, 'No code recorded');
  return h('span', {class: 'tag-row'}, all.map((c) => h('span', {class: 'tag mono', title: 'Moodboard code'}, c)));
}

// --- List -------------------------------------------------------------------------

export function renderMoodboards(root, route, ctx) {
  const {exportId} = route;
  ctx.setTitle('Moodboards');
  const head = pageHead({
    eyebrow: 'Moodboards',
    title: 'Moodboards',
    sub: h('p', null, 'Each board’s own reference images, and the jobs in this export that were made using it.'),
  });
  const body = h('div', null, loadingState('Loading moodboards…'));
  root.append(h('div', {class: 'page'}, head.el, body));

  async function load() {
    fill(body, loadingState('Loading moodboards…'));
    try {
      const data = obj(await getJSON(paths.moodboards(exportId), {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      const items = arr(data.items);
      head.heading.textContent = `Moodboards`;
      head.heading.append(' ', h('span', {class: 'count count--title'}, fmtInt(items.length)));
      if (!items.length) {
        fill(body, notesBlock(data.notes), emptyState({
          iconName: 'board',
          title: 'No moodboards in this export',
          body: h('p', null, 'moodboards.jsonl is empty or absent. A small-test export includes at most one board, and a board created after the export is not included.'),
        }));
        return;
      }
      fill(body, notesBlock(data.notes), h('ul', {class: 'bgrid', 'aria-label': 'Moodboards'}, items.map((b) => h('li', null, boardCard(exportId, b)))));
    } catch (err) {
      if (isAbort(err)) return;
      fill(body, errorState(err, {retry: load, title: 'Could not load moodboards'}));
    }
  }
  load();
}

function boardCard(exportId, board) {
  const b = obj(board);
  const gens = obj(b.generations);
  const cover = arr(b.cover).slice(0, 4);
  const coverEl = h('div', {class: ['bcard__cover', `bcard__cover--${Math.max(1, cover.length)}`]});
  if (!cover.length) coverEl.append(placeholder('not_exported', {compact: true}));
  for (const ref of cover) coverEl.append(h('div', {class: 'bcard__cell'}, imageEl(ref, {mode: 'thumb', compact: true})));
  const refCount = isNum(b.reference_count) ? b.reference_count : null;
  const present = isNum(b.references_present) ? b.references_present : null;
  const totalGens = LINK_ORDER.reduce((n, k) => n + (isNum(gens[k]) ? gens[k] : 0), 0);
  return h('article', {class: 'bcard'},
    h('a', {class: 'bcard__main', href: hrefs.moodboard(exportId, b.id)},
      coverEl,
      h('h2', {class: 'bcard__title'}, str(b.name) || 'Untitled moodboard')),
    h('div', {class: 'bcard__body'},
      codeTags(b.codes, b.code),
      h('dl', {class: 'bcard__stats'},
        h('div', {class: 'bcard__stat bcard__stat--ref'},
          h('dt', null, 'References'),
          h('dd', null, fmtInt(refCount)),
          present !== null && refCount !== null && present < refCount
            ? h('dd', {class: 'bcard__stat-note is-warn'}, `${fmtInt(refCount - present)} missing`) : null),
        h('div', {class: 'bcard__stat bcard__stat--gen'},
          h('dt', null, 'Generations'),
          h('dd', null, fmtInt(totalGens)),
          isNum(b.generated_left_out) && b.generated_left_out > 0
            ? h('dd', {class: 'bcard__stat-note muted', title: NOT_EXPORTED_HELP}, `${fmtInt(b.generated_left_out)} not exported`) : null)),
      totalGens ? h('div', {class: 'tag-row'}, LINK_ORDER.filter((k) => gens[k]).map((k) => h('span', {class: ['link-chip', `link-chip--${linkInfo(k).style}`], title: linkInfo(k).help}, `${fmtInt(gens[k])} ${linkInfo(k).short}`)))
        : h('p', {class: 'muted small'}, 'No linked generations in this export'),
      b.created_at ? h('p', {class: 'bcard__meta'}, 'Created ', timeEl(recordedDate(b.created_at, b.created_at_raw), {dateOnly: true})) : null));
}

// --- Detail -------------------------------------------------------------------------

export function renderMoodboard(root, route, ctx) {
  const {exportId, boardId} = route;
  ctx.setTitle('Moodboard');
  const nav = h('nav', {class: 'job-nav', 'aria-label': 'Moodboard navigation'},
    h('a', {class: 'btn btn--ghost btn--sm', href: hrefs.moodboards(exportId)}, icon('arrow-left', {size: 15}), 'All moodboards'));
  const head = pageHead({eyebrow: 'Moodboard', title: 'Loading moodboard…'});
  const body = h('div', null, loadingState('Loading moodboard…'));
  root.append(h('div', {class: 'page'}, nav, head.el, body));
  let banner = null;

  /** Load and draw the board. After a hide or show again: quiet keeps the page until the new answer is drawn, and
   * focus names what takes focus then ('banner' or 'heading'). */
  async function load({quiet = false, focus = ''} = {}) {
    if (!quiet) fill(body, loadingState('Loading moodboard…'));
    try {
      const data = obj(await getJSON(paths.moodboard(exportId, boardId), {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      // Browsers fold '.' and '..' path segments, so such an id reaches another route: never show that as a board.
      if (str(obj(data.moodboard).id) !== str(boardId)) {
        throw new ApiError(404, 'unknown_moodboard', 'No moodboard with this id in the export.');
      }
      draw(data);
    } catch (err) {
      if (isAbort(err)) return;
      head.heading.textContent = err && err.status === 404 ? 'Moodboard not found' : 'Moodboard';
      setActions([]);
      fill(body, errorState(err, {retry: load, title: 'Could not load this moodboard'}));
    }
    if (focus === 'banner' && banner && banner.heading.isConnected) banner.heading.focus();
    else if (focus === 'heading' || focus === 'banner') head.heading.focus();
  }

  function setActions(content) {
    const old = head.el.querySelector('.page-head__actions');
    if (old) old.remove();
    if (content.length) head.el.append(h('div', {class: 'page-head__actions'}, content));
  }

  // After a hide the page stays where it is, now with the banner (Show again… is one click away); after a show
  // again it is drawn as everyone sees it.
  function hide(data) {
    runChange(ctx, () => confirmHide({exportId, kind: 'moodboard', id: boardId, data}), 'hide',
      'Hidden from everyone else', () => load({quiet: true, focus: 'banner'}));
  }

  function show(data) {
    const note = obj(data.hidden);
    runChange(ctx, () => confirmShow({exportId, kind: 'moodboard', id: boardId,
      name: str(obj(data.moodboard).name) || 'Untitled moodboard',
      generations: isNum(note.generations) ? note.generations : null,
      stayHidden: isNum(note.stay_hidden) ? note.stay_hidden : null}),
    'show', 'Shown to everyone again', () => load({quiet: true, focus: 'heading'}));
  }

  function draw(data) {
    const board = obj(data.moodboard);
    const name = str(board.name) || 'Untitled moodboard';
    head.heading.textContent = name;
    ctx.setTitle(name);
    const references = arr(data.references);
    const generations = arr(data.generations);
    const unresolved = arr(data.unresolved_generated_job_ids);
    const subItems = h('div', {class: 'page-sub'},
      codeTags(board.codes, board.code),
      board.version !== null && board.version !== undefined && board.version !== '' ? badge(`Version ${str(board.version)}`, {tone: 'muted'}) : null,
      board.created_at ? h('span', {class: 'page-sub__item'}, icon('clock', {size: 14}), 'Created ',
        timeEl(recordedDate(board.created_at, board.created_at_raw)),
        isNaiveDate(board.created_at_raw) ? h('span', {class: 'muted'}, ' (time zone not recorded)') : null) : null,
      h('span', {class: 'page-sub__item'}, 'ID ', idValue(board.id || boardId, {what: 'moodboard id'})));
    const textBox = head.el.querySelector('.page-head__text');
    const oldSub = textBox.querySelector('.page-sub');
    if (oldSub) oldSub.replaceWith(subItems);
    else textBox.append(subItems);
    // Hosted mode, the export's managers only: Hide… on a board everyone sees, the banner on a hidden one. The
    // gallery is what everyone sees, so a hidden board has nothing to show there.
    const managed = managerView(data);
    setActions([
      managed === 'hidden' ? null
        : h('a', {class: 'btn', href: hrefs.gallery(exportId, {moodboard: board.id || boardId})}, icon('grid', {size: 15}), 'Show in gallery'),
      managed === 'visible' ? hideButton('moodboard', () => hide(data)) : null,
    ].filter(Boolean));
    banner = managed === 'hidden' ? hiddenBanner({exportId, kind: 'moodboard', data, onShow: () => show(data)}) : null;

    fill(body,
      banner ? banner.el : null,
      legend(references.length, generations.length),
      referencesSection(references, name),
      generationsSection(generations, isNum(board.generated_left_out) ? board.generated_left_out : 0),
      unresolvedSection(unresolved),
      notesBlock(data.notes),
      unknownSection(arr(data.unknown_fields)),
      rawSection(data.raw, data.raw_withheld === true));
  }

  function legend(refCount, genCount) {
    return h('div', {class: 'legend', role: 'note', 'aria-label': 'How to read this page'},
      h('div', {class: 'legend__item legend__item--ref'},
        h('span', {class: 'legend__swatch legend__swatch--ref', 'aria-hidden': 'true'}, h('span')),
        h('div', null,
          h('p', {class: 'legend__title'}, `Reference images · ${fmtInt(refCount)}`),
          h('p', {class: 'legend__text'}, 'The board’s own inputs: pictures added to the moodboard. They were not generated by these jobs.'))),
      h('div', {class: 'legend__item legend__item--gen'},
        h('span', {class: 'legend__swatch legend__swatch--gen', 'aria-hidden': 'true'}, h('span')),
        h('div', null,
          h('p', {class: 'legend__title'}, `Generations made using this board · ${fmtInt(genCount)}`),
          h('p', {class: 'legend__text'}, 'Jobs from this export linked to the board. Each card says how the link was established.'))));
  }

  function referencesSection(references, boardName) {
    const sec = section('Reference images', {count: references.length, cls: 'section--ref', iconName: 'pin',
      intro: 'The board’s reference images when it was exported, in the order the export lists them. They may differ from the set a generation was made with.'});
    if (!references.length) {
      sec.body.append(h('p', {class: 'muted'}, 'This board has no reference images in the export.'));
      return sec.el;
    }
    const present = references.filter((r) => r && r.status === 'present' && safeFileUrl(r.url));
    const items = present.map((r) => ({
      url: r.url, thumb_url: r.thumb_url, width: r.width, height: r.height, file: r.file,
      label: `Reference ${(isNum(r.index) ? r.index : references.indexOf(r)) + 1}`,
    }));
    sec.body.append(h('ul', {class: 'refs', 'aria-label': 'Reference images'}, references.map((ref, i) => {
      const r = obj(ref);
      const n = (isNum(r.index) ? r.index : i) + 1;
      const frame = h('div', {class: 'ref__frame'});
      if (isNum(r.width) && isNum(r.height) && r.width > 0 && r.height > 0) frame.style.setProperty('--ar', `${r.width} / ${r.height}`);
      const pIndex = present.indexOf(ref);
      if (pIndex >= 0) {
        const button = h('button', {type: 'button', class: 'ref__btn', 'aria-label': `Open reference image ${n} of ${references.length} at full size`},
          imageEl(r, {mode: 'thumb', alt: `Reference image ${n}`}));
        button.addEventListener('click', () => openLightbox(items, pIndex, {opener: button, caption: `${boardName}: reference image`}));
        frame.append(button);
      } else {
        frame.append(placeholder(r.status, {file: str(r.file)}));
      }
      // A picture that is not available here (hosted mode: a hidden generation's) has no size or address to show.
      const unavailable = r.status === 'unavailable';
      return h('li', {class: 'ref'},
        frame,
        h('div', {class: 'ref__cap'},
          h('span', {class: 'ref__label'}, `Reference ${n}`),
          !unavailable && isNum(r.width) && isNum(r.height) ? h('span', {class: 'ref__dims'}, `${r.width} × ${r.height}`) : null,
          !unavailable && r.source_url ? copyButton(str(r.source_url), {what: `source URL of reference ${n}`, iconOnly: true}) : null));
    })));
    return sec.el;
  }

  function generationsSection(generations, leftOut) {
    // The full sentence is the first of the notes below the section.
    const sec = section('Generations made using this board', {count: generations.length, cls: 'section--gen', iconName: 'layers',
      actions: leftOut && generations.length ? badge(`${fmtInt(leftOut)} more not exported`,
        {tone: 'muted', iconName: 'unlink', title: NOT_EXPORTED_HELP}) : null});
    if (!generations.length) {
      sec.body.append(emptyState({
        iconName: 'layers',
        title: 'No linked generations in this export',
        body: [
          h('p', null, 'No exported job references this board. That does not prove the board was never used: jobs outside the export scope are not included, and older jobs may not record the reference.'),
          leftOut ? h('p', null, notExportedText(leftOut)) : null,
        ],
      }));
      return sec.el;
    }
    const groups = new Map();
    for (const g of generations) {
      const x = obj(g);
      const key = str(x.link) || 'unknown';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(x);
    }
    const order = [...LINK_ORDER.filter((k) => groups.has(k)), ...[...groups.keys()].filter((k) => !LINK_ORDER.includes(k))];
    for (const key of order) {
      const list = groups.get(key);
      const info = linkInfo(key);
      sec.body.append(h('div', {class: 'gen-group'},
        h('div', {class: 'gen-group__head'},
          linkChip(key),
          h('span', {class: 'gen-group__count'}, plural(list.length, 'job')),
          h('p', {class: 'gen-group__help'}, info.help)),
        h('ul', {class: 'grid grid--gen', 'aria-label': `${info.label}: generations`}, list.map((x) => {
          const job = obj(x.job);
          return gridItem(job, jobCard(job, {
            exportId,
            cls: 'card--gen',
            footer: h('div', {class: 'card__link'},
              linkChip(x.link, {short: false}),
              x.evidence ? h('p', {class: 'card__evidence'}, str(x.evidence)) : null),
          }));
        }))));
    }
    return sec.el;
  }

  function unresolvedSection(ids) {
    if (!ids.length) return null;
    const sec = section('generated_job_ids not in this export', {count: ids.length, iconName: 'unlink',
      intro: 'The exporter listed these job ids for the board, but the jobs themselves are not in this export (for example, outside the selected scope). They are kept, not dropped.'});
    sec.body.append(h('ul', {class: 'id-list'}, ids.map((id) => h('li', null, idValue(typeof id === 'string' ? id : JSON.stringify(id), {what: 'job id'})))));
    return sec.el;
  }

  function unknownSection(fields) {
    if (!fields.length) return null;
    const sec = section('Unknown fields', {count: fields.length, iconName: 'help',
      intro: 'Fields this viewer does not recognise. Their values are in the raw record below.'});
    sec.body.append(h('ul', {class: 'tag-list'}, fields.map((f) => h('li', {class: 'tag mono'}, str(f)))));
    return sec.el;
  }

  function rawSection(raw, withheld) {
    const sec = section('Raw record', {iconName: 'file', intro: withheld
      ? 'The original moodboards.jsonl row; ids and pictures of items not available here are left out.'
      : 'The original moodboards.jsonl row, unchanged.'});
    sec.body.append(rawBlock(raw === undefined ? null : raw, {summary: 'Show moodboards.jsonl row'}));
    return sec.el;
  }

  load();
  return closeConfirm;  // a hide or show-again dialog goes with its view (one that is working stays until it ends)
}
