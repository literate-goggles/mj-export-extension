// Hosted mode: hiding one generation or one moodboard of a published export
// from everyone but the export's owner and the viewer's administrators (its
// managers), and showing it again. The server decides who manages an export and
// what everyone else sees: a manager's catalog item has can_hide, and the
// detail of an item carries hide_preview (a visible item) or hidden (a hidden
// one) for its managers only. This module asks first, in plain words, and draws
// what managers see: the Hide… button, the banner on a hidden item's page and
// the Hidden section of the overview. Local mode shows none of it.

import {h, icon, fill, nextId} from './dom.js';
import {hrefs} from './router.js';
import {hosted, catalogItemOf, catalogIdOf, knownExports, setHidden, loadHidden, getJSON, paths, isAbort} from './api.js';
import {arr, str, obj, isNum, fmtInt, plural, shortId} from './format.js';
import {
  section, imageEl, placeholder, timeEl, errorState, loadingState, writeFailure, pageNotice, announce,
} from './ui.js';
import {confirmAction} from './confirm.js';

const PROMPT_CHARS = 120;
const STAYS_STORED = "It stays in the stored export, and the owner or an administrator can show it again from the export's overview.";
const BROWSER_CACHE = 'Browsers that already showed its pictures may keep them for up to 10 minutes.';
const MANAGERS_ONLY = "Only the export's owner and the viewer's administrators can see";
const NOT_RECORDED = 'Who hid it and when were not recorded.';
const NOT_HERE = 'Not in this export';
const SECTION_INTRO = "Only this export's owner and the viewer's administrators see this. Everyone else sees the export "
  + 'as if these were never in it: lists, search, counts and checks leave them out. They stay in the stored export '
  + 'file, and what was made from them stays visible.';

/** True when the signed-in user manages this published export: may hide in it and see what it hides. */
export function canHide(exportId) {
  return hosted && obj(catalogItemOf(exportId)).can_hide === true;
}

/**
 * How the server answered an item's detail: 'visible' (a manager's view of an item everyone sees, with
 * hide_preview), 'hidden' (a manager's view of a hidden item, with hidden), or null (everyone else, local mode).
 */
export function managerView(data) {
  const d = obj(data);
  if (!hosted) return null;
  if (d.hidden && typeof d.hidden === 'object') return 'hidden';
  if (d.hide_preview && typeof d.hide_preview === 'object') return 'visible';
  return null;
}

/** The Hide… button of a generation's or a moodboard's page. */
export function hideButton(kind, onclick) {
  const label = `Hide this ${kind === 'job' ? 'generation' : 'moodboard'} from everyone else`;
  return h('button', {type: 'button', class: 'btn btn--ghost hide-btn', 'aria-label': label, title: label, onclick},
    icon('eye-off', {size: 15}), 'Hide…');
}

/**
 * Run a hide or show-again dialog (open() resolves true once the change is made); then, while the view that
 * opened it is still there, announce said and run then() (draw the view anew). A failure after the dialog was
 * gone is said on the page. action: 'hide' or 'show'.
 */
export async function runChange(ctx, open, action, said, then) {
  let done;
  try {
    done = await open();
  } catch (err) {
    pageNotice(hideFailure(err, action));
    return;
  }
  if (!done || !ctx.isActive()) return;
  announce(said);
  then();
}

/** What a hide or show-again dialog says when the change failed (see writeFailure). action: 'hide' or 'show'. */
export function hideFailure(err, action) {
  const show = action === 'show';
  return writeFailure(err, {
    notUpdated: 'The viewer could not update its list right now. Nothing changed. Try again in a few minutes.',
    unconfirmed: 'The viewer could not confirm whether this changed. Reload the page to check.',
    unconfirmedCode: 'hide_unconfirmed',
    gone: 'This export is not published any more. It may have been deleted.',
    refused: show ? 'Could not show it again' : 'Could not hide',
    own: ['not_owner', 'hide_disabled'],
    more: {
      unknown_job: 'This generation is not in this export.',
      unknown_moodboard: 'This moodboard is not in this export.',
      too_many_hidden: 'This export already hides 1,000 generations and moodboards, the most it can. Show some again first.',
      export_not_ready: 'The export is still opening on the server. Try again in a moment.',
    },
    otherwise: show ? 'Could not show this again. Reload the page and try again.'
      : 'Could not hide this. Reload the page and try again.',
  });
}

// --- Dialogs ----------------------------------------------------------------------

function clip(text) {
  return text.length > PROMPT_CHARS ? `${text.slice(0, PROMPT_CHARS - 1)}…` : text;
}

