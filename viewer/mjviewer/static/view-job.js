// Job detail: images, command, flags, references, links, lineage, raw record.

import {h, icon, fill, isTypingTarget} from './dom.js';
import {hrefs} from './router.js';
import {getJSON, paths, isAbort, loadOverview, ApiError} from './api.js';
import {arr, str, obj, isNum, fmtInt, kindLabel, linkInfo, imageStatusInfo, plural} from './format.js';
import {
  pageHead, section, kindBadge, versionBadge, linkChip, badge, timeEl, copyButton, idValue, imageEl, placeholder,
  kvList, dataTable, rawBlock, renderValue, callout, errorState, loadingState, miniJob, jobLink, safeFileUrl,
} from './ui.js';
import {openLightbox, lightboxOpen} from './lightbox.js';

const FLAG_ORDER = ['ar', 'v', 'niji', 'stylize', 'chaos', 'weird', 'raw', 'tile', 'iw', 'sref', 'profile'];
const FLAG_HELP = {
  ar: 'Aspect ratio',
  v: 'Model version',
  niji: 'Niji model version',
  stylize: 'Stylize',
  chaos: 'Chaos',
  weird: 'Weird',
  raw: 'Raw style',
  tile: 'Tile',
  iw: 'Image weight',
  sref: 'Style reference',
  profile: 'Personalization profile or moodboard code',
};

