// HTTP client for the viewer API (api 1). GET requests rely on the session
// cookie set with the page; every non-GET request also sends X-MJV-Token.
// In hosted mode a 401 sends the browser to sign in again, and a request for a
// published export that is not open yet opens it first (see prepareExport).

const tokenMeta = document.querySelector('meta[name="mjv-token"]');
const TOKEN = tokenMeta ? tokenMeta.getAttribute('content') || '' : '';
const modeMeta = document.querySelector('meta[name="mjv-mode"]');

/** True when the page was not served by the viewer (token never substituted). */
export const tokenMissing = !TOKEN || TOKEN.includes('{{');

/** True on a hosted deployment: Google sign-in and a published catalog instead of local files. */
export const hosted = Boolean(modeMeta && modeMeta.getAttribute('content') === 'hosted');

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const STATUS_TEXT = {
  400: 'The server did not accept this request.',
  401: 'Your session has ended. Signing in again…',
  403: 'The viewer refused this request. Reload the page to renew the session.',
  404: 'Not found.',
  405: 'This action is not available.',
  409: 'This export is not open yet.',
  413: 'The file is larger than the server accepts (see --max-upload-mb).',
  415: 'Unsupported file type.',
  500: 'The viewer server hit an internal error.',
  503: 'The viewer server is busy. Try again.',
};

const enc = (value) => encodeURIComponent(String(value));

function query(params) {
  if (!params) return '';
  const sp = params instanceof URLSearchParams ? params : new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => [k, String(v)]),
  );
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export const paths = {
  health: () => '/api/health',
  exports: () => '/api/exports',
  open: () => '/api/exports/open',
  upload: () => '/api/exports/upload',
  overview: (x) => `/api/exports/${enc(x)}/overview`,
  jobs: (x, params) => `/api/exports/${enc(x)}/jobs${query(params)}`,
  job: (x, j) => `/api/exports/${enc(x)}/jobs/${enc(j)}`,
  moodboards: (x) => `/api/exports/${enc(x)}/moodboards`,
  moodboard: (x, b) => `/api/exports/${enc(x)}/moodboards/${enc(b)}`,
  profiles: (x) => `/api/exports/${enc(x)}/profiles`,
  likes: (x) => `/api/exports/${enc(x)}/likes`,
  catalogItem: (c) => `/api/catalog/${enc(c)}`,
  catalogOpen: (c) => `/api/catalog/${enc(c)}/open`,
  catalogDelete: (c) => `/api/catalog/${enc(c)}/delete`,
};

function errorFrom(status, body) {
  const err = body && typeof body === 'object' && body.error && typeof body.error === 'object' ? body.error : null;
  const code = err && typeof err.code === 'string' && err.code ? err.code : `http_${status}`;
  const message = err && typeof err.message === 'string' && err.message
    ? err.message
    : STATUS_TEXT[status] || `Request failed (HTTP ${status}).`;
  return new ApiError(status, code, message);
}

function parseBody(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

const RETRY_DELAY_MS = 400;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let signingIn = false;

/** Hosted mode: start a new sign-in that comes back to the current view. */
export function signIn() {
  if (signingIn) return;
  signingIn = true;
  const next = `${location.pathname}${location.search}${location.hash}`;
  location.assign(`/auth/login?next=${encodeURIComponent(next || '/')}`);
}

async function request(path, init, attempt = 0, prepared = false) {
  let res;
  const isGet = !init || !init.method || init.method === 'GET';
  try {
    res = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...init});
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    // Browsers abort even local requests when the computer's network changes
    // (Chromium: ERR_NETWORK_CHANGED). A GET is safe to repeat once.
    if (attempt === 0 && isGet) {
      await pause(RETRY_DELAY_MS);
      return request(path, init, 1, prepared);
    }
    throw new ApiError(0, 'network', 'Cannot reach the viewer server. Check that it is still running, then retry.');
  }
  let text = '';
  try {
    text = await res.text();
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
  }
  const body = parseBody(text);
  if (hosted && res.status === 401) {
    signIn();
    throw errorFrom(401, null);
  }
  if (hosted && res.status === 409 && isGet && !prepared && body && body.error && body.error.code === 'export_not_ready'
      && body.catalog && typeof body.catalog === 'object') {
    await prepareExport(body.catalog, {signal: init && init.signal});
    return request(path, init, attempt, true);
  }
  if (!res.ok) throw errorFrom(res.status, body);
  if (!body || typeof body !== 'object') {
    throw new ApiError(res.status, 'bad_response', 'The server sent a response this viewer could not read.');
  }
  return body;
}

export function getJSON(path, {signal} = {}) {
  return request(path, {headers: {Accept: 'application/json'}, signal});
}

export function postJSON(path, data, {signal} = {}) {
  return request(path, {
    method: 'POST',
    headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-MJV-Token': TOKEN},
    body: JSON.stringify(data),
    signal,
  });
}

