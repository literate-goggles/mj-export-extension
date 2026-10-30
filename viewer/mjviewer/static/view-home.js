// Exports home: opened exports, library entries, open-by-path and ZIP upload.

import {h, icon, fill} from './dom.js';
import {hrefs} from './router.js';
import {loadExports, postJSON, getJSON, paths, uploadZip, rememberSummaries, isAbort} from './api.js';
import {arr, str, obj, isNum, fmtInt, fmtBytes} from './format.js';
import {
  pageHead, section, statusBadge, supportBadge, badge, emptyState, errorState, loadingState, callout, timeEl, announce,
} from './ui.js';

export function renderHome(root, route, ctx) {
  ctx.setTitle('Exports');
  const head = pageHead({
    eyebrow: 'Local viewer',
    title: 'Exports',
    sub: h('p', null, 'Midjourney exports opened in this viewer. Files are read in place and never modified.'),
  });
  const listSlot = h('div', {class: 'slot'}, loadingState('Loading exports…'));
  const opened = section('Opened exports', {cls: 'section--first'});
  opened.body.append(listSlot);
  const librarySlot = h('div', {class: 'slot'});
  const library = section('Library', {intro: 'Exports found in the --library folder. Opening one reads it in place.'});
  library.body.append(librarySlot);
  library.el.hidden = true;

  const openArea = section('Open another export');
  const message = h('div', {class: 'open-message', 'aria-live': 'polite'});
  const limits = {maxBytes: null};
  const upload = uploadPanel(onOpened, message, limits);
  openArea.body.append(h('div', {class: 'open-grid'}, upload.panel, pathPanel(onOpened, message)), message);
  const failedSlot = h('div', {class: 'slot'});
  opened.body.prepend(failedSlot);

  root.append(h('div', {class: 'page'}, head.el, opened.el, openArea.el, library.el));

  // Keep a stray drop elsewhere on the page from navigating away from the viewer.
  const stopDrop = (event) => {
    if (event.dataTransfer && [...event.dataTransfer.types].includes('Files')) event.preventDefault();
  };
  window.addEventListener('dragover', stopDrop);
  window.addEventListener('drop', stopDrop);

  async function load(force = true) {
    fill(listSlot, loadingState('Loading exports…'));
    try {
      const data = await loadExports({force, signal: ctx.signal});
      if (!ctx.isActive()) return;
      renderLists(data);
    } catch (err) {
      if (isAbort(err)) return;
      fill(listSlot, errorState(err, {retry: () => load(true), title: 'Could not list exports'}));
    }
  }

  function renderLists(data) {
    const failed = arr(data.failed);
    fill(failedSlot, failed.length ? callout('err', h('p', null, `${failed.length === 1 ? 'One path or export root' : `${fmtInt(failed.length)} paths or export roots`} could not be opened:`),
      h('ul', {class: 'notes'}, failed.map((f) => {
        const x = obj(f);
        // root: an export root inside the path that failed while the others opened ('' is the top of it).
        const where = typeof x.root === 'string' ? ` › ${x.root || '(top)'}` : '';
        return h('li', null, h('span', {class: 'mono break'}, (str(x.path) || '(unknown path)') + where), ': ', str(x.message) || str(x.code) || 'unreadable');
      }))) : null);
    const exports = arr(data.exports);
    if (!exports.length) {
      fill(listSlot, emptyState({
        iconName: 'archive',
        title: 'No exports opened yet',
        body: [
          h('p', null, 'Upload an export ZIP below, enter a path on this computer, or start the viewer with one:'),
          h('pre', {class: 'code inline-code'}, 'cd viewer && python3 -m mjviewer /path/to/mj_export_….zip'),
        ],
      }));
    } else {
      fill(listSlot, h('ul', {class: 'xgrid', 'aria-label': 'Opened exports'}, exports.map((e) => h('li', null, exportCard(e)))));
    }
    const entries = arr(data.library);
    library.el.hidden = entries.length === 0;
    fill(librarySlot, entries.length ? libraryList(entries, exports, onOpened, message) : null);
  }

  function onOpened(result, {source}) {
    const list = arr(result && result.exports);
    rememberSummaries(list);
    if (list.length === 1) {
      announce(`Opened ${str(list[0].label) || 'export'}`);
      location.hash = hrefs.overview(list[0].id);
      return;
    }
    fill(message, callout(list.length ? 'ok' : 'warn', h('p', null, list.length
      ? `Opened ${fmtInt(list.length)} exports from ${source}.`
      : `No export was found in ${source}.`)));
    load(true);
  }

  load(false);
  getJSON(paths.health(), {signal: ctx.signal}).then((health) => {
    if (typeof health.max_upload_bytes === 'number' && health.max_upload_bytes > 0) {
      limits.maxBytes = health.max_upload_bytes;
      upload.setLimit(health.max_upload_bytes);
    }
  }).catch(() => {});
  return () => {
    window.removeEventListener('dragover', stopDrop);
    window.removeEventListener('drop', stopDrop);
  };
}

