// Overview: what was exported and how complete it is. In hosted mode the
// export's managers also see what is hidden from everyone else.

import {h, icon, fill, append} from './dom.js';
import {hrefs} from './router.js';
import {loadOverview, isAbort} from './api.js';
import {arr, str, obj, isNum, fmtInt, fmtBytes, kindLabel, plural, shortId} from './format.js';
import {
  pageHead, section, statusBadge, supportBadge, badge, errorState, loadingState, callout, timeEl, kvList, dataTable,
  rawBlock, renderValue, jobLink, emptyState, linkChip,
} from './ui.js';
import {canHide, hiddenSection} from './hide.js';
import {closeConfirm} from './confirm.js';

const EXPORT_TIME_HELP = 'export_time: when export_meta.json was last written, at the end or stop of the latest run.';

const LEVELS = [
  {level: 'error', one: 'error', many: 'errors', tone: 'err', iconName: 'alert-circle'},
  {level: 'warning', one: 'warning', many: 'warnings', tone: 'warn', iconName: 'alert-triangle'},
  {level: 'info', one: 'note', many: 'notes', tone: 'info', iconName: 'info'},
  {level: 'ok', one: 'passed', many: 'passed', tone: 'ok', iconName: 'check-circle'},
];

export function renderOverview(root, route, ctx) {
  const {exportId} = route;
  ctx.setTitle('Overview');
  const head = pageHead({eyebrow: 'Export overview', title: 'Loading export…'});
  const body = h('div', {class: 'overview'}, loadingState('Reading the export summary…'));
  root.append(h('div', {class: 'page'}, head.el, body));
  let hidden = null;

  /** Load and draw the overview; focusHidden: then focus the Hidden section's heading (after a show again). */
  async function load({focusHidden = false} = {}) {
    fill(body, loadingState('Reading the export summary…'));
    hidden = null;
    try {
      const data = await loadOverview(exportId, {signal: ctx.signal});
      if (!ctx.isActive()) return;
      draw(data);
    } catch (err) {
      if (isAbort(err)) return;
      head.heading.textContent = 'Export overview';
      fill(body, errorState(err, {retry: load, title: 'Could not read this export', home: true}));
      // A hidden list that cannot be applied keeps the export closed to everyone else; its managers can still
      // show items again from here, and the export opens once the list that is left applies.
      if (err && err.code === 'hidden_apply_failed' && canHide(exportId)) {
        hidden = hiddenPart();
        body.append(hidden.el);
      }
    }
    if (focusHidden && hidden && hidden.heading.isConnected) hidden.heading.focus();
  }

  /** The Hidden section (the export's managers only); after a show again the overview is read anew. */
  function hiddenPart() {
    return hiddenSection(exportId, ctx, {onChanged: () => load({focusHidden: true})});
  }

  function draw(data) {
    const d = obj(data);
    const summary = obj(d.summary);
    const meta = d.meta && typeof d.meta === 'object' ? d.meta : null;
    const label = str(summary.label) || exportId;
    head.heading.textContent = label;
    ctx.setTitle('Overview');
    const sub = head.el.querySelector('.page-sub') || h('div', {class: 'page-sub'});
    fill(sub,
      statusBadge(d.status !== undefined ? d.status : summary.status),
      supportBadge(summary.version_support, summary.extension_version),
      summary.username ? h('span', {class: 'page-sub__item'}, icon('user', {size: 14}), str(summary.username)) : null,
      summary.export_time ? h('span', {class: 'page-sub__item', title: EXPORT_TIME_HELP}, icon('clock', {size: 14}), 'Exported ', timeEl(summary.export_time)) : null);
    if (!sub.isConnected) head.el.querySelector('.page-head__text').append(sub);

    const checks = arr(d.checks);
    const counts = obj(d.counts);
    const observed = obj(counts.observed);
    const facets = obj(d.facets);
    hidden = canHide(exportId) ? hiddenPart() : null;

    fill(body,
      versionNotice(summary),
      statTiles(exportId, summary, observed, facets),
      hidden ? hidden.el : null,
      h('div', {class: 'overview__cols'},
        h('div', {class: 'overview__main'}, checksSection(checks, exportId).el),
        h('div', {class: 'overview__side'}, detailsSection(d, summary).el)),
      countsSection(counts).el,
      selectionSection(d.selection),
      failuresSection(d, exportId),
      gapsSection(d, exportId),
      filesSection(obj(d.files), exportId),
      unresolvedSection(obj(d.unresolved), exportId, facets),
      limitationsSection(arr(d.limitations), Boolean(meta)),
      rawMetaSection(meta));
  }

  load();
  return closeConfirm;  // a show-again dialog goes with its view (one that is working stays until it ends)
}