export function renderJob(root, route, ctx) {
  const {exportId, jobId} = route;
  const from = route.query.get('in') || '';
  const posRaw = route.query.get('pos');
  const pos = posRaw !== null && /^\d{1,7}$/.test(posRaw) ? Number(posRaw) : null;
  const inGallery = pos !== null;
  ctx.setTitle('Job');

  const backParams = new URLSearchParams(from);
  backParams.delete('offset');
  backParams.delete('limit');
  const neighbours = h('div', {class: 'job-nav__steps'});
  const nav = h('nav', {class: 'job-nav', 'aria-label': 'Job navigation'},
    h('a', {class: 'btn btn--ghost btn--sm', href: hrefs.gallery(exportId, backParams)},
      icon('arrow-left', {size: 15}), inGallery ? 'Back to results' : 'Gallery'),
    neighbours);
  const head = pageHead({eyebrow: h('span', {class: 'eyebrow__row'}, 'Job'), title: 'Loading job…', titleCls: 'job-title'});
  const body = h('div', {class: 'job'}, loadingState('Loading job…'));
  root.append(h('div', {class: 'page page--job'}, nav, head.el, body));

  let prevHref = null;
  let nextHref = null;

  async function load() {
    fill(body, loadingState('Loading job…'));
    try {
      const data = obj(await getJSON(paths.job(exportId, jobId), {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      // Browsers fold '.' and '..' path segments, so such an id reaches another route: never show that as a job.
      if (str(obj(data.job).job_id) !== str(jobId)) throw new ApiError(404, 'unknown_job', 'No job with this id in the export.');
      draw(data);
    } catch (err) {
      if (isAbort(err)) return;
      head.heading.textContent = err && err.status === 404 ? 'Job not found' : 'Job';
      fill(body, errorState(err, {retry: load, title: 'Could not load this job'}));
    }
  }

  function draw(data) {
    const job = obj(data.job);
    const prompt = str(job.prompt).trim();
    head.heading.textContent = prompt || 'Job without prompt text';
    head.heading.classList.toggle('is-empty', !prompt);
    head.heading.classList.toggle('is-long', prompt.length > 180);
    ctx.setTitle(prompt ? (prompt.length > 60 ? `${prompt.slice(0, 57)}…` : prompt) : 'Job');
    const eyebrow = head.el.querySelector('.eyebrow');
    fill(eyebrow, h('span', {class: 'eyebrow__row'}, 'Job', kindBadge(job.kind, job.kind_source), versionBadge(job.version),
      job.liked ? badge('Liked', {tone: 'like', iconName: 'heart'}) : null,
      job.video ? badge('Video', {tone: 'warn', iconName: 'video', title: 'Video media is not exported'}) : null,
      timeEl(job.created_at, {cls: 'eyebrow__date'})));

    const images = arr(data.images).length ? arr(data.images) : arr(job.images);
    fill(body,
      h('div', {class: 'job-layout'},
        h('div', {class: 'job-media'}, imagesBlock(images, job), filesBlock(images)),
        h('div', {class: 'job-side'}, commandBlock(job), detailsBlock(job, images, data), relationsBlock(data, job))),
      lineageSection(obj(data.lineage), job),
      referencesSection(arr(data.references)),
      flagsSection(job),
      likesSection(job, arr(data.likes)),
      unknownSection(arr(data.unknown_fields)),
      rawSection(data.raw));
  }

  function imagesBlock(images, job) {
    const present = images.filter((r) => r && r.status === 'present' && safeFileUrl(r.url));
    const prompt = str(job.prompt);
    const wrap = h('div', {class: ['shots', images.length === 1 ? 'shots--single' : 'shots--grid']});
    if (!images.length) {
      const ph = placeholder(job.video ? 'unsupported' : 'not_exported');
      ph.classList.add('shots__empty');
      wrap.append(ph, h('p', {class: 'muted small'}, job.video ? 'Video media is not exported.' : 'This job has no image files in the export.'));
      return wrap;
    }
    images.forEach((ref, i) => {
      const r = obj(ref);
      const n = isNum(r.index) ? r.index : i;
      const label = `Image ${n + 1}`;
      const pIndex = present.indexOf(ref);
      const cell = h('div', {class: 'shot'});
      if (isNum(r.width) && isNum(r.height) && r.width > 0 && r.height > 0) cell.style.setProperty('--ar', `${r.width} / ${r.height}`);
      else if (isNum(job.width) && isNum(job.height)) cell.style.setProperty('--ar', `${job.width} / ${job.height}`);
      if (pIndex >= 0) {
        const button = h('button', {type: 'button', class: 'shot__btn', 'aria-label': `Open ${label.toLowerCase()} of ${images.length} at full size`},
          imageEl(r, {mode: 'responsive', alt: `${label} of ${images.length}`, eager: true,
            sizes: images.length === 1 ? '(min-width: 1100px) 720px, 100vw' : '(min-width: 1100px) 360px, 50vw'}),
          h('span', {class: 'shot__zoom', 'aria-hidden': 'true'}, icon('maximize', {size: 15})));
        button.addEventListener('click', () => openLightbox(present.map((p) => ({
          url: p.url, thumb_url: p.thumb_url, width: p.width, height: p.height, file: p.file,
          label: `Image ${(isNum(p.index) ? p.index : images.indexOf(p)) + 1}`,
        })), pIndex, {opener: button, caption: prompt}));
        cell.append(button);
      } else {
        cell.append(placeholder(r.status, {file: str(r.file)}));
      }
      cell.append(h('span', {class: 'shot__index', 'aria-hidden': 'true'}, String(n + 1)));
      wrap.append(cell);
    });
    return wrap;
  }

  function filesBlock(images) {
    if (!images.length) return null;
    const rows = images.map((ref, i) => {
      const r = obj(ref);
      const status = imageStatusInfo(r.status);
      return [
        String((isNum(r.index) ? r.index : i) + 1),
        h('span', {class: 'stack'},
          badge(status.short, {tone: r.status === 'present' ? 'ok' : r.status === 'not_exported' ? 'muted' : 'warn', title: status.label}),
          r.reason ? h('span', {class: 'note'}, str(r.reason)) : null),
        r.file ? h('span', {class: 'mono break'}, str(r.file)) : h('span', {class: 'muted'}, '—'),
        isNum(r.width) && isNum(r.height) ? `${fmtInt(r.width)} × ${fmtInt(r.height)}` : '—',
        r.source_url ? h('span', {class: 'url-cell'}, h('span', {class: 'mono break url-cell__text'}, str(r.source_url)),
          copyButton(str(r.source_url), {what: 'source URL', iconOnly: true})) : h('span', {class: 'muted'}, '—'),
      ];
    });
    return h('details', {class: 'files'},
      h('summary', null, icon('chevron-right', {size: 14, cls: 'raw__chev'}), `Files and source URLs (${fmtInt(images.length)})`),
      h('p', {class: 'section__intro'}, 'Source URLs are shown as text only; the viewer never loads them.'),
      dataTable([{label: '#', cls: 'num'}, {label: 'Status'}, {label: 'File'}, {label: 'Size', cls: 'nowrap'}, {label: 'Source URL'}], rows,
        {caption: 'Image files and source URLs'}));
  }

  function commandBlock(job) {
    const command = str(job.full_command);
    return h('div', {class: 'panel panel--tight'},
      h('h2', {class: 'panel__title'}, icon('terminal', {size: 15}), 'Full command'),
      command
        ? h('div', {class: 'code'}, h('pre', {class: 'code__pre code__pre--wrap'}, command),
          h('div', {class: 'code__actions'}, copyButton(command, {what: 'command'})))
        : h('p', {class: 'muted'}, 'No command recorded.'));
  }

  function detailsBlock(job, images, data) {
    const v = obj(job.version);
    const present = images.filter((r) => r && r.status === 'present').length;
    const lineage = obj(data.lineage);
    const parent = obj(lineage.parent);
    const parentId = str(parent.id || job.parent_id);
    const parentStatus = parent.status || job.parent_status;
    let parentValue = null;
    if (parentId && parentStatus === 'in_export') parentValue = jobLink(exportId, parentId);
    else if (parentId) parentValue = h('span', {class: 'stack'}, h('span', {class: 'mono break'}, parentId), badge('Not in this export', {tone: 'warn', iconName: 'unlink'}));
    const parentIndex = isNum(parent.index) ? parent.index : isNum(job.parent_index) ? job.parent_index : null;
    const versionSource = v.source === 'job_type' ? 'parsed from job_type' : v.source === 'flag' ? 'from the command flags' : 'not recorded';
    return h('div', {class: 'panel panel--tight'},
      h('h2', {class: 'panel__title'}, icon('info', {size: 15}), 'Details'),
      kvList([
        ['Created', timeEl(job.created_at)],
        ['Kind', h('span', {class: 'stack'}, kindLabel(job.kind),
          h('span', {class: 'note'}, job.kind_source === 'viewer'
            ? 'Derived by the viewer with the exporter’s rule; the export did not record a kind.'
            : 'Set by the exporter from keywords in job type and event type.'))],
        ['Version', h('span', {class: 'stack'}, str(v.label) || 'Unknown', h('span', {class: 'note'}, versionSource))],
        ['Dimensions', isNum(job.width) && isNum(job.height) ? `${fmtInt(job.width)} × ${fmtInt(job.height)} px` : null],
        ['Images', images.length ? `${fmtInt(present)} of ${fmtInt(images.length)} present` : 'None exported'],
        ['Job type', job.job_type ? h('span', {class: 'mono'}, str(job.job_type)) : null],
        ['Event type', job.event_type ? h('span', {class: 'mono'}, str(job.event_type)) : null],
        ['Parent', parentValue],
        parentId ? ['From image', parentIndex !== null ? `Image ${parentIndex + 1} of the parent (index ${parentIndex})` : 'Not recorded'] : null,
        job.parent_grid !== undefined && job.parent_grid !== null && !isNum(job.parent_grid)
          ? ['parent_grid', h('span', {class: 'mono break'}, JSON.stringify(job.parent_grid)), 'Unrecognised parent_grid structure, kept as recorded'] : null,
        job.video ? ['Video', h('span', {class: 'is-warn'}, 'Video job; the exporter does not export video media.')] : null,
        ['Job ID', idValue(job.job_id, {what: 'job id'})],
      ]));
  }

  function relationsBlock(data, job) {
    const boards = arr(data.moodboards).length ? arr(data.moodboards) : arr(job.moodboards);
    const profiles = arr(data.profiles).length ? arr(data.profiles) : arr(job.profiles);
    const unmatched = arr(job.codes).filter((c) => c && c.resolved === false);
    const panel = h('div', {class: 'panel panel--tight'},
      h('h2', {class: 'panel__title'}, icon('link', {size: 15}), 'Made with'));
    if (!boards.length && !profiles.length && !unmatched.length) {
      panel.append(h('p', {class: 'muted'}, 'Not linked to any exported moodboard or profile.'));
      return panel;
    }
    const list = h('ul', {class: 'relations'});
    for (const b of boards) {
      const x = obj(b);
      list.append(h('li', {class: 'relation'},
        h('span', {class: 'relation__kind'}, icon('board', {size: 14}), 'Moodboard'),
        h('a', {class: 'relation__name', href: hrefs.moodboard(exportId, x.id)}, str(x.name) || str(x.id)),
        linkChip(x.link),
        x.evidence ? h('p', {class: 'relation__evidence'}, str(x.evidence)) : null));
    }
    for (const p of profiles) {
      const x = obj(p);
      list.append(h('li', {class: 'relation'},
        h('span', {class: 'relation__kind'}, icon('user', {size: 14}), 'Profile'),
        h('a', {class: 'relation__name', href: hrefs.gallery(exportId, {profile: x.id}), title: 'Show jobs that used this profile'}, str(x.name) || str(x.id)),
        linkChip(x.link),
        x.evidence ? h('p', {class: 'relation__evidence'}, str(x.evidence)) : null));
    }
    for (const c of unmatched) {
      list.append(h('li', {class: 'relation'},
        h('span', {class: 'relation__kind'}, icon('hash', {size: 14}), 'Code'),
        h('a', {class: 'relation__name mono', href: hrefs.gallery(exportId, {profile: `code:${str(c.code)}`}), title: 'Show jobs with this code'}, str(c.code)),
        linkChip('unresolved'),
        h('p', {class: 'relation__evidence'}, `A ${str(c.type) || 'personalization'} code that matches no exported moodboard or profile.`)));
    }
    panel.append(list);
    return panel;
  }

  function lineageSection(lineage, job) {
    const sec = section('Lineage', {iconName: 'branch'});
    const ancestors = arr(lineage.ancestors);
    const parent = obj(lineage.parent);
    const children = arr(lineage.children);
    const parentId = str(parent.id || job.parent_id);
    const parentStatus = parent.status || job.parent_status || (parentId ? 'missing' : 'none');
    const parentIndex = isNum(parent.index) ? parent.index : isNum(job.parent_index) ? job.parent_index : null;

    if (lineage.cycle) {
      sec.body.append(callout('warn', h('p', null, 'The parent chain loops back on itself, so the ancestor list stops at the repeated job. The export records contradictory parent ids.')));
    }
    if (ancestors.length || parentStatus !== 'none') {
      const trail = h('ol', {class: 'trail', 'aria-label': 'Ancestors, root first'});
      if (parentStatus === 'missing' && !ancestors.length) {
        trail.append(h('li', {class: 'trail__item'}, h('span', {class: 'mini mini--gap'},
          h('span', {class: 'mini__thumb'}, icon('unlink', {size: 18})),
          h('span', {class: 'mini__text'}, h('span', {class: 'mini__title'}, 'Parent not exported'),
            h('span', {class: 'mini__meta mono'}, parentId)))));
      }
      ancestors.forEach((a, i) => {
        const note = i === ancestors.length - 1 ? 'Parent' : i === 0 ? 'Root' : '';
        trail.append(h('li', {class: 'trail__item'}, miniJob(a, {exportId, note})));
      });
      trail.append(h('li', {class: 'trail__item'}, miniJob(job, {exportId, current: true,
        note: parentIndex !== null && parentStatus === 'in_export' ? `This job · from image ${parentIndex + 1}` : 'This job'})));
      const wrap = h('div', {class: 'trail-wrap'}, trail);
      sec.body.append(wrap);
      // Long chains scroll sideways on wide screens; start at the current job.
      requestAnimationFrame(() => {
        wrap.scrollLeft = wrap.scrollWidth;
      });
    }

    const facts = h('div', {class: 'lineage-facts'});
    if (parentStatus === 'none' && !ancestors.length) {
      facts.append(h('p', null, icon('branch', {size: 15}), 'A root job: it has no parent.'));
    } else if (parentStatus === 'in_export') {
      facts.append(h('p', null, icon('corner-up', {size: 15}),
        parentIndex !== null ? `Made from image ${parentIndex + 1} of its parent (index ${parentIndex}).` : 'Made from its parent; the image index was not recorded.'));
    } else if (parentStatus === 'missing') {
      const note = h('span', {class: 'gap-note'});
      facts.append(callout('warn', h('p', null, 'The parent job ', h('span', {class: 'mono break'}, parentId),
        ' is not in this export, so the chain stops here.', parent.reason ? ` ${str(parent.reason)}` : '', note)));
      crossCheckGap(note, parent);
    }
    const descendants = isNum(lineage.descendant_count) ? lineage.descendant_count : null;
    if (descendants !== null && descendants > children.length) {
      facts.append(h('p', null, icon('corner-down', {size: 15}), `${plural(descendants, 'descendant')} in total across later generations.`));
    }
    sec.body.append(facts);

    const childHead = h('h3', {class: 'subhead'}, `Children (${fmtInt(children.length)})`);
    if (!children.length) {
      sec.body.append(childHead, h('p', {class: 'muted'}, 'No job in this export was made from this one.'));
    } else {
      sec.body.append(childHead, h('ul', {class: 'mini-list'}, children.map((c) => {
        const x = obj(c);
        const note = isNum(x.parent_index) ? `From image ${x.parent_index + 1}` : 'Source image not recorded';
        return h('li', null, miniJob(x, {exportId, note}));
      })));
    }
    return sec.el;
  }

  /** Say whether export_meta.json also reports this parent gap (from the cached overview). */
  function crossCheckGap(note, parent) {
    const direct = [parent.reported_by_exporter, parent.in_meta, parent.listed_in_meta].find((v) => typeof v === 'boolean');
    const say = (listed) => {
      note.textContent = listed
        ? ' export_meta.json lists this gap too.'
        : ' export_meta.json does not list this gap.';
    };
    if (direct !== undefined) {
      say(direct);
      return;
    }
    loadOverview(exportId, {signal: ctx.signal}).then((data) => {
      if (!ctx.isActive()) return;
      const d = obj(data);
      const observed = arr(obj(d.unresolved).parent_gaps).find((g) => g && g.job_id === jobId);
      if (observed && typeof observed.reported_by_exporter === 'boolean') say(observed.reported_by_exporter);
      else if (arr(d.parent_gaps).some((g) => g && g.job_id === jobId)) say(true);
    }).catch(() => {});
  }

  function referencesSection(references) {
    const sec = section('Personalization references', {count: references.length, iconName: 'link',
      intro: 'Profile and moodboard references recorded on this job, and what each one resolves to in this export.'});
    if (!references.length) {
      sec.body.append(h('p', {class: 'muted'}, 'This job records no personalization references.'));
      return sec.el;
    }
    sec.body.append(dataTable([{label: 'Type'}, {label: 'Code'}, {label: 'From'}, {label: 'Reference id'}, {label: 'Link'}, {label: 'Resolves to'}, {label: 'Reason'}],
      references.map((ref) => {
        const r = obj(ref);
        const target = r.target && typeof r.target === 'object' ? r.target : null;
        let targetEl = h('span', {class: 'muted'}, 'Nothing in this export');
        if (target && target.kind === 'moodboard') {
          targetEl = h('a', {href: hrefs.moodboard(exportId, target.id)}, icon('board', {size: 14}), ' ', str(target.name) || str(target.id));
        } else if (target && target.kind === 'profile') {
          targetEl = h('a', {href: hrefs.gallery(exportId, {profile: target.id})}, icon('user', {size: 14}), ' ', str(target.name) || str(target.id));
        }
        return [
          str(r.type) || '—',
          r.code ? h('span', {class: 'mono'}, str(r.code)) : h('span', {class: 'muted'}, '—'),
          r.source === 'flag' ? h('span', {class: 'nowrap'}, '--profile flag') : r.source === 'reference' ? 'Reference' : str(r.source) || '—',
          r.id ? h('span', {class: 'mono break'}, str(r.id)) : h('span', {class: 'muted'}, 'none'),
          linkChip(r.link),
          targetEl,
          r.reason ? str(r.reason) : h('span', {class: 'muted'}, linkInfo(r.link).help || '—'),
        ];
      }), {caption: 'Personalization references'}));
    return sec.el;
  }

  function flagsSection(job) {
    const flags = obj(job.flags);
    const unknown = obj(job.unknown_flags);
    const rows = [];
    const keys = [...FLAG_ORDER.filter((k) => k in flags), ...Object.keys(flags).filter((k) => !FLAG_ORDER.includes(k))];
    for (const key of keys) {
      const value = flags[key];
      if (value === null || value === undefined || value === false || (Array.isArray(value) && !value.length)) continue;
      rows.push([h('span', {class: 'mono'}, `--${key}`), FLAG_HELP[key] || h('span', {class: 'muted'}, 'Flag'), flagValue(value)]);
    }
    for (const [key, values] of Object.entries(unknown)) {
      rows.push({cls: 'is-unknown', cells: [h('span', {class: 'mono'}, `--${key}`), badge('Unknown flag', {tone: 'muted'}), flagValue(values)]});
    }
    if (job.profile_default === true) {
      rows.push([h('span', {class: 'mono'}, '--profile'), FLAG_HELP.profile,
        h('span', null, 'Given without a code: the account’s default profile.')]);
    }
    const intro = job.flags_source === 'viewer'
      ? 'The export did not record parsed flags, so the viewer parsed them from the full command (same rules as the exporter). Unknown flags are kept as written.'
      : job.flags_source === 'none'
        ? 'The export recorded neither parsed flags nor a command to parse.'
        : 'Parsed by the exporter from the full command. Unknown flags are kept as written.';
    const sec = section('Parsed flags', {count: rows.length, iconName: 'sliders', intro});
    if (!rows.length) sec.body.append(h('p', {class: 'muted'}, 'The command has no flags.'));
    else sec.body.append(dataTable([{label: 'Flag'}, {label: 'Meaning'}, {label: 'Value'}], rows, {caption: 'Parsed flags', cls: 'table--flags'}));
    return sec.el;
  }

  function likesSection(job, likes) {
    const imageLikes = arr(job.image_likes);
    if (!imageLikes.length && !likes.length) return null;
    const sec = section('Likes', {iconName: 'heart'});
    if (imageLikes.length) {
      sec.body.append(h('div', {class: 'panel panel--tight'}, kvList([['image_likes', likedText(imageLikes)]])));
    }
    if (likes.length) sec.body.append(rawBlock(likes, {summary: 'likes.jsonl rows for this job'}));
    return sec.el;
  }

  function unknownSection(fields) {
    if (!fields.length) return null;
    const sec = section('Unknown fields', {count: fields.length, iconName: 'help',
      intro: 'Fields this viewer does not recognise. Their values are in the raw record below.'});
    sec.body.append(h('ul', {class: 'tag-list'}, fields.map((f) => h('li', {class: 'tag mono'}, str(f)))));
    return sec.el;
  }

  function rawSection(raw) {
    const sec = section('Raw record', {iconName: 'file', intro: 'The original jobs.jsonl row, unchanged.'});
    sec.body.append(rawBlock(raw === undefined ? null : raw, {summary: 'Show jobs.jsonl row'}));
    return sec.el;
  }

  // --- Previous / next within the gallery list ---
  async function loadNeighbours() {
    if (!inGallery) return;
    const params = new URLSearchParams(from);
    params.delete('offset');
    params.delete('limit');
    const offset = Math.max(0, pos - 1);
    params.set('offset', String(offset));
    params.set('limit', String(pos === 0 ? 2 : 3));
    try {
      const data = await getJSON(paths.jobs(exportId, params), {signal: ctx.signal});
      if (!ctx.isActive()) return;
      const list = arr(data.items);
      const idx = list.findIndex((j) => j && j.job_id === jobId);
      if (idx < 0) return;
      const actual = offset + idx;
      const prev = idx > 0 ? list[idx - 1] : null;
      const next = list[idx + 1] || null;
      prevHref = prev ? hrefs.job(exportId, prev.job_id, {in: from, pos: actual - 1}) : null;
      nextHref = next ? hrefs.job(exportId, next.job_id, {in: from, pos: actual + 1}) : null;
      const step = (href, label, iconName, rel) => (href
        ? h('a', {class: 'btn btn--sm', href, rel, 'aria-label': `${label} job`, title: `${label} job (${rel === 'prev' ? '←' : '→'})`}, rel === 'prev' ? icon(iconName, {size: 15}) : null, h('span', {class: 'job-nav__label'}, label), rel === 'next' ? icon(iconName, {size: 15}) : null)
        : h('span', {class: 'btn btn--sm is-disabled', 'aria-disabled': 'true'}, rel === 'prev' ? icon(iconName, {size: 15}) : null, h('span', {class: 'job-nav__label'}, label), rel === 'next' ? icon(iconName, {size: 15}) : null));
      fill(neighbours,
        step(prevHref, 'Previous', 'chevron-left', 'prev'),
        h('span', {class: 'job-nav__pos'}, isNum(data.total) ? `${fmtInt(actual + 1)} of ${fmtInt(data.total)}` : `#${fmtInt(actual + 1)}`),
        step(nextHref, 'Next', 'chevron-right', 'next'));
    } catch (err) {
      if (!isAbort(err)) fill(neighbours);
    }
  }

  const onKey = (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (lightboxOpen() || isTypingTarget(event.target)) return;
    if (event.target instanceof Element && event.target.closest('pre, .table-wrap, .trail-wrap')) return;
    if (event.key === 'ArrowLeft' && prevHref) {
      event.preventDefault();
      location.hash = prevHref;
    } else if (event.key === 'ArrowRight' && nextHref) {
      event.preventDefault();
      location.hash = nextHref;
    }
  };
  document.addEventListener('keydown', onKey);

  load();
  loadNeighbours();
  return () => document.removeEventListener('keydown', onKey);
}

function flagValue(value) {
  if (value === true) return h('span', {class: 'muted'}, 'on (no value)');
  if (Array.isArray(value)) {
    if (!value.length) return h('span', {class: 'muted'}, '—');
    return h('span', {class: 'flag-values'}, value.map((v) => h('span', {class: 'tag mono'}, v === true ? '(no value)' : str(v))));
  }
  if (value && typeof value === 'object') return renderValue(value);
  return h('span', {class: 'tag mono'}, str(value));
}

// The meaning of image_likes entries is not documented by the exporter, so the
// values are shown exactly as recorded rather than mapped onto images.
function likedText(imageLikes) {
  return h('span', {class: 'stack'}, renderValue(imageLikes), h('span', {class: 'note'}, 'Values as recorded by the exporter.'));
}