function exportCard(summary) {
  const s = obj(summary);
  const counts = obj(s.counts);
  const source = obj(s.source);
  const label = str(s.label) || str(s.id);
  const warnings = isNum(s.warnings) ? s.warnings : 0;
  const errors = isNum(s.errors) ? s.errors : 0;
  const imagesText = isNum(counts.images_expected)
    ? `${fmtInt(counts.images_present)} / ${fmtInt(counts.images_expected)}`
    : fmtInt(counts.images_present);
  const sourceType = source.type === 'folder' ? 'Folder' : source.type === 'upload' ? 'Upload' : 'ZIP';
  return h('article', {class: 'xcard'},
    h('div', {class: 'xcard__top'}, statusBadge(s.status), supportBadge(s.version_support, s.extension_version)),
    h('h3', {class: 'xcard__title'}, h('a', {class: 'xcard__link', href: hrefs.overview(s.id)}, label)),
    h('p', {class: 'xcard__source', title: str(source.path)},
      icon(source.type === 'folder' ? 'folder' : 'archive', {size: 14}),
      h('span', {class: 'xcard__source-type'}, sourceType),
      h('span', {class: 'mono truncate'}, str(source.name) || str(source.path) || '—'),
      s.root ? h('span', {class: 'mono truncate muted'}, `› ${str(s.root)}`) : null),
    h('p', {class: 'xcard__meta'},
      s.username ? h('span', {class: 'truncate'}, str(s.username)) : null,
      s.export_time ? h('span', null, 'Exported ', timeEl(s.export_time)) : h('span', {class: 'muted'}, 'Export time not recorded')),
    h('dl', {class: 'xcard__counts'},
      countItem('Jobs', fmtInt(counts.jobs)),
      countItem('Images', imagesText),
      countItem('Boards', fmtInt(counts.moodboards)),
      countItem('Profiles', fmtInt(counts.profiles)),
      countItem('Likes', fmtInt(counts.likes))),
    warnings || errors
      ? h('p', {class: 'xcard__issues'},
        errors ? badge(`${fmtInt(errors)} ${errors === 1 ? 'error' : 'errors'}`, {tone: 'err', iconName: 'alert-circle'}) : null,
        warnings ? badge(`${fmtInt(warnings)} ${warnings === 1 ? 'warning' : 'warnings'}`, {tone: 'warn', iconName: 'alert-triangle'}) : null)
      : h('p', {class: 'xcard__issues'}, badge('No warnings', {tone: 'ok', iconName: 'check-circle'})));
}

function countItem(label, value) {
  return h('div', {class: 'xcard__count'}, h('dt', null, label), h('dd', null, value));
}

function libraryList(entries, exports, onOpened, message) {
  return h('ul', {class: 'library'}, entries.map((entry) => {
    const e = obj(entry);
    const match = exports.find((x) => obj(x.source).path === e.path);
    let action;
    if (match) {
      action = h('a', {class: 'btn btn--sm', href: hrefs.overview(match.id)}, 'View');
    } else {
      const btn = h('button', {type: 'button', class: 'btn btn--sm btn--primary'}, 'Open');
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = 'Opening…';
        try {
          const result = await postJSON(paths.open(), {path: e.path});
          onOpened(result, {source: str(e.name) || str(e.path)});
        } catch (err) {
          fill(message, callout('err', h('p', null, `Could not open ${str(e.name) || str(e.path)}: ${str(err.message)}`)));
        } finally {
          btn.disabled = false;
          btn.textContent = 'Open';
        }
      });
      action = e.opened ? h('span', {class: 'library__state'}, badge('Opened', {tone: 'ok'}), btn) : btn;
    }
    return h('li', {class: 'library__item'},
      h('span', {class: 'library__icon'}, icon(e.kind === 'folder' ? 'folder' : 'archive', {size: 18})),
      h('span', {class: 'library__text'},
        h('span', {class: 'library__name'}, str(e.name) || str(e.path)),
        h('span', {class: 'library__path mono'}, str(e.path))),
      action);
  }));
}