function versionNotice(summary) {
  if (summary.version_support === 'unknown') {
    return callout('warn', h('p', null,
      `This viewer was not checked against exporter version ${str(summary.extension_version) || '(unknown)'}. `,
      'Everything readable is shown; fields the viewer does not recognise are listed as unknown.'));
  }
  if (summary.version_support === 'missing') {
    return callout('warn', h('p', null, 'This export does not record an exporter version. Everything readable is shown on a best-effort basis.'));
  }
  return null;
}

function statTiles(exportId, summary, observed, facets) {
  const counts = obj(summary.counts);
  const pick = (key) => (isNum(observed[key]) ? observed[key] : counts[key]);
  const kinds = arr(facets.kinds).filter((k) => k && isNum(k.count)).sort((a, b) => b.count - a.count);
  const kindText = kinds.slice(0, 3).map((k) => `${fmtInt(k.count)} ${kindLabel(k.value).toLowerCase()}`).join(' · ');
  const expected = pick('images_expected');
  const present = pick('images_present');
  const missing = observed.images_missing;
  const tile = (label, value, metaText, href, tone = '') => {
    const content = [h('span', {class: 'stat__label'}, label), h('span', {class: 'stat__value'}, value),
      metaText ? h('span', {class: ['stat__meta', tone && `is-${tone}`]}, metaText) : null];
    return href ? h('a', {class: 'stat', href}, content) : h('div', {class: 'stat'}, content);
  };
  return h('div', {class: 'stats'},
    tile('Jobs', fmtInt(pick('jobs')), kindText, hrefs.gallery(exportId)),
    tile('Images', isNum(expected) ? `${fmtInt(present)} / ${fmtInt(expected)}` : fmtInt(present),
      isNum(missing) && missing > 0 ? `${fmtInt(missing)} missing` : isNum(observed.bytes_present) ? fmtBytes(observed.bytes_present) : '',
      null, isNum(missing) && missing > 0 ? 'warn' : ''),
    tile('Moodboards', fmtInt(pick('moodboards')), '', hrefs.moodboards(exportId)),
    tile('Profiles', fmtInt(pick('profiles')), '', hrefs.profiles(exportId)),
    tile('Likes', fmtInt(pick('likes')), '', hrefs.likes(exportId)));
}

function checksSection(checks, exportId) {
  const counts = Object.fromEntries(LEVELS.map((l) => [l.level, 0]));
  for (const c of checks) counts[c && LEVELS.some((l) => l.level === c.level) ? c.level : 'info'] += 1;
  const summaryText = LEVELS.filter((l) => counts[l.level])
    .map((l) => `${fmtInt(counts[l.level])} ${counts[l.level] === 1 ? l.one : l.many}`).join(' · ');
  const sec = section('Completeness checks', {iconName: 'check-circle', intro: summaryText || null});
  if (!checks.length) {
    sec.body.append(emptyState({iconName: 'info', title: 'No checks reported', body: h('p', null, 'The server returned no checks for this export.')}));
    return sec;
  }
  for (const level of LEVELS) {
    const group = checks.filter((c) => (c && LEVELS.some((l) => l.level === c.level) ? c.level : 'info') === level.level);
    if (!group.length) continue;
    const list = h('div', {class: 'checks'}, group.map((c) => checkItem(c, level, exportId)));
    if (level.level === 'ok') {
      sec.body.append(h('details', {class: 'checks-ok'},
        h('summary', null, icon('chevron-right', {size: 14, cls: 'raw__chev'}), `${plural(group.length, 'check')} passed`),
        list));
    } else {
      sec.body.append(list);
    }
  }
  return sec;
}