/**
 * Upload a ZIP with XMLHttpRequest so upload progress is observable.
 * The file name travels percent-encoded in X-Filename (headers are Latin-1 only).
 * Returns {promise, abort}.
 */
export function uploadZip(file, {onProgress, onSent} = {}) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open('POST', paths.upload());
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('Content-Type', 'application/zip');
    xhr.setRequestHeader('X-MJV-Token', TOKEN);
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name || 'upload.zip'));
    xhr.upload.addEventListener('progress', (event) => {
      if (onProgress) onProgress(event.lengthComputable && event.total ? event.loaded / event.total : null, event.loaded);
    });
    xhr.upload.addEventListener('load', () => {
      if (onSent) onSent();
    });
    xhr.addEventListener('load', () => {
      const body = parseBody(xhr.responseText);
      if (xhr.status >= 200 && xhr.status < 300 && body && typeof body === 'object') resolve(body);
      else reject(errorFrom(xhr.status, body));
    });
    xhr.addEventListener('error', () => reject(new ApiError(0, 'network',
      'The upload was interrupted. The server may have stopped, or the file may exceed its upload limit (--max-upload-mb).')));
    xhr.addEventListener('abort', () => reject(new ApiError(0, 'aborted', 'Upload cancelled.')));
    xhr.send(file);
  });
  return {promise, abort: () => xhr.abort()};
}

// ---------------------------------------------------------------------------
// Hosted mode: a published export is downloaded, checked and opened on the
// server in the background. prepareExport starts that (POST .../open) and polls
// until it is ready; every caller of the same export shares one run, and
// onPrepare listeners see each state (to show progress).
//
// A failed preparation starts again only when someone asks for it ({retry:
// true}: a click on Open, Try again or Retry). Automatic calls never ask, so two
// server instances that disagree about an export cannot restart a broken
// download over and over.

const PREPARE_POLL_MS = 700;
const REPOST_MIN_MS = 3000;
const preparing = new Map();
const retryRequested = new Set();
const prepareListeners = new Set();

export function onPrepare(fn) {
  prepareListeners.add(fn);
  return () => prepareListeners.delete(fn);
}

function emitPrepare(item) {
  for (const fn of prepareListeners) {
    try {
      fn(item);
    } catch (err) {
      console.error(err);
    }
  }
}

function abortError() {
  return new DOMException('The operation was aborted.', 'AbortError');
}

function untilAborted(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener('abort', onAbort, {once: true});
    promise.then((value) => {
      signal.removeEventListener('abort', onAbort);
      resolve(value);
    }, (err) => {
      signal.removeEventListener('abort', onAbort);
      reject(err);
    });
  });
}

function prepareFailure(id, item) {
  const error = item.error && typeof item.error === 'object' ? item.error : {};
  const detail = String(error.message || 'unknown error');
  const err = new ApiError(409, String(error.code || 'open_failed'), `This export could not be opened: ${detail}`);
  err.detail = detail;  // the server's own words, for views that add their own prefix
  err.retryPrepare = () => retryRequested.add(id);  // a Retry button: the next attempt starts it again
  return err;
}

async function runPrepare(id, retry) {
  let item = await postJSON(paths.catalogOpen(id), retry ? {retry: true} : {});
  let posted = Date.now();
  emitPrepare(item);
  while (item.state !== 'ready') {
    if (item.state === 'failed') throw prepareFailure(id, item);
    await pause(PREPARE_POLL_MS);
    // Behind a load balancer the poll may reach another server instance, which
    // starts the work there too (it reuses the shared download) when it has not.
    // That request never retries a failure, and goes out at most every few seconds.
    item = await getJSON(paths.catalogItem(id));
    if ((item.state === 'absent' || item.state === 'cached') && Date.now() - posted >= REPOST_MIN_MS) {
      item = await postJSON(paths.catalogOpen(id), {});
      posted = Date.now();
    }
    emitPrepare(item);
  }
  rememberSummaries(item.exports);
  return item;
}

/**
 * Make a published export (a catalog item) ready; resolves with the ready item.
 * retry: start a failed preparation again (only for a user's click).
 */
export function prepareExport(item, {signal, retry = false} = {}) {
  const id = item && typeof item.id === 'string' ? item.id : '';
  if (!id) return Promise.reject(new ApiError(0, 'bad_response', 'The server sent an export this viewer could not read.'));
  let run = preparing.get(id);
  if (!run) {
    run = runPrepare(id, retryRequested.delete(id) || retry);
    preparing.set(id, run);
    const forget = () => preparing.delete(id);
    run.then(forget, forget);
  }
  return untilAborted(run, signal);
}

let health = null;

/** GET /api/health, asked once per page; a failure is forgotten, so the next call asks again. */
export function loadHealth() {
  if (!health) {
    health = getJSON(paths.health()).catch((err) => {
      health = null;
      throw err;
    });
  }
  return health;
}

