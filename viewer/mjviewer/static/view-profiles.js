// Profiles: custom and global personalization profiles, their codes and
// aggregate counts, the jobs linked to each, and codes that match nothing.

import {h, icon, fill} from './dom.js';
import {hrefs} from './router.js';
import {getJSON, paths, isAbort} from './api.js';
import {arr, str, obj, isNum, fmtInt, linkInfo, plural, profileVersionLabel, recordedDate} from './format.js';
import {
  pageHead, section, linkChip, badge, timeEl, copyButton, idValue, imageEl, placeholder, kvList, rawBlock,
  callout, emptyState, errorState, loadingState,
} from './ui.js';

const STRIP_LIMIT = 8;

export function renderProfiles(root, route, ctx) {
  const {exportId} = route;
  ctx.setTitle('Profiles');
  const head = pageHead({
    eyebrow: 'Personalization',
    title: 'Profiles',
    sub: h('p', null, 'Personalization profiles returned for this account, with the exported jobs that used them.'),
  });
  const body = h('div', null, loadingState('Loading profiles…'));
  root.append(h('div', {class: 'page'}, head.el, body));

  async function load() {
    fill(body, loadingState('Loading profiles…'));
    try {
      const data = obj(await getJSON(paths.profiles(exportId), {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      draw(data);
    } catch (err) {
      if (isAbort(err)) return;
      fill(body, errorState(err, {retry: load, title: 'Could not load profiles'}));
    }
  }

  function draw(data) {
    const items = arr(data.items).map(obj);
    const unmatched = arr(data.unmatched_codes).map(obj);
    const custom = items.filter((p) => p.type === 'profile');
    const global = items.filter((p) => p.type === 'global');
    const other = items.filter((p) => p.type !== 'profile' && p.type !== 'global');
    const notes = arr(data.notes).filter(Boolean);
    fill(body,
      callout('info', h('p', null, 'An export holds profile ids, codes and aggregate rating counts only. ',
        h('strong', null, 'Individual ratings and trained personalization weights are not part of an export.'))),
      notes.length ? callout('neutral', notes.length === 1 ? h('p', null, str(notes[0])) : h('ul', {class: 'notes'}, notes.map((n) => h('li', null, str(n))))) : null,
      !items.length ? emptyState({
        iconName: 'user',
        title: 'No profiles in this export',
        body: h('p', null, 'profiles.jsonl is empty or absent. A small-test export includes at most one profile.'),
      }) : null,
      group('Custom profiles', custom, 'Profiles you trained by rating images. Codes come from the profile record or from job references.', 'custom'),
      group('Global profiles', global, 'Aggregate profiles per model version, synthesized by the exporter from visible counts. They rarely carry codes.', 'global'),
      group('Other profile types', other, 'Profiles with a type this viewer does not recognise; shown as recorded.', 'other'),
      unmatchedSection(unmatched));
  }

  function group(title, list, intro, variant) {
    if (!list.length) return null;
    const sec = section(title, {count: list.length, intro, iconName: variant === 'global' ? 'layers' : 'user'});
    sec.body.append(h('ul', {class: ['pgrid', `pgrid--${variant}`]}, list.map((p) => h('li', null, profileCard(p, variant)))));
    return sec.el;
  }

  function profileCard(p, variant) {
    const jobs = arr(p.jobs).map(obj);
    const linkCounts = {};
    for (const j of jobs) linkCounts[str(j.link) || 'unknown'] = (linkCounts[str(j.link) || 'unknown'] || 0) + 1;
    const codes = arr(p.codes).map(str).filter(Boolean);
    const typeLabel = p.type === 'profile' ? 'Custom' : p.type === 'global' ? 'Global' : str(p.type) || 'Unknown type';
    const versionLabel = profileVersionLabel(p);
    const unknown = arr(p.unknown_fields);
    return h('article', {class: ['pcard', `pcard--${variant}`]},
      h('div', {class: 'pcard__head'},
        h('h3', {class: 'pcard__title'}, str(p.name) || 'Unnamed profile'),
        h('div', {class: 'tag-row'},
          badge(typeLabel, {tone: p.type === 'profile' ? 'accent' : 'muted'}),
          versionLabel ? badge(versionLabel, {tone: 'version'}) : null,
          p.service ? badge(str(p.service), {tone: 'muted', title: 'Service'}) : null)),
      kvList([
        ['Codes', codes.length ? h('span', {class: 'tag-row'}, codes.map((c) => h('span', {class: 'code-tag'},
          h('span', {class: 'tag mono'}, c), copyButton(c, {what: `code ${c}`, iconOnly: true})))) : h('span', {class: 'muted'}, 'No code recorded')],
        ['Ratings', isNum(p.ratings_count) ? h('span', {class: 'num-strong'}, fmtInt(p.ratings_count)) : null, 'Aggregate count only'],
        ['Created', p.created_at !== undefined && p.created_at !== null ? timeEl(recordedDate(p.created_at, p.created_at_raw)) : h('span', {class: 'muted'}, 'Not recorded')],
        ['Profile ID', idValue(p.id, {what: 'profile id'})],
      ], {cls: 'kv--compact'}),
      h('div', {class: 'pcard__jobs'},
        h('div', {class: 'pcard__jobs-head'},
          h('p', {class: 'pcard__jobs-title'}, jobs.length ? `Used by ${plural(jobs.length, 'job')}` : 'No exported job references this profile'),
          jobs.length ? h('span', {class: 'tag-row'}, Object.entries(linkCounts).map(([k, n]) => h('span', {
            class: ['link-chip', `link-chip--${linkInfo(k).style}`], title: linkInfo(k).help}, `${fmtInt(n)} ${linkInfo(k).short}`))) : null,
          jobs.length ? h('a', {class: 'btn btn--sm', href: hrefs.gallery(exportId, {profile: p.id})}, icon('grid', {size: 14}), 'Show in gallery') : null),
        jobs.length ? jobStrip(p.id, new Map(jobs.map((j) => [str(j.job_id), j])), jobs.length) : null),
      unknown.length ? h('p', {class: 'pcard__unknown'}, 'Unknown fields: ', unknown.map((f, i) => [i ? ', ' : '', h('span', {class: 'mono'}, str(f))])) : null,
      p.raw !== undefined ? rawBlock(p.raw, {summary: 'Raw record'}) : null);
  }

  function unmatchedSection(unmatched) {
    const sec = section('Unmatched codes', {count: unmatched.length, iconName: 'hash',
      intro: 'Personalization codes on exported jobs that match no exported profile or moodboard, for example a profile outside a small-test export. They are listed, not guessed.'});
    if (!unmatched.length) {
      sec.body.append(h('p', {class: 'muted'}, 'Every personalization code on an exported job matches an exported profile or moodboard.'));
      return sec.el;
    }
    sec.body.append(h('ul', {class: 'pgrid pgrid--codes'}, unmatched.map((u) => {
      const ids = arr(u.job_ids).map(str);
      const code = str(u.code);
      return h('li', null, h('article', {class: 'pcard pcard--code'},
        h('div', {class: 'pcard__head'},
          h('h3', {class: 'pcard__title mono'}, code || '(empty code)'),
          h('div', {class: 'tag-row'}, badge(str(u.type) || 'unknown type', {tone: 'muted', title: 'Reference type'}), linkChip('unresolved'),
            code ? copyButton(code, {what: `code ${code}`, iconOnly: true}) : null)),
        h('div', {class: 'pcard__jobs'},
          h('div', {class: 'pcard__jobs-head'},
            h('p', {class: 'pcard__jobs-title'}, `On ${plural(ids.length, 'job')}`),
            ids.length ? h('a', {class: 'btn btn--sm', href: hrefs.gallery(exportId, {profile: `code:${code}`})}, icon('grid', {size: 14}), 'Show in gallery') : null),
          ids.length ? jobStrip(`code:${code}`, null, ids.length) : null)));
    })));
    return sec.el;
  }

  /** A row of small thumbnails for the first jobs matching a profile filter. */
  function jobStrip(profileParam, linkMap, count) {
    const strip = h('ul', {class: 'strip', 'aria-label': 'Jobs'}, Array.from({length: Math.min(count, STRIP_LIMIT)}, () => h('li', {class: 'strip__item strip__item--skeleton', 'aria-hidden': 'true'})));
    getJSON(paths.jobs(exportId, {profile: profileParam, limit: STRIP_LIMIT, offset: 0}), {signal: ctx.signal}).then((data) => {
      if (!ctx.isActive()) return;
      const items = arr(obj(data).items).map(obj);
      if (!items.length) {
        fill(strip, h('li', {class: 'muted small'}, 'No matching jobs were returned.'));
        return;
      }
      fill(strip, items.map((job) => {
        const first = arr(job.images).find((r) => r && r.status === 'present') || arr(job.images)[0];
        const relation = linkMap ? linkMap.get(str(job.job_id)) : null;
        const prompt = str(job.prompt).trim();
        return h('li', {class: 'strip__item'},
          h('a', {class: 'strip__link', href: hrefs.job(exportId, job.job_id), title: prompt || 'No prompt text'},
            h('span', {class: 'strip__thumb'}, first ? imageEl(first, {mode: 'thumb', compact: true}) : placeholder('not_exported', {compact: true})),
            h('span', {class: 'sr-only'}, prompt || 'Job without prompt text'),
            relation && relation.link !== 'explicit' ? h('span', {class: 'strip__tag'}, linkInfo(relation.link).short) : null));
      }), total(data, items.length));
    }).catch((err) => {
      if (isAbort(err)) return;
      fill(strip, h('li', {class: 'muted small'}, `Could not load jobs: ${str(err.message)}`));
    });
    return strip;
  }

  function total(data, shown) {
    const t = obj(data).total;
    return isNum(t) && t > shown ? h('li', {class: 'strip__more'}, `+${fmtInt(t - shown)}`) : null;
  }

  load();
}