function checkItem(check, level, exportId) {
  const c = obj(check);
  const items = arr(c.items);
  return h('div', {class: ['check', `check--${level.level}`]},
    h('span', {class: 'check__icon'}, icon(level.iconName, {size: 17})),
    h('div', {class: 'check__body'},
      h('p', {class: 'check__title'}, str(c.title) || str(c.id) || 'Check'),
      c.detail ? h('p', {class: 'check__detail'}, str(c.detail)) : null,
      items.length ? h('details', {class: 'check__items'},
        h('summary', null, `Show ${plural(items.length, 'item')}`),
        h('ul', {class: 'check__list'}, items.slice(0, 200).map((it) => h('li', null, renderValue(it, {exportId}))),
          items.length > 200 ? h('li', {class: 'muted'}, `and ${fmtInt(items.length - 200)} more`) : null)) : null));
}

// scope.max is always written (default 20) and scope.since is '' when unused, so
// each is shown only for its own mode.
function scopeText(scope) {
  const s = obj(scope);
  switch (s.mode) {
    case 'all': return 'All jobs the own-job feed returned';
    case 'since': return s.since ? `Jobs created on or after ${str(s.since)} (00:00 UTC)` : 'Jobs since a date (date not recorded)';
    case 'max': return isNum(s.max) ? `The first ${fmtInt(s.max)} jobs in feed order` : 'A maximum number of jobs';
    default: return s.mode ? `Mode: ${str(s.mode)}` : 'Not recorded';
  }
}

function feedText(feed, scope) {
  if (feed === true) return 'Read to the end of the own-job feed';
  if (feed === false) {
    return obj(scope).mode === 'max'
      ? 'Stopped at the selected maximum; older jobs were not read'
      : h('span', {class: 'is-warn'}, 'Stopped before the end of the feed; more jobs may exist');
  }
  return h('span', {class: 'is-warn'}, 'Not recorded; the run may have stopped before reading the feed');
}

function boolText(value, yes, no) {
  if (value === true) return yes;
  if (value === false) return no;
  return h('span', {class: 'muted'}, 'Not recorded');
}

function detailsSection(d, summary) {
  const meta = d.meta && typeof d.meta === 'object' ? d.meta : null;
  const source = obj(summary.source);
  const scope = d.scope && typeof d.scope === 'object' ? d.scope : null;
  const sec = section('Export details', {iconName: 'archive'});
  sec.body.append(h('div', {class: 'panel'}, kvList([
    ['Source', h('span', {class: 'stack'},
      h('span', null, source.type === 'folder' ? 'Folder' : source.type === 'upload' ? 'Uploaded ZIP'
        : source.type === 'catalog' ? 'Published ZIP' : 'ZIP file'),
      source.path || source.name ? h('span', {class: 'mono small break'}, str(source.path || source.name)) : null)],
    summary.root ? [source.type === 'folder' ? 'Export subfolder' : 'Folder in ZIP', h('span', {class: 'mono small break'}, str(summary.root))] : undefined,
    ['Username', summary.username ? str(summary.username) : null],
    ['Exported', summary.export_time ? h('span', {class: 'stack'}, timeEl(summary.export_time),
      h('span', {class: 'note'}, 'When export_meta.json was last written: the end or stop of the latest run.')) : null, EXPORT_TIME_HELP],
    ['Extension', h('span', {class: 'stack-inline'}, str(summary.extension_version) || '—', supportBadge(summary.version_support))],
    ['Status', statusBadge(d.status !== undefined ? d.status : summary.status)],
    meta ? ['Feed', feedText(d.feed_exhausted, scope), 'feed_exhausted'] : undefined,
    ['Scope', scope ? scopeText(scope) : h('span', {class: 'muted'}, 'Not recorded')],
    ['Images', scope ? boolText(scope.images, 'Included', 'Metadata only (no image files)') : undefined],
    ['Small test', scope ? boolText(scope.testSubset, 'On: at most one moodboard and one profile', 'Off') : undefined, 'testSubset'],
    selectionRow(d.selection, meta),
  ].filter(Boolean))));
  return sec;
}