/** True when an export id (the catalog id, or <catalog id>-<n> per root) belongs to this catalog entry. */
export function fromCatalogItem(exportId, catalogId) {
  const x = String(exportId || '');
  return Boolean(catalogId) && (x === catalogId || x.startsWith(`${catalogId}-`));
}

/**
 * Delete a published export (its owner or an administrator only). The id is
 * sent again as confirmation. Never repeated on its own, and what this page
 * knew about the export is forgotten once it is gone.
 */
export async function deleteExport(id) {
  const result = await postJSON(paths.catalogDelete(id), {confirm: id});
  forgetCatalogItem(id);
  return result;
}

// ---------------------------------------------------------------------------
// Small in-memory caches. Exports are read-only, so summaries and overviews
// stay valid for the page session; opening or uploading refreshes them.

let exportsState = null;
const overviews = new Map();
const listeners = new Set();

export function onSummariesChanged(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  for (const fn of listeners) {
    try {
      fn();
    } catch (err) {
      console.error(err);
    }
  }
}

export async function loadExports({force = false, signal} = {}) {
  if (exportsState && !force) return exportsState;
  const data = await getJSON(paths.exports(), {signal});
  exportsState = {
    exports: Array.isArray(data.exports) ? data.exports.filter((e) => e && typeof e === 'object' && e.id) : [],
    library: Array.isArray(data.library) ? data.library.filter((e) => e && typeof e === 'object') : [],
    // Paths given on the command line that could not be opened (additive field).
    failed: Array.isArray(data.failed) ? data.failed.filter((e) => e && typeof e === 'object') : [],
    // Hosted mode: the published catalog and the state of each entry.
    catalog: data.catalog && typeof data.catalog === 'object' ? data.catalog : null,
  };
  notify();
  return exportsState;
}

export function knownExports() {
  return exportsState ? exportsState.exports : [];
}

/** The published catalog (hosted mode), or null. */
export function knownCatalog() {
  return exportsState ? exportsState.catalog : null;
}

export function knownSummary(id) {
  const found = knownExports().find((e) => e.id === id);
  if (found || !hosted) return found || null;
  // A published export that is not open yet: name it from its catalog entry.
  const catalog = knownCatalog();
  const items = catalog && Array.isArray(catalog.items) ? catalog.items : [];
  const item = items.find((i) => i && typeof i.id === 'string' && i.id === String(id).split('-')[0]);
  if (!item) return null;
  const summary = item.summary && typeof item.summary === 'object' ? item.summary : {};
  return {id, label: item.label || String(item.name || '').replace(/\.zip$/i, '') || id,
    counts: {jobs: summary.jobs, moodboards: summary.moodboards, profiles: summary.profiles}};
}

/** Drop a deleted catalog entry and the exports opened from it from the caches. */
function forgetCatalogItem(id) {
  for (const key of [...overviews.keys()]) if (fromCatalogItem(key, id)) overviews.delete(key);
  if (!exportsState) return;
  exportsState.exports = exportsState.exports.filter((e) => !fromCatalogItem(e.id, id));
  const catalog = exportsState.catalog;
  if (catalog && Array.isArray(catalog.items)) {
    exportsState.catalog = {...catalog, items: catalog.items.filter((i) => !(i && i.id === id))};
  }
  notify();
}

/** Merge summaries returned by open/upload/overview into the cache. */
export function rememberSummaries(list) {
  const items = (Array.isArray(list) ? list : []).filter((e) => e && typeof e === 'object' && e.id);
  if (!items.length) return;
  if (!exportsState) exportsState = {exports: [], library: [], failed: [], catalog: null};
  for (const summary of items) {
    const i = exportsState.exports.findIndex((e) => e.id === summary.id);
    if (i >= 0) exportsState.exports[i] = summary;
    else exportsState.exports.push(summary);
    overviews.delete(summary.id);
  }
  notify();
}

export async function loadOverview(id, {signal} = {}) {
  if (overviews.has(id)) return overviews.get(id);
  const data = await getJSON(paths.overview(id), {signal});
  overviews.set(id, data);
  if (data && data.summary && typeof data.summary === 'object' && data.summary.id) {
    const i = exportsState ? exportsState.exports.findIndex((e) => e.id === data.summary.id) : -1;
    if (i < 0) rememberSummaries([data.summary]);
    overviews.set(id, data);
  }
  return data;
}

/** Combine an outer (view) abort signal with a local one. */
export function linkedController(outer) {
  const controller = new AbortController();
  if (outer) {
    if (outer.aborted) controller.abort();
    else outer.addEventListener('abort', () => controller.abort(), {once: true});
  }
  return controller;
}

export function isAbort(err) {
  return Boolean(err && err.name === 'AbortError');
}
