// Hash routes. Every piece of view state that is worth sharing lives in the hash:
//   #/                                   exports home
//   #/x/{id}/overview                    export overview
//   #/x/{id}/gallery?q=&kind=&...        gallery with filters
//   #/x/{id}/job/{job_id}?in=&pos=       job detail (in/pos: the gallery list it was opened from)
//   #/x/{id}/moodboards                  moodboard list
//   #/x/{id}/moodboard/{board_id}        one moodboard
//   #/x/{id}/profiles                    profiles and unmatched codes
//   #/x/{id}/likes                       likes

const enc = (value) => encodeURIComponent(String(value));

function dec(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

const SECTIONS = new Set(['overview', 'gallery', 'moodboards', 'profiles', 'likes']);

export function parseHash(hash) {
  let raw = String(hash || '').replace(/^#/, '');
  if (!raw.startsWith('/')) raw = `/${raw}`;
  const qi = raw.indexOf('?');
  const pathPart = qi >= 0 ? raw.slice(0, qi) : raw;
  const query = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : '');
  const parts = pathPart.split('/').filter(Boolean).map(dec);
  if (parts.length === 0) return {name: 'home', query};
  if (parts[0] !== 'x' || !parts[1] || parts.length > 4) return {name: 'notfound', query};
  const exportId = parts[1];
  const section = parts[2] || 'overview';
  const arg = parts[3];
  if (SECTIONS.has(section)) return arg === undefined ? {name: section, exportId, query} : {name: 'notfound', exportId, query};
  if (section === 'job' && arg) return {name: 'job', exportId, jobId: arg, query};
  if (section === 'moodboard' && arg) return {name: 'moodboard', exportId, boardId: arg, query};
  return {name: 'notfound', exportId, query};
}

/** Build '?a=b' from an object or URLSearchParams, dropping empty values. */
export function qs(params) {
  let sp;
  if (params instanceof URLSearchParams) {
    sp = new URLSearchParams();
    for (const [k, v] of params) if (v !== '') sp.append(k, v);
  } else {
    sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (v === undefined || v === null || v === '') continue;
      sp.set(k, String(v));
    }
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export const hrefs = {
  home: () => '#/',
  overview: (x) => `#/x/${enc(x)}/overview`,
  gallery: (x, params) => `#/x/${enc(x)}/gallery${qs(params)}`,
  job: (x, j, params) => `#/x/${enc(x)}/job/${enc(j)}${qs(params)}`,
  moodboards: (x) => `#/x/${enc(x)}/moodboards`,
  moodboard: (x, b) => `#/x/${enc(x)}/moodboard/${enc(b)}`,
  profiles: (x) => `#/x/${enc(x)}/profiles`,
  likes: (x) => `#/x/${enc(x)}/likes`,
};

/** Navigate without adding a history entry and without re-rendering. */
export function replaceHash(hash) {
  try {
    history.replaceState(history.state, '', hash);
  } catch {
    // Some embedded contexts refuse replaceState; the visible state is still correct.
  }
}