// Exporters that let the designer choose write selection: null when they did not; older ones write no key.
function selectionRow(selection, meta) {
  if (selection && typeof selection === 'object') {
    return ['Selection', h('span', {class: 'stack'}, 'Chosen by the designer',
      selection.chosen_at ? h('span', {class: 'note'}, 'On ', timeEl(selection.chosen_at)) : null), 'selection'];
  }
  if (!meta || !('selection' in meta)) return undefined;
  return ['Selection', meta.selection === null ? 'Not used: the designer did not leave anything out'
    : h('span', {class: 'is-warn'}, 'Recorded, but not readable (see the checks)'), 'selection'];
}

function selectionSection(selection) {
  if (!selection || typeof selection !== 'object') return null;
  const boards = obj(selection.moodboards);
  const jobs = obj(selection.jobs);
  const sec = section('What the designer chose to export', {
    iconName: 'sliders',
    intro: 'The designer picked the moodboards and generations to export. What was left out is not in this export: no rows and no images. Generations that appeared after the choice were not exported either.',
  });
  const cell = (v) => (isNum(v) ? fmtInt(v) : h('span', {class: 'muted'}, '—'));
  sec.body.append(dataTable([
    {label: 'Item'}, {label: 'In scope', cls: 'num'}, {label: 'Exported', cls: 'num'}, {label: 'Left out', cls: 'num'},
    {label: 'Appeared after choosing', cls: 'num'},
  ], [
    ['Moodboards', cell(boards.available), cell(boards.exported), cell(boards.left_out), h('span', {class: 'muted'}, '—')],
    ['Generations', cell(jobs.available), cell(jobs.exported), cell(jobs.left_out), cell(jobs.not_reviewed)],
  ], {caption: 'What the designer chose to export', cls: 'table--selection'}));
  // Counted by the server from the references it labels left out, not from the id list as written.
  const used = isNum(selection.left_out_moodboards_used) ? selection.left_out_moodboards_used : 0;
  if (selection.chosen_at || used) {
    sec.body.append(h('div', {class: 'selection-notes'},
      selection.chosen_at ? h('p', null, 'Chosen ', timeEl(selection.chosen_at), '.') : null,
      used ? h('p', null, `${plural(used, 'left-out moodboard was', 'left-out moodboards were')} used by exported generations. `,
        'Job pages label those references ', linkChip('left_out'), '.') : null));
  }
  return sec.el;
}

const COUNT_ROWS = [
  {label: 'Jobs', meta: 'jobs', observed: 'jobs'},
  {label: 'Moodboards', meta: 'moodboards', observed: 'moodboards'},
  {label: 'Profiles', meta: 'profiles', observed: 'profiles'},
  {label: 'Likes', meta: 'likes', observed: 'likes'},
  {label: 'Image files', meta: 'files', observed: 'images_present'},
  {label: 'Bytes', meta: 'download_bytes', observed: 'bytes_present', bytes: true},
  {label: 'Completed jobs', meta: 'completed_jobs', observed: null},
  {label: 'Images referenced', meta: null, observed: 'images_expected'},
  {label: 'Images missing', meta: null, observed: 'images_missing', alertIfPositive: true},
  {label: 'Unreferenced files', meta: null, observed: 'images_unreferenced', alertIfPositive: true},
  {label: 'Unsupported files', meta: null, observed: 'images_unsupported', alertIfPositive: true},
];

