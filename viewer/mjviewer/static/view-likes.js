// Likes: liked own jobs with their image_likes, or an explicit empty state.

import {h, fill, icon} from './dom.js';
import {getJSON, paths, isAbort} from './api.js';
import {arr, str, obj, fmtInt} from './format.js';
import {pageHead, jobCard, gridItem, renderValue, callout, emptyState, errorState, loadingState} from './ui.js';

export function renderLikes(root, route, ctx) {
  const {exportId} = route;
  ctx.setTitle('Likes');
  const head = pageHead({
    eyebrow: 'Likes',
    title: 'Likes',
    sub: h('p', null, 'Likes recorded on your own exported jobs (likes.jsonl).'),
  });
  const body = h('div', null, loadingState('Loading likes…'));
  root.append(h('div', {class: 'page'}, head.el, body));

  async function load() {
    fill(body, loadingState('Loading likes…'));
    try {
      const data = obj(await getJSON(paths.likes(exportId), {signal: ctx.signal}));
      if (!ctx.isActive()) return;
      const items = arr(data.items).map(obj);
      const note = data.note ? callout('info', h('p', null, str(data.note))) : null;
      head.heading.textContent = 'Likes';
      head.heading.append(' ', h('span', {class: 'count count--title'}, fmtInt(items.length)));
      if (!items.length) {
        fill(body, emptyState({
          iconName: 'heart',
          title: 'No likes in this export',
          body: [
            h('p', null, 'likes.jsonl is empty or absent. The exporter records likes only as image_likes on your own exported jobs; it does not export work by other creators that you liked.'),
            note,
          ],
        }));
        return;
      }
      fill(body, note, h('ul', {class: 'grid', 'aria-label': 'Liked jobs'}, items.map((item) => {
        const footer = h('div', {class: 'card__likes'},
          h('span', {class: 'card__likes-label'}, 'image_likes'),
          renderValue(item.image_likes),
          item.source === 'jobs.jsonl' ? h('span', {class: 'card__likes-source'}, 'from the job row (likes.jsonl lacks it)') : null);
        const job = item.job && typeof item.job === 'object' ? item.job : null;
        if (!job) return missingJobItem(item.job_id, footer);
        return gridItem(job, jobCard(job, {exportId, footer}));
      })));
    } catch (err) {
      if (isAbort(err)) return;
      fill(body, errorState(err, {retry: load, title: 'Could not load likes'}));
    }
  }
  load();
}

/** A like whose job is not in the export: shown, never dropped. */
function missingJobItem(jobId, footer) {
  const card = h('article', {class: 'card card--orphan'},
    h('div', {class: 'media media--single'},
      h('div', {class: 'ph ph--missing', role: 'img', 'aria-label': 'The liked job is not in this export'},
        icon('unlink', {size: 22}), h('span', {class: 'ph__label'}, 'Not in this export'))),
    h('div', {class: 'card__body'},
      h('p', {class: 'card__orphan-title'}, 'Job not in this export'),
      h('p', {class: 'mono small break'}, str(jobId) || 'No job id recorded'),
      footer));
  return h('li', {class: 'grid__item'}, card);
}
