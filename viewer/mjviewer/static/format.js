// Formatting and label tables. Pure functions: no DOM.

const numberFormat = new Intl.NumberFormat(undefined);
const formatters = new Map();

function dateFormatter(options) {
  const key = JSON.stringify(options);
  if (!formatters.has(key)) formatters.set(key, new Intl.DateTimeFormat(undefined, options));
  return formatters.get(key);
}

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
export const arr = (v) => (Array.isArray(v) ? v : []);
export const str = (v) => (v === null || v === undefined ? '' : String(v));
export const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});

export function fmtInt(n) {
  return isNum(n) ? numberFormat.format(n) : '—';
}

/** Sizes in binary units, named as such (KiB, MiB, ...): the settings that limit them (--max-upload-mb, the cache) are binary too. */
export function fmtBytes(n) {
  if (!isNum(n)) return '—';
  if (Math.abs(n) < 1024) return `${numberFormat.format(n)} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = n / 1024;
  let i = 0;
  while (Math.abs(value) >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[i]}`;
}

export function plural(n, one, many = `${one}s`) {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

export function shortId(id, keep = 8) {
  const s = str(id);
  return s.length > keep + 1 ? `${s.slice(0, keep)}…` : s;
}

// --- Dates ------------------------------------------------------------------
// Exports carry ISO strings with an offset, naive 'YYYY-MM-DD HH:MM:SS.ffffff'
// strings (no zone recorded), epoch-millisecond integers, or null.

const NAIVE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?$/;
const ZONED = /(?:z|[+-]\d{2}(?::?\d{2})?)$/i;

export function parseDate(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return {date: null, raw: value};
    const ms = Math.abs(value) < 1e11 ? value * 1000 : value;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? {date: null, raw: value} : {date, naive: false, raw: value};
  }
  if (typeof value !== 'string') return {date: null, raw: value};
  const s = value.trim();
  if (/^\d{12,14}$/.test(s)) return parseDate(Number(s));
  const m = NAIVE.exec(s);
  if (m && !ZONED.test(s.slice(10))) {
    const [, y, mo, d, hh, mm, ss, frac] = m;
    const ms = frac ? Number(`${frac}000`.slice(0, 3)) : 0;
    const date = new Date(Date.UTC(+y, +mo - 1, +d, +(hh || 0), +(mm || 0), +(ss || 0), ms));
    if (Number.isNaN(date.getTime())) return {date: null, raw: value};
    return {date, naive: true, dateOnly: hh === undefined, raw: value};
  }
  const t = Date.parse(s);
  if (Number.isNaN(t)) return {date: null, raw: value};
  return {date: new Date(t), naive: false, raw: value};
}

/** Human date; naive values are shown exactly as recorded (no zone conversion). */
export function formatDate(value, {dateOnly = false} = {}) {
  const p = parseDate(value);
  if (!p) return '—';
  if (!p.date) return str(p.raw);
  const options = dateOnly || p.dateOnly ? {dateStyle: 'medium'} : {dateStyle: 'medium', timeStyle: 'short'};
  if (p.naive) options.timeZone = 'UTC';
  return dateFormatter(options).format(p.date);
}

export function dateTitle(value) {
  const p = parseDate(value);
  if (!p) return 'Not recorded in the export';
  if (!p.date) return `Unrecognised date value: ${str(p.raw)}`;
  if (p.naive) return `${str(p.raw)} (recorded without a time zone)`;
  if (typeof p.raw === 'number') return `${p.date.toISOString()} (epoch ${p.raw})`;
  return str(p.raw);
}

/** True for a date string recorded without a time zone ('YYYY-MM-DD HH:MM:SS.ffffff'). */
export function isNaiveDate(value) {
  const p = parseDate(value);
  return Boolean(p && p.date && p.naive);
}

/**
 * The value to display for a creation date the API also sends as recorded
 * (created_at_raw): a naive string is shown exactly as written instead of being
 * converted from an assumed UTC; anything else uses the normalised value.
 */
export function recordedDate(normalised, raw) {
  return typeof raw === 'string' && isNaiveDate(raw) ? raw : normalised;
}

/** Value for <time datetime>, or '' when it cannot be expressed. */
export function dateAttr(value) {
  const p = parseDate(value);
  if (!p || !p.date) return '';
  if (p.naive) return p.date.toISOString().replace('Z', '').slice(0, p.dateOnly ? 10 : 19);
  return p.date.toISOString();
}