function countsSection(counts) {
  const meta = counts.meta && typeof counts.meta === 'object' ? counts.meta : null;
  const observed = obj(counts.observed);
  const sec = section('Counts', {
    iconName: 'hash',
    intro: 'What export_meta.json claims next to what the viewer found in the files. Differences are highlighted.',
  });
  const known = new Set();
  const rows = [];
  let mismatches = 0;
  for (const row of COUNT_ROWS) {
    if (row.meta) known.add(row.meta);
    const mv = meta && row.meta ? meta[row.meta] : undefined;
    const ov = row.observed ? observed[row.observed] : undefined;
    if (mv === undefined && ov === undefined) continue;
    const differs = isNum(mv) && isNum(ov) && mv !== ov;
    // Byte counts that differ slightly would round to the same text, so show them exactly.
    const fmt = (v) => (v === undefined ? h('span', {class: 'muted'}, '—')
      : row.bytes && isNum(v) ? (differs ? `${fmtInt(v)} B` : fmtBytes(v)) : isNum(v) ? fmtInt(v) : str(v));
    const alert = row.alertIfPositive && isNum(ov) && ov > 0;
    if (differs) mismatches += 1;
    rows.push({
      cls: differs || alert ? 'is-mismatch' : '',
      cells: [row.label, fmt(mv), fmt(ov),
        differs ? badge('Differs', {tone: 'warn', iconName: 'alert-triangle'})
          : alert ? badge('Check', {tone: 'warn', iconName: 'alert-triangle'})
            : isNum(mv) && isNum(ov) ? badge('Match', {tone: 'ok'}) : h('span', {class: 'muted'}, '—')],
    });
  }
  if (meta) {
    for (const [key, value] of Object.entries(meta)) {
      if (known.has(key)) continue;
      rows.push([h('span', {class: 'mono'}, key), isNum(value) ? fmtInt(value) : str(JSON.stringify(value)), h('span', {class: 'muted'}, '—'),
        h('span', {class: 'muted'}, 'Unknown field')]);
    }
  }
  if (!meta) {
    sec.body.append(callout('warn', h('p', null, 'export_meta.json is missing or has no counts, so only observed values are shown.')));
  } else if (mismatches) {
    sec.body.append(callout('warn', h('p', null, `${plural(mismatches, 'count')} in export_meta.json ${mismatches === 1 ? 'differs' : 'differ'} from what is in the files.`)));
  }
  sec.body.append(dataTable([
    {label: 'Item'}, {label: 'export_meta.json', cls: 'num'}, {label: 'Observed', cls: 'num'}, {label: 'Result'},
  ], rows, {caption: 'Counts from export metadata compared with observed files', cls: 'table--counts'}));
  return sec;
}

function failureId(kind, id, exportId) {
  if (kind === 'job' && id) return jobLink(exportId, id, {short: true});
  return h('span', {class: 'mono break'}, str(id) || '—');
}

function failuresSection(d, exportId) {
  const failures = arr(d.failures);
  const hasMeta = Boolean(d.meta && typeof d.meta === 'object');
  const sec = section('Failures recorded by the exporter', {count: hasMeta ? failures.length : null, iconName: 'alert-circle'});
  if (!hasMeta) {
    sec.body.append(h('p', {class: 'muted'}, 'Unknown: export_meta.json is missing or unreadable, so the exporter’s failure list is not available.'));
    return sec.el;
  }
  if (!failures.length) {
    sec.body.append(h('p', {class: 'muted'}, 'None. export_meta.json lists no failures from the latest run.'));
    return sec.el;
  }
  sec.body.append(dataTable([{label: 'Kind'}, {label: 'Item'}, {label: 'Reason'}],
    failures.map((f) => {
      const x = obj(f);
      return [badge(str(x.kind) || 'unknown', {tone: 'err'}), failureId(x.kind, x.id, exportId), str(x.reason) || '—'];
    }), {caption: 'Failures'}));
  return sec.el;
}

function gapsSection(d, exportId) {
  const gaps = arr(d.parent_gaps);
  if (!gaps.length) return null;
  const leftOut = gaps.some((g) => obj(g).left_out === true);
  const sec = section('Parent gaps', {
    count: gaps.length,
    iconName: 'unlink',
    intro: leftOut
      ? 'Jobs whose parent is not in this export, as reported in export_meta.json. A date or count limit, or the designer’s choice, explains them.'
      : 'Jobs whose parent is not in this export, as reported in export_meta.json. A date or count limit usually explains them.',
  });
  sec.body.append(dataTable([{label: 'Job'}, {label: 'Missing parent'}, {label: 'Reason'}],
    gaps.map((g) => {
      const x = obj(g);
      const parent = h('span', {class: 'mono break'}, str(x.parent_id) || '—');
      return [jobLink(exportId, x.job_id, {short: true}),
        x.left_out === true ? h('span', {class: 'stack'}, parent, badge('Left out by the designer', {iconName: 'unlink'})) : parent,
        str(x.reason) || '—'];
    }), {caption: 'Parent gaps'}));
  return sec.el;
}