function promptText(job) {
  const prompt = str(obj(job).prompt).trim();
  return prompt ? clip(prompt) : 'No prompt text';
}

function boardName(board) {
  return str(obj(board).name) || 'Untitled moodboard';
}

/** A moodboard named in a sentence: its name, else its id when no export root has it. */
function boardLabel(board) {
  const b = obj(board);
  return str(b.name) || shortId(b.id, 12) || 'unnamed';
}

/** 'the moodboard A', 'the moodboards A and B', 'the moodboards A, B and C'. */
function boardsPhrase(boards) {
  const names = boards.map(boardLabel);
  if (!names.length) return 'a moodboard';
  if (names.length === 1) return `the moodboard ${names[0]}`;
  return `the moodboards ${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function facts(rows) {
  return h('dl', {class: 'confirm__facts'}, rows.map(([label, value]) => [h('dt', null, label), h('dd', null, value)]));
}

function madeFromSentence(n, from) {
  if (!isNum(n) || n <= 0) return null;
  return n === 1
    ? `1 generation made from ${from} (an upscale, a variation…) stays visible.`
    : `${fmtInt(n)} generations made from ${from} (upscales, variations…) stay visible.`;
}

/**
 * Ask before hiding a visible generation (kind 'job') or moodboard, with the numbers of its detail's hide_preview,
 * and hide it on confirm. Resolves true when it was hidden, false when cancelled; rejects only when hiding failed
 * after the dialog was gone (say so on the page).
 */
export function confirmHide({exportId, kind, id, data}) {
  const d = obj(data);
  const preview = obj(d.hide_preview);
  const job = kind === 'job';
  let lead;
  let factRows;
  if (job) {
    const card = obj(d.job);
    factRows = [['Generation', promptText(card)], ['Created', timeEl(card.created_at)]];
    lead = 'Nobody else will see it in this viewer: its page, the gallery, search and moodboards leave it out.';
  } else {
    const n = preview.generations;
    const made = !isNum(n) ? ', its reference images or the generations made with it'
      : n > 0 ? `, its reference images or ${n === 1 ? 'the 1 generation' : `the ${fmtInt(n)} generations`} made with it`
        : ' or its reference images';
    factRows = [['Moodboard', boardName(d.moodboard)], ['Reference images', fmtInt(arr(d.references).length)]];
    lead = `In this viewer, nobody else will see the moodboard${made}.`;
  }
  const count = job ? madeFromSentence(preview.made_from_it, 'it') : madeFromSentence(preview.made_from_them, 'those');
  return confirmAction({
    title: job ? 'Hide this generation?' : 'Hide this moodboard?',
    body: [facts(factRows), h('p', null, lead), count ? h('p', null, count) : null, h('p', null, STAYS_STORED),
      h('p', null, BROWSER_CACHE)],
    confirmLabel: job ? 'Hide generation' : 'Hide moodboard',
    busyLabel: 'Hiding…',
    tone: 'primary',
    failure: (err) => hideFailure(err, 'hide'),
    action: () => setHidden(exportId, kind, id, true),
  });
}

/**
 * Ask before hiding on its own a generation that is hidden only with a moodboard (from its banner), so that it
 * stays hidden when the moodboard is shown again.
 */
export function confirmHideOnItsOwn({exportId, id, data}) {
  const d = obj(data);
  const boards = arr(obj(d.hidden).with_moodboards).map(obj);
  const one = boards.length === 1;
  return confirmAction({
    title: 'Hide this generation on its own?',
    body: [facts([['Generation', promptText(d.job)]]),
      h('p', null, `It is hidden now because ${boardsPhrase(boards)} ${one ? 'is' : 'are'} hidden. Hidden on its own, `
        + `it stays hidden when ${one ? 'the moodboard is' : 'they are'} shown again.`)],
    confirmLabel: 'Hide on its own',
    busyLabel: 'Hiding…',
    tone: 'primary',
    failure: (err) => hideFailure(err, 'hide'),
    action: () => setHidden(exportId, 'job', id, true),
  });
}

/**
 * Ask before showing a hidden generation (kind 'job') or moodboard again, and show it on confirm. name: the
 * prompt or the moodboard's name; notHere: no export root has it; generations and stayHidden: how many
 * generations a moodboard hides and how many of those stay hidden another way (null when not known); withBoards:
 * the hidden moodboards that also hide a generation. Resolves and rejects like confirmHide.
 */
export function confirmShow({exportId, kind, id, name, notHere = false, generations = null, stayHidden = null,
  withBoards = [], returnFocus = null}) {
  const job = kind === 'job';
  let lead = 'Everyone who can open this export will see it again.';
  let more = null;
  if (notHere) {
    lead = 'It is not in this export, so nothing changes for anyone.';
  } else if (job && withBoards.length) {
    lead = `It stays hidden while ${boardsPhrase(withBoards)} ${withBoards.length === 1 ? 'is' : 'are'} hidden.`;
  } else if (!job && isNum(generations) && generations > 0) {
    const stay = isNum(stayHidden) && stayHidden > 0 ? Math.min(stayHidden, generations) : 0;
    const back = generations - stay;
    if (back > 0) {
      lead = stay
        ? `Everyone who can open this export will see it again, with ${fmtInt(back)} of its ${plural(generations, 'generation')}.`
        : `Everyone who can open this export will see it again, with its ${plural(generations, 'generation')}.`;
    }
    if (stay) {
      more = stay === 1 ? '1 of its generations stays hidden: it is hidden on its own or with another hidden moodboard.'
        : `${fmtInt(stay)} of its generations stay hidden: they are hidden on their own or with another hidden moodboard.`;
    }
  }
  return confirmAction({
    title: job ? 'Show this generation again?' : 'Show this moodboard again?',
    body: [facts([[job ? 'Generation' : 'Moodboard', clip(str(name))]]), h('p', null, lead), more ? h('p', null, more) : null],
    confirmLabel: 'Show again',
    busyLabel: 'Showing…',
    failure: (err) => hideFailure(err, 'show'),
    action: () => setHidden(exportId, kind, id, false),
    returnFocus,
  });
}

// --- Who hid it -----------------------------------------------------------------------

/** 'Hidden by <who> on <date>', 'Hidden by <who>', 'Hidden on <date>' or 'Hidden', as nodes (lead: the first word). */
function hiddenBy(at, by, lead = 'Hidden') {
  const parts = [lead];
  if (str(by)) parts.push(' by ', h('span', {class: 'hidden-by'}, str(by)));
  if (str(at)) parts.push(' on ', timeEl(at, {dateOnly: true}));
  return parts;
}

/** The who-and-when line of a row in the Hidden section. */
function whoWhen(at, by) {
  return str(at) || str(by) ? hiddenBy(at, by) : 'Who hid it and when were not recorded';
}

// --- Banner ---------------------------------------------------------------------------

/**
 * The banner on a hidden item's page, for its managers: who hid it and when, what went with it, and what they can
 * do: Show again…, or for a generation hidden only with a moodboard, Open the moodboard and Hide on its own….
 * Returns {el, heading}; the heading takes focus after a change.
 */
export function hiddenBanner({exportId, kind, data, onShow, onHideOwn}) {
  const note = obj(obj(data).hidden);
  const headingId = nextId('hidden-banner');
  const heading = h('h2', {class: 'hidden-banner__title', id: headingId, tabindex: '-1'}, 'Hidden from everyone else');
  const showButton = () => h('button', {type: 'button', class: 'btn btn--sm', onclick: onShow},
    icon('eye', {size: 14}), 'Show again…');
  let text;
  let actions;
  if (kind === 'moodboard') {
    const n = note.generations;
    const known = Boolean(str(note.hidden_at) || str(note.hidden_by));
    const what = isNum(n) && n > 0
      ? `its reference images and ${n === 1 ? 'the 1 generation' : `the ${fmtInt(n)} generations`} made with it`
      : 'its reference images';
    text = [...hiddenBy(note.hidden_at, note.hidden_by), `, with ${what}.`, known ? '' : ` ${NOT_RECORDED}`,
      ` ${MANAGERS_ONLY} them.`];
    actions = [showButton()];
  } else {
    const boards = arr(note.with_moodboards).map(obj).filter((b) => str(b.id));
    // Listed on its own, or (what the server never sends) on no list at all: either way Show again applies.
    const own = note.on_its_own && typeof note.on_its_own === 'object' ? note.on_its_own : boards.length ? null : {};
    if (own) {
      const known = Boolean(str(own.hidden_at) || str(own.hidden_by));
      text = [...hiddenBy(own.hidden_at, own.hidden_by), boards.length ? `, and with ${boardsPhrase(boards)}.` : '.',
        known ? '' : ` ${NOT_RECORDED}`, ` ${MANAGERS_ONLY} it.`];
      actions = [showButton()];
    } else {
      const first = boards[0] || {};
      const known = boards.length === 1 && Boolean(str(first.hidden_at) || str(first.hidden_by));
      text = boards.length === 1
        ? [`It is hidden with ${boardsPhrase(boards)}`, known ? [' (', ...hiddenBy(first.hidden_at, first.hidden_by, 'hidden'), ')'] : '',
          '. It comes back when the moodboard is shown again.']
        : [`It is hidden with ${boardsPhrase(boards)}. It comes back when they are all shown again.`];
      actions = [
        str(first.id) ? h('a', {class: 'btn btn--sm', href: hrefs.moodboard(exportId, first.id)},
          icon('board', {size: 14}), 'Open the moodboard') : null,
        h('button', {type: 'button', class: 'btn btn--sm', onclick: onHideOwn}, icon('eye-off', {size: 14}),
          'Hide on its own…'),
      ];
    }
  }
  const el = h('section', {class: 'callout callout--info hidden-banner', 'aria-labelledby': headingId},
    icon('eye-off', {size: 18}),
    h('div', {class: 'callout__body'},
      heading,
      h('p', {class: 'hidden-banner__text'}, text),
      h('div', {class: 'hidden-banner__actions'}, actions)));
  return {el, heading};
}

// --- Hidden section of the overview ---------------------------------------------------------

/**
 * The Hidden section of an export's overview, for its managers, over the whole catalog entry (every export root of
 * the ZIP): the moodboards and the generations hidden on their own, each with who hid it and when, and Show
 * again…. It loads what is hidden itself, never from a cache. onChanged() runs once something was shown again
 * (and announced). Returns {el, heading}; the heading takes focus after a change.
 */
export function hiddenSection(exportId, ctx, {onChanged}) {
  const sec = section('Hidden', {iconName: 'eye-off', cls: 'section--hidden', intro: SECTION_INTRO});
  const heading = sec.el.querySelector('.section__title');
  heading.setAttribute('tabindex', '-1');
  // A ZIP with several export roots: each row says which one has it.
  const several = catalogIdOf(exportId) !== String(exportId);

  async function load() {
    fill(sec.body, loadingState('Loading what is hidden…'));
    try {
      const data = obj(await loadHidden(exportId, {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      draw(data);
    } catch (err) {
      if (isAbort(err) || !ctx.isActive()) return;
      if (err && err.status === 403) {
        sec.el.remove();  // no longer one of its managers, or hiding was turned off
        return;
      }
      fill(sec.body, errorState(err, {retry: load, title: 'Could not load what is hidden'}));
    }
  }

  function draw(data) {
    const boards = arr(data.moodboards).map(obj).filter((b) => str(b.id));
    const jobs = arr(data.jobs).map(obj).filter((j) => str(j.id));
    for (const old of heading.querySelectorAll('.count')) old.remove();
    heading.append(h('span', {class: 'count'}, fmtInt(boards.length + jobs.length)));
    if (!boards.length && !jobs.length) {
      fill(sec.body, h('p', {class: 'muted'},
        'Nothing is hidden. To hide a generation or a moodboard from everyone else, open it and choose Hide.'));
      return;
    }
    fill(sec.body,
      boards.length ? [h('h3', {class: 'subhead'}, `Moodboards (${fmtInt(boards.length)})`),
        h('ul', {class: 'hidden-list', 'aria-label': 'Hidden moodboards'}, boards.map(boardRow))] : null,
      jobs.length ? [h('h3', {class: 'subhead'}, `Generations (${fmtInt(jobs.length)})`),
        h('ul', {class: 'hidden-list', 'aria-label': 'Hidden generations'}, jobs.map(jobRow))] : null,
      boards.length ? h('p', {class: 'hidden-list__note'},
        "Generations hidden with a moodboard are listed on the moodboard's page, not here.") : null);
  }

  /** Which root has the item: its label from the summaries of the roots this page knows, else its export id. */
  function rootLine(row) {
    if (!several || !row.export_id) return null;
    const exact = (list) => arr(list).map(obj).find((summary) => summary.id === row.export_id);
    const found = exact(knownExports()) || exact(obj(catalogItemOf(exportId)).exports) || {};
    return h('p', {class: 'hidden-row__meta'}, `In ${str(found.label) || str(row.export_id)}`);
  }

  function row(thumb, title, lines, button) {
    return h('li', {class: 'hidden-row'},
      thumb,
      h('div', {class: 'hidden-row__text'}, title, lines.filter(Boolean)),
      h('div', {class: 'hidden-row__actions'}, button));
  }

  function showButton(label, onclick) {
    return h('button', {type: 'button', class: 'btn btn--sm', 'aria-label': label, onclick},
      icon('eye', {size: 14}), 'Show again…');
  }

  function boardRow(b) {
    const here = Boolean(str(b.export_id));
    const cover = arr(b.cover).filter((r) => r && r.status === 'present').slice(0, 4);
    const thumb = h('span', {class: ['hidden-row__thumb', cover.length > 1 && 'hidden-row__thumb--mosaic']});
    if (cover.length) {
      for (const ref of cover) thumb.append(h('span', {class: 'hidden-row__cell'}, imageEl(ref, {mode: 'thumb', compact: true})));
    } else {
      thumb.append(placeholder(here ? 'not_exported' : 'unavailable', {compact: true}));
    }
    const name = here ? boardName(b) : `Moodboard ${shortId(b.id, 10)}`;
    const n = b.generations;
    // Generations of other roots that name an absent moodboard by its id are hidden with it too.
    const made = isNum(n) && n > 0 ? `${plural(n, 'generation')} hidden with it` : here ? 'No generations hidden with it' : '';
    const title = here
      ? h('a', {class: 'hidden-row__title', href: hrefs.moodboard(b.export_id, b.id)}, name)
      : h('span', {class: 'hidden-row__title mono'}, name);
    const button = showButton(`Show the moodboard ${name} again`, () => showBoard(b, name, button));
    return row(thumb, title, [
      here ? null : h('p', {class: 'hidden-row__note'}, NOT_HERE),
      made ? h('p', {class: 'hidden-row__meta'}, made) : null,
      h('p', {class: 'hidden-row__meta'}, whoWhen(b.hidden_at, b.hidden_by)),
      rootLine(b),
    ], button);
  }

  function jobRow(j) {
    const job = j.job && typeof j.job === 'object' ? j.job : null;
    const here = Boolean(str(j.export_id)) && job !== null;
    const images = job ? arr(job.images) : [];
    const first = images.find((r) => r && r.status === 'present') || images[0];
    const thumb = h('span', {class: 'hidden-row__thumb'},
      first ? imageEl(first, {mode: 'thumb', compact: true}) : placeholder(here ? 'not_exported' : 'unavailable', {compact: true}));
    const prompt = job ? str(job.prompt).trim() : '';
    const title = here
      ? h('a', {class: ['hidden-row__title', 'hidden-row__title--prompt', !prompt && 'is-empty'], href: hrefs.job(j.export_id, j.id),
        title: prompt || null}, prompt || 'No prompt text')
      : h('span', {class: 'hidden-row__title mono'}, `Generation ${shortId(j.id)}`);
    const boards = arr(j.with_moodboards).map(obj).filter((b) => str(b.id));
    const name = here ? promptText(job) : `Generation ${shortId(j.id)}`;
    const button = showButton(here ? 'Show this generation again' : `Show the generation ${shortId(j.id)} again`,
      () => showAgain(() => confirmShow({exportId, kind: 'job', id: j.id, name, notHere: !here, withBoards: boards,
        returnFocus: () => button})));
    return row(thumb, title, [
      here ? h('p', {class: 'hidden-row__meta'}, 'Created ', timeEl(job.created_at, {dateOnly: true}))
        : h('p', {class: 'hidden-row__note'}, NOT_HERE),
      h('p', {class: 'hidden-row__meta'}, whoWhen(j.hidden_at, j.hidden_by)),
      boards.length ? h('p', {class: 'hidden-row__meta'}, `Also hidden with ${boardsPhrase(boards)}`) : null,
      rootLine(j),
    ], button);
  }

  function showAgain(open) {
    runChange(ctx, open, 'show', 'Shown to everyone again', onChanged);
  }

  // A moodboard's own page says how many of its generations stay hidden another way; the dialog says so too.
  let asking = false;
  async function showBoard(b, name, button) {
    if (asking) return;
    asking = true;
    let generations = isNum(b.generations) ? b.generations : null;
    let stayHidden = null;
    try {
      if (str(b.export_id)) {
        const note = obj(obj(await getJSON(paths.moodboard(b.export_id, b.id), {signal: ctx.signal})).hidden);
        if (isNum(note.generations)) generations = note.generations;
        if (isNum(note.stay_hidden)) stayHidden = note.stay_hidden;
      }
    } catch (err) {
      if (isAbort(err)) {
        asking = false;
        return;
      }
      // The dialog still opens, with the list's numbers.
    }
    asking = false;
    if (!ctx.isActive() || !button.isConnected) return;
    showAgain(() => confirmShow({exportId, kind: 'moodboard', id: b.id, name, notHere: !str(b.export_id), generations,
      stayHidden, returnFocus: () => button}));
  }

  load();
  return {el: sec.el, heading};
}