/** YYYY-MM-DD for <input type=date> min/max, or ''. */
export function dayValue(value) {
  const p = parseDate(value);
  if (!p || !p.date) return '';
  if (p.naive) return p.date.toISOString().slice(0, 10);
  const d = p.date;
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// --- Labels -----------------------------------------------------------------

const KINDS = {
  grid: 'Grid',
  variation: 'Variation',
  upscale: 'Upscale',
  zoom: 'Zoom',
  pan: 'Pan',
  remix: 'Remix',
  other: 'Other',
};

export function kindLabel(kind) {
  const k = str(kind);
  if (!k) return 'Unknown kind';
  return KINDS[k] || k.charAt(0).toUpperCase() + k.slice(1);
}

const STATUS = {
  complete: {label: 'Complete', tone: 'ok', icon: 'check-circle'},
  completed_with_failures: {label: 'Completed with failures', tone: 'warn', icon: 'alert-triangle'},
  stopped: {label: 'Stopped', tone: 'err', icon: 'alert-circle'},
  paused: {label: 'Paused', tone: 'warn', icon: 'pause'},
  reading: {label: 'Reading, unfinished', tone: 'info', icon: 'clock'},
  exporting: {label: 'Exporting, unfinished', tone: 'info', icon: 'clock'},
};

export function statusInfo(status) {
  const s = str(status);
  if (STATUS[s]) return STATUS[s];
  return {label: s ? `Status: ${s}` : 'Status not recorded', tone: 'neutral', icon: 'help'};
}

const SUPPORT = {
  supported: {label: 'Supported', tone: 'ok', help: 'The viewer was checked against exports of this exporter version (0.3.0 and 0.3.1 share one writer).'},
  unknown: {label: 'Untested version', tone: 'warn', help: 'The viewer was not checked against this exporter version; data is shown on a best-effort basis.'},
  missing: {label: 'No version', tone: 'warn', help: 'export_meta.json does not record an extension version; data is shown on a best-effort basis.'},
};

export function supportInfo(value) {
  return SUPPORT[str(value)] || {label: 'Version support unknown', tone: 'neutral', help: ''};
}

export const LINKS = {
  exporter: {
    label: 'Exporter link',
    short: 'exporter',
    style: 'solid',
    help: "Listed by the exporter in the board's generated_job_ids.",
  },
  derived: {
    label: 'Derived from job reference',
    short: 'derived',
    style: 'soft',
    help: "A personalization reference on the job has this board's id, but generated_job_ids does not list the job, so the viewer derived the link.",
  },
  explicit: {
    label: 'Explicit reference',
    short: 'explicit',
    style: 'solid',
    help: "A personalization reference on the job has this id.",
  },
  inferred_code: {
    label: 'Inferred by code match',
    short: 'inferred',
    style: 'dashed',
    help: 'Only a code matches. The link is likely but not proven.',
  },
  unresolved: {
    label: 'Unresolved',
    short: 'unresolved',
    style: 'warn',
    help: 'This reference matches nothing in the export.',
  },
  left_out: {
    label: 'Left out by the designer',
    short: 'left out',
    style: 'left-out',
    help: 'The designer chose not to export this moodboard, so it is not in the export.',
  },
};

export function linkInfo(link) {
  return LINKS[str(link)] || {label: str(link) || 'Unknown link', short: str(link) || 'unknown', style: 'dashed', help: ''};
}

export const IMAGE_STATUS = {
  present: {label: 'Present', short: 'Present', icon: 'image'},
  missing: {label: 'File missing from the export', short: 'Missing file', icon: 'image-off'},
  not_exported: {label: 'Image not exported (metadata only)', short: 'Not exported', icon: 'image'},
  unsupported: {label: 'Unsupported image format', short: 'Unsupported', icon: 'file'},
  failed: {label: 'The image could not be loaded', short: 'Load failed', icon: 'image-off'},
};

export function imageStatusInfo(status) {
  return IMAGE_STATUS[str(status)] || {label: 'Image unavailable', short: 'Unavailable', icon: 'image-off'};
}

export function profileVersionLabel(profile) {
  const v = str(profile && profile.version);
  if (!v) return '';
  return str(profile.service).toLowerCase() === 'niji' ? `Niji ${v}` : `V${v}`;
}