function filesSection(files, exportId) {
  const missing = arr(files.missing);
  const unreferenced = arr(files.unreferenced);
  const unreadable = arr(files.unreadable);
  const total = missing.length + unreferenced.length + unreadable.length;
  const sec = section('Files', {count: total || null, iconName: 'file'});
  if (!total) {
    sec.body.append(h('p', {class: 'muted'}, 'Every referenced file is present and readable, and no image file is unreferenced.'));
    return sec.el;
  }
  if (missing.length) {
    append(sec.body, [h('h3', {class: 'subhead'}, `Missing (${fmtInt(missing.length)})`),
      h('p', {class: 'section__intro'}, 'Referenced in the metadata but not found in the export.'),
      dataTable([{label: 'Path'}, {label: 'Referenced by'}], missing.slice(0, 500).map((m) => {
        const x = obj(m);
        return [h('span', {class: 'mono break'}, str(x.path)), referencedBy(x.referenced_by, exportId)];
      }), {caption: 'Missing files'}),
      missing.length > 500 ? h('p', {class: 'muted'}, `and ${fmtInt(missing.length - 500)} more`) : null]);
  }
  if (unreferenced.length) {
    append(sec.body, [h('h3', {class: 'subhead'}, `Unreferenced (${fmtInt(unreferenced.length)})`),
      h('p', {class: 'section__intro'}, 'Image files in the export that no job or moodboard refers to.'),
      h('ul', {class: 'path-list'}, unreferenced.slice(0, 500).map((p) => h('li', {class: 'mono break'}, typeof p === 'string' ? p : JSON.stringify(p)))),
      unreferenced.length > 500 ? h('p', {class: 'muted'}, `and ${fmtInt(unreferenced.length - 500)} more`) : null]);
  }
  if (unreadable.length) {
    sec.body.append(h('h3', {class: 'subhead'}, `Unreadable (${fmtInt(unreadable.length)})`),
      dataTable([{label: 'Path'}, {label: 'Reason'}], unreadable.slice(0, 500).map((u) => {
        const x = obj(u);
        return [h('span', {class: 'mono break'}, str(x.path)), str(x.reason) || '—'];
      }), {caption: 'Unreadable files'}));
  }
  return sec.el;
}

function referencedBy(value, exportId) {
  if (Array.isArray(value)) {
    if (!value.length) return h('span', {class: 'muted'}, '—');
    const shown = value.slice(0, 12).map((ref, i) => {
      const x = obj(ref);
      const n = isNum(x.index) ? x.index + 1 : null;
      let el;
      if (x.kind === 'job' && x.id) {
        el = jobLink(exportId, x.id, {label: `job ${shortId(x.id)}${n !== null ? `, image ${n}` : ''}`});
      } else if (x.kind === 'moodboard' && x.id) {
        el = h('a', {class: 'mono', href: hrefs.moodboard(exportId, x.id), title: str(x.id)},
          `moodboard ${shortId(x.id, 10)}${n !== null ? `, reference ${n}` : ''}`);
      } else {
        el = renderValue(ref, {exportId});
      }
      return h('span', {class: 'nowrap'}, i ? '; ' : '', el);
    });
    return h('span', null, shown, value.length > 12 ? ` and ${fmtInt(value.length - 12)} more` : '');
  }
  if (typeof value === 'string' && value) {
    const m = /^(job|moodboard|board):(.+)$/.exec(value);
    if (m && m[1] === 'job') return jobLink(exportId, m[2], {label: `job ${shortId(m[2])}`});
    if (m) return h('a', {class: 'mono', href: hrefs.moodboard(exportId, m[2]), title: m[2]}, `moodboard ${shortId(m[2], 10)}`);
    return h('span', {class: 'mono break'}, value);
  }
  return renderValue(value, {exportId});
}

function sourceLabel(source) {
  if (source === 'flag') return '--profile flag';
  if (source === 'reference') return 'Reference';
  return source ? str(source) : '—';
}