function uploadPanel(onOpened, message, limits) {
  const input = h('input', {type: 'file', accept: '.zip,application/zip', class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true'});
  const choose = h('button', {type: 'button', class: 'btn btn--primary'}, icon('upload', {size: 15}), 'Choose ZIP…');
  const progress = h('div', {class: 'upload', hidden: true});
  const zone = h('div', {class: 'dropzone'},
    h('span', {class: 'dropzone__icon'}, icon('archive', {size: 22})),
    h('div', {class: 'dropzone__text'},
      h('p', {class: 'dropzone__title'}, 'Drop an export ZIP here'),
      h('p', {class: 'dropzone__hint'}, 'The ZIP is copied into the viewer cache and read from there. The original file stays untouched.'),
      h('p', {class: 'dropzone__hint dropzone__limit', hidden: true})),
    h('div', {class: 'dropzone__actions'}, choose),
    input);
  const panel = h('div', {class: 'panel open-panel'},
    h('h3', {class: 'panel__title'}, icon('upload', {size: 16}), 'Upload a ZIP'),
    zone, progress);

  let active = null;
  choose.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) start(input.files[0]);
    input.value = '';
  });
  zone.addEventListener('dragenter', (event) => {
    event.preventDefault();
    zone.classList.add('is-over');
  });
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    zone.classList.add('is-over');
  });
  zone.addEventListener('dragleave', (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove('is-over');
  });
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.classList.remove('is-over');
    const files = event.dataTransfer ? [...event.dataTransfer.files] : [];
    if (files.length > 1) {
      fill(message, callout('warn', h('p', null, 'Drop one ZIP at a time.')));
      return;
    }
    if (files[0]) start(files[0]);
  });

  function start(file) {
    if (active) return;
    fill(message);
    if (!/\.zip$/i.test(file.name || '')) {
      fill(message, callout('err', h('p', null, `${str(file.name) || 'This file'} is not a .zip file. Choose the ZIP of an export folder.`)));
      return;
    }
    if (limits.maxBytes && file.size > limits.maxBytes) {
      fill(message, callout('err', h('p', null,
        `${str(file.name)} is ${fmtBytes(file.size)}, more than the ${fmtBytes(limits.maxBytes)} upload limit. `,
        'Open it by path instead (nothing is copied), or restart the viewer with a larger --max-upload-mb.')));
      return;
    }
    const name = h('span', {class: 'upload__name truncate'}, str(file.name));
    const pct = h('span', {class: 'upload__pct'}, '0%');
    const bar = h('div', {class: 'progress__bar'});
    const track = h('div', {class: 'progress', role: 'progressbar', 'aria-label': `Uploading ${str(file.name)}`,
      'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0'}, bar);
    const phase = h('p', {class: 'upload__phase'}, `Uploading ${fmtBytes(file.size)}…`);
    const cancel = h('button', {type: 'button', class: 'btn btn--sm'}, 'Cancel');
    fill(progress, h('div', {class: 'upload__row'}, icon('archive', {size: 15}), name, pct), track, h('div', {class: 'upload__row'}, phase, cancel));
    progress.hidden = false;
    choose.disabled = true;

    const job = uploadZip(file, {
      onProgress: (fraction, loaded) => {
        if (fraction === null) {
          track.classList.add('is-indeterminate');
          pct.textContent = fmtBytes(loaded);
          return;
        }
        const value = Math.round(fraction * 100);
        bar.style.setProperty('width', `${value}%`);
        track.setAttribute('aria-valuenow', String(value));
        pct.textContent = `${value}%`;
      },
      onSent: () => {
        bar.style.setProperty('width', '100%');
        pct.textContent = '100%';
        track.classList.add('is-indeterminate');
        phase.textContent = 'Upload complete. Checking the export…';
        cancel.disabled = true;
      },
    });
    active = job;
    cancel.addEventListener('click', () => job.abort());
    job.promise.then((result) => {
      progress.hidden = true;
      onOpened(result, {source: str(file.name)});
    }, (err) => {
      progress.hidden = true;
      const cancelled = err && err.code === 'aborted';
      fill(message, callout(cancelled ? 'info' : 'err', h('p', null, cancelled
        ? 'Upload cancelled. Nothing was opened.'
        : `Upload of ${str(file.name)} failed: ${str(err && err.message)}`)));
    }).finally(() => {
      active = null;
      choose.disabled = false;
    });
  }
  return {
    panel,
    setLimit(bytes) {
      const el = zone.querySelector('.dropzone__limit');
      el.textContent = `Up to ${fmtBytes(bytes)}.`;
      el.hidden = false;
    },
  };
}

function pathPanel(onOpened, message) {
  const input = h('input', {
    class: 'input input--block mono', type: 'text', name: 'path', id: 'open-path', autocomplete: 'off', spellcheck: 'false',
    placeholder: '/home/me/Downloads/mj_export_….zip', 'aria-describedby': 'open-path-help', required: true,
  });
  const submit = h('button', {type: 'submit', class: 'btn btn--primary'}, 'Open');
  const form = h('form', {class: 'path-form', novalidate: true},
    h('label', {class: 'field__label', for: 'open-path'}, 'ZIP or folder path'),
    h('div', {class: 'path-form__row'}, input, submit),
    h('p', {class: 'hint-text', id: 'open-path-help'},
      'A path on the computer running the viewer: an export ZIP, an export folder, or a ZIP holding several exports.'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const path = input.value.trim();
    if (!path) {
      fill(message, callout('warn', h('p', null, 'Enter the path of an export ZIP or folder.')));
      input.focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = 'Opening…';
    fill(message);
    try {
      const result = await postJSON(paths.open(), {path});
      onOpened(result, {source: path});
    } catch (err) {
      fill(message, callout('err', h('p', null, `Could not open ${path}: ${str(err.message)}`)));
    } finally {
      submit.disabled = false;
      submit.textContent = 'Open';
    }
  });
  return h('div', {class: 'panel open-panel'},
    h('h3', {class: 'panel__title'}, icon('folder', {size: 16}), 'Open from this computer'),
    form);
}