function unresolvedSection(unresolved, exportId, facets) {
  const boardNames = new Map(arr(obj(facets).moodboards).map((m) => [str(m.id), str(m.label)]));
  const refs = arr(unresolved.references);
  const ids = arr(unresolved.generated_job_ids);
  const gaps = arr(unresolved.parent_gaps);
  const total = refs.length + ids.length + gaps.length;
  const sec = section('Unresolved relationships', {
    count: total || null,
    iconName: 'link',
    intro: 'Links the viewer could not connect to anything in this export. They are listed, never guessed.',
  });
  if (!total) {
    sec.body.append(h('p', {class: 'muted'}, 'Every personalization reference, generated_job_id and parent resolves inside this export.'));
    return sec.el;
  }
  if (refs.length) {
    sec.body.append(h('h3', {class: 'subhead'}, `Personalization references (${fmtInt(refs.length)})`),
      dataTable([{label: 'Job'}, {label: 'Code'}, {label: 'Type'}, {label: 'From'}, {label: 'Reason'}], refs.map((r) => {
        const x = obj(r);
        return [x.job_id ? jobLink(exportId, x.job_id, {short: true}) : h('span', {class: 'muted'}, '—'),
          x.code ? h('a', {class: 'mono', href: hrefs.gallery(exportId, {profile: `code:${str(x.code)}`}), title: 'Show jobs with this code'}, str(x.code)) : '—',
          str(x.type) || '—', sourceLabel(x.source), str(x.reason) || '—'];
      }), {caption: 'Unresolved personalization references'}));
  }
  if (ids.length) {
    sec.body.append(h('h3', {class: 'subhead'}, `generated_job_ids without a job (${fmtInt(ids.length)})`),
      dataTable([{label: 'Moodboard'}, {label: 'Job id'}, {label: 'Reason'}], ids.map((r) => {
        const x = typeof r === 'string' ? {job_id: r} : obj(r);
        const board = str(x.board_id || x.moodboard_id);
        const name = str(x.board_name || x.moodboard_name) || boardNames.get(board) || board;
        return [board ? h('a', {href: hrefs.moodboard(exportId, board), title: board}, name) : '—',
          h('span', {class: 'mono break'}, str(x.job_id) || '—'), str(x.reason) || 'Not in this export'];
      }), {caption: 'Unresolved generated_job_ids'}));
  }
  if (gaps.length) {
    sec.body.append(h('h3', {class: 'subhead'}, `Parents outside the export (${fmtInt(gaps.length)})`),
      dataTable([{label: 'Job'}, {label: 'Parent id'}, {label: 'In export_meta'}, {label: 'Reason'}], gaps.map((g) => {
        const x = obj(g);
        const flag = [x.reported_by_exporter, x.in_meta, x.listed_in_meta].find((v) => typeof v === 'boolean');
        const inMeta = flag === true ? 'Listed' : flag === false ? badge('Not listed', {tone: 'warn'}) : '—';
        return [jobLink(exportId, x.job_id, {short: true}), h('span', {class: 'mono break'}, str(x.parent_id) || '—'), inMeta, str(x.reason) || '—'];
      }), {caption: 'Unresolved parents'}));
  }
  return sec.el;
}

function limitationsSection(limitations, hasMeta) {
  const sec = section('Limitations stated by the exporter', {iconName: 'info'});
  if (!limitations.length) {
    sec.body.append(h('p', {class: 'muted'}, hasMeta
      ? 'export_meta.json states no limitations.'
      : 'Unknown: export_meta.json is missing or unreadable.'));
    return sec.el;
  }
  sec.body.append(h('ul', {class: 'quotes'}, limitations.map((l) => h('li', null, h('blockquote', null, typeof l === 'string' ? l : JSON.stringify(l))))));
  return sec.el;
}

function rawMetaSection(meta) {
  const sec = section('export_meta.json', {iconName: 'file'});
  if (!meta) {
    sec.body.append(callout('warn', h('p', null, 'This export has no readable export_meta.json.')));
    return sec.el;
  }
  sec.body.append(rawBlock(meta, {summary: 'Show the raw file'}));
  return sec.el;
}

