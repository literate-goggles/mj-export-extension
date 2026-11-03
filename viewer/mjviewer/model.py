"""Parse one MJ Export root into an immutable, queryable index.

The index only reads through a RootView (see sources.py) and never writes.
Relationship rules (the viewer never invents a relationship):

* Moodboard <-> job: 'exporter' when the job id is in the board's
  generated_job_ids; 'derived' when the board row has no generated_job_ids
  field (older export) and a job's personalization reference id equals the
  board id (whatever the reference type, as in lib/normalize.js), or when a
  present generated_job_ids omits such a job (reported as a warning);
  'inferred_code' when only a code matches (a reference code or a --profile
  flag code that is one of the board's codes).
* Profile <-> job: 'explicit' when a reference id equals the profile id,
  'inferred_code' when only a code matches.
* A reference whose id names no exported record but a moodboard the designer
  left out of the export (export_meta.selection.left_out_moodboard_ids) is
  'left_out', whatever its code matches.
* A reference that matches nothing is 'unresolved' and stays visible as an
  unmatched code.
* Lineage uses parent_id only. parent_index comes from parent_index, else an
  integer parent_grid. Walks are guarded against cycles. A parent that
  export_meta.parent_gaps marks left_out is 'left_out' rather than 'missing'.

Hidden items (hosted mode): an index built with hidden= (a Hidden, from
hidden_view of the full index) reads the same root as if the hidden jobs and
moodboards had never been exported: their rows, likes and files are left out
while it loads, and what visible data says about them (a child's parent id, a
board's generated_job_ids, a reference picture of a hidden job, export_meta.json)
is withheld, so no answer built from it can mention them.
"""
from __future__ import annotations

import contextlib
import gc
import hashlib
import json
import math
import re
import threading
import urllib.parse
from collections import Counter, defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .sources import (Issue, Limits, MemberError, RootView, TooLarge, describe_bytes,
                      detect_image_type, display_name, unsafe_reason)

META_FILE = 'export_meta.json'
JOBS = 'jobs.jsonl'
BOARDS = 'moodboards.jsonl'
PROFILES = 'profiles.jsonl'
LIKES = 'likes.jsonl'
JSONL_FILES = (JOBS, BOARDS, PROFILES, LIKES)
# Writers this viewer was checked against: 0.3.0 and 0.3.1 share one writer (lib/normalize.js,
# src/content.js), and 0.4.0 writes the same files plus the designer's selection (export_meta.selection,
# board generated_left_out, parent_gaps left_out). Anything else, including 0.2.x, is read with best effort.
SUPPORTED_VERSIONS = ('0.3.0', '0.3.1', '0.4.0')
LEVEL_ORDER = {'error': 0, 'warning': 1, 'info': 2, 'ok': 3}
MAX_CHECK_ITEMS = 500
MAX_LIMIT = 500
SUMMARY_COUNTS = ('jobs', 'moodboards', 'profiles', 'likes', 'images_expected', 'images_present')

KNOWN_META = {'extension_version', 'export_time', 'username', 'scope', 'status', 'counts',
              'failures', 'parent_gaps', 'feed_exhausted', 'limitations', 'selection'}
KNOWN_SCOPE = {'mode', 'max', 'since', 'images', 'testSubset'}
# export_meta.selection, written when the designer chose what to export (null when they did not).
KNOWN_SELECTION = {'chosen_at', 'moodboards', 'jobs', 'left_out_moodboard_ids'}
SELECTION_COUNTS = (('moodboards', ('available', 'exported', 'left_out')),
                    ('jobs', ('available', 'exported', 'left_out', 'not_reviewed')))
# A larger whole number is not exact in a browser, where JSON numbers are doubles. Such a selection count is
# treated as unreadable, so the server's sentences and the page never show different numbers.
MAX_COUNT = 2 ** 53
TOO_LARGE = 'too large to be a real count (above 2^53); ignored'
KNOWN_COUNTS = {'jobs', 'moodboards', 'profiles', 'likes', 'files', 'completed_jobs', 'download_bytes'}
KNOWN_JOB = {'job_id', 'created_at', 'prompt', 'full_command', 'flags', 'profile_default',
             'unknown_flags', 'parent_id', 'parent_index', 'kind', 'image_files', 'image_urls',
             'width', 'height', 'job_type', 'event_type', 'parent_grid', 'personalization_codes',
             'image_likes', 'video'}
KNOWN_FLAGS = {'ar', 'v', 'stylize', 'chaos', 'weird', 'sref', 'profile', 'niji', 'raw', 'tile', 'iw'}
KNOWN_REFERENCE = {'code', 'id', 'type', 'user_id'}
KNOWN_BOARD = {'id', 'code', 'codes', 'name', 'version', 'created_at', 'generated_job_ids', 'images',
               'generated_left_out'}
KNOWN_BOARD_IMAGE = {'source_url', 'file', 'width', 'height'}
KNOWN_PROFILE = {'id', 'codes', 'name', 'version', 'created_at', 'ratings_count', 'service', 'type'}
KNOWN_LIKE = {'job_id', 'image_likes'}
KNOWN_STATUS = ('reading', 'exporting', 'complete', 'completed_with_failures', 'paused', 'stopped')
PARTIAL_STATUS = ('paused', 'stopped')
# In-memory run states of the exporter; export_meta.json is only written after a run ends.
RUN_STATES = ('reading', 'exporting')
# The reason of a moodboard reference picture withheld because it is a hidden job's picture (ImageRef status
# 'unavailable'); only an index built with hidden items has such slots.
UNAVAILABLE = 'not available here'
# export_meta.counts values that lose what the hidden items account for, each by its Hidden.delta amount.
HIDDEN_META_COUNTS = ('jobs', 'moodboards', 'likes', 'files', 'download_bytes', 'completed_jobs')


_GC_LOCK = threading.Lock()
_GC_STATE = {'depth': 0, 'was_enabled': True}


@contextlib.contextmanager
def _gc_paused():
    """Pause the cyclic GC while an index is built: about 40% faster on large exports.

    Index objects form almost no reference cycles. Nested and concurrent loads
    are counted, and collection resumes when the last one finishes.
    """
    with _GC_LOCK:
        if _GC_STATE['depth'] == 0:
            _GC_STATE['was_enabled'] = gc.isenabled()
            gc.disable()
        _GC_STATE['depth'] += 1
    try:
        yield
    finally:
        with _GC_LOCK:
            _GC_STATE['depth'] -= 1
            if _GC_STATE['depth'] == 0 and _GC_STATE['was_enabled']:
                gc.enable()


class QueryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ helpers

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_or_none(value: Any) -> Optional[int]:
    return value if _is_int(value) else None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value != '' else None


def _as_id(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if value.strip() else None
    if _is_int(value):
        return str(value)
    return None


def _str_list(value: Any) -> List[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _dedupe(items: Iterable) -> list:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _scalar_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if value != '' else None
    if _is_int(value) or (isinstance(value, float) and math.isfinite(value)):
        return str(value)
    return None


def _reject_constant(name: str):
    raise ValueError('non-finite number %s' % name)


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('number out of range')
    return value


def loads(text: str):
    """json.loads that refuses NaN/Infinity so every API response stays valid JSON."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def _capped(items: list, limit: int = MAX_CHECK_ITEMS) -> Tuple[list, str]:
    if len(items) <= limit:
        return items, ''
    return items[:limit], ' Showing the first %d of %d.' % (limit, len(items))


def _count(value: Any) -> Optional[int]:
    """A selection count the viewer can use: a whole number from 0 to MAX_COUNT, else None."""
    return value if _is_int(value) and 0 <= value <= MAX_COUNT else None


def _things(n: int, one: str) -> str:
    """'1 moodboard', '1,203 generations'."""
    return '{:,} {}'.format(n, one if n == 1 else one + 's')


def _and_list(items: List[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return items[0] if len(items) == 1 else '%s and %s' % (', '.join(items[:-1]), items[-1])


# -------------------------------------------------------------------- dates

_ISO_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})'
    r'(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?'
    r'\s*(Z|z|UTC|[+-]\d{2}(?::?\d{2})?)?$')
_NUMERIC_RE = re.compile(r'^-?\d{9,}(?:\.\d+)?$')


def parse_date(value: Any) -> Optional[datetime]:
    """ISO with offset, naive strings (treated as UTC), epoch ms/s numbers, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        try:  # an integer too large for a float raises here too
            seconds = value / 1000.0 if abs(value) >= 1e11 else float(value)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _NUMERIC_RE.match(text):
        try:
            return parse_date(float(text))
        except (OverflowError, ValueError):
            return None
    match = _ISO_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        micro = int((fraction or '0').ljust(6, '0')[:6])
        moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                          int(second or 0), micro)
        if zone in (None, 'Z', 'z', 'UTC'):
            offset = timezone.utc
        else:
            digits = zone[1:].replace(':', '')
            hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
            if hours > 23 or minutes > 59:
                return None
            delta = timedelta(hours=hours, minutes=minutes)
            offset = timezone(delta if zone[0] == '+' else -delta)
        return moment.replace(tzinfo=offset).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def iso_utc(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    return '%04d-%02d-%02dT%02d:%02d:%02d.%03dZ' % (
        moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second,
        moment.microsecond // 1000)


def _date_key(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else '%04d-%02d-%02d' % (moment.year, moment.month, moment.day)


# ------------------------------------------------------- command and kinds

_FLAG_RE = re.compile(r'--([a-z][a-z0-9-]*)\b', re.I | re.A)
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+')
_QUOTED_RE = re.compile(r'^(["\']).*\1$')
_ALIASES = {'aspect': 'ar', 'version': 'v', 's': 'stylize', 'c': 'chaos', 'w': 'weird', 'p': 'profile'}


def empty_flags() -> dict:
    return {'ar': None, 'v': None, 'stylize': None, 'chaos': None, 'weird': None, 'sref': [],
            'profile': [], 'niji': None, 'raw': False, 'tile': False, 'iw': None}


def parse_command(full_command: Any) -> dict:
    """Port of lib/parser.js parseCommand, for rows that lack the parsed fields."""
    text = full_command if isinstance(full_command, str) else ''
    starts = []
    quote = None
    i, size = 0, len(text)
    while i < size:
        char = text[i]
        if char == '\\':
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in '"\'' and (i == 0 or text[i - 1].isspace()):
            quote = char
            i += 1
            continue
        if char == '-' and i + 1 < size and text[i + 1] == '-' and (i == 0 or text[i - 1].isspace()):
            match = _FLAG_RE.match(text, i)
            if match:
                starts.append((i, match.end(), match.group(1).lower()))
                i = match.end()
                continue
        i += 1
    flags = empty_flags()
    unknown: Dict[str, list] = {}
    profile_default = False
    for k, (_, end, raw_name) in enumerate(starts):
        name = _ALIASES.get(raw_name, raw_name)
        stop = starts[k + 1][0] if k + 1 < len(starts) else size
        value = text[end:stop].strip()
        if name in ('sref', 'profile'):
            flags[name].extend(t[1:-1] if _QUOTED_RE.match(t) else t for t in _TOKEN_RE.findall(value))
            if name == 'profile' and not value:
                profile_default = True
        elif name in ('raw', 'tile'):
            flags[name] = True
        elif name in flags:
            flags[name] = value or True
        else:
            unknown.setdefault(name, []).append(value or True)
    prompt = text[:starts[0][0] if starts else size].strip()
    return {'prompt': prompt, 'full_command': text, 'flags': flags,
            'profile_default': profile_default, 'unknown_flags': unknown}


_IMAGE_NAME = re.compile(r'^images/(.+)_(\d{1,3})\.[A-Za-z0-9]+$')
GRID_SIZE = 4  # images of one Midjourney job; the exporter keeps at most this many (lib/normalize.js)
_KIND_RULES = [(re.compile('zoom'), 'zoom'), (re.compile('pan'), 'pan'), (re.compile('remix'), 'remix'),
               (re.compile('upscale|upsample'), 'upscale'), (re.compile('variation|vary'), 'variation')]


def compute_kind(job_type: Any, event_type: Any, parent_id: Optional[str], image_count: int) -> str:
    """Same rules as lib/normalize.js; image_count stands in for batch_size."""
    text = ('%s %s' % (job_type if job_type else '', event_type if event_type else '')).lower()
    for pattern, kind in _KIND_RULES:
        if pattern.search(text):
            return kind
    if not parent_id and image_count > 1:
        return 'grid'
    return 'other'


def _norm_version(value: Any) -> Optional[str]:
    if _is_int(value):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        value = repr(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = re.match(r'^(\d{1,9})(?:\.(\d{1,9}))?$', text)
    if match:
        minor = (match.group(2) or '').rstrip('0')
        return str(int(match.group(1))) + ('.' + minor if minor else '')
    return display_name(text, 24)


def compute_version(flags: dict, job_type: Any) -> dict:
    """Version facet: --niji, else --v ('7.0' groups with '7'), else job_type, else Unknown.

    Only the observed job_type shape ('v8-2_diffusion', 'v7_diffusion') is read. A job_type
    that mentions niji is never turned into a version: its naming has not been observed, so
    neither 'Niji n' nor 'Vn' would be more than a guess.
    """
    niji = flags.get('niji')
    if niji is True:
        return {'value': 'niji', 'label': 'Niji', 'source': 'flag'}
    number = _norm_version(niji) if niji is not None and niji is not False else None
    if number:
        return {'value': 'niji' + number, 'label': 'Niji ' + number, 'source': 'flag'}
    value = flags.get('v')
    number = _norm_version(value) if value is not None and not isinstance(value, bool) else None
    if number:
        return {'value': 'v' + number, 'label': 'V' + number, 'source': 'flag'}
    if isinstance(job_type, str) and 'niji' not in job_type.lower():
        match = re.match(r'^v(\d{1,9})(?:[-_.](\d{1,9}))?(?:_|$)', job_type, re.I)
        if match:
            minor = (match.group(2) or '').rstrip('0')
            number = str(int(match.group(1))) + ('.' + minor if minor else '')
            return {'value': 'v' + number, 'label': 'V' + number, 'source': 'job_type'}
    return {'value': 'unknown', 'label': 'Unknown', 'source': 'none'}


def _version_sort_key(value: str) -> tuple:
    match = re.match(r'^(v|niji)(\d{1,24})(?:\.(\d{1,24}))?$', value)
    if match:
        group = 0 if match.group(1) == 'v' else 1
        return (group, -int(match.group(2)), -int(match.group(3) or 0), value)
    if value == 'niji':
        return (1, 1, 0, value)
    if value == 'unknown':
        return (3, 0, 0, value)
    return (2, 0, 0, value)


# ----------------------------------------------------------------- records

class FileRec:
    __slots__ = ('rel', 'file_id', 'stat', 'status', 'reason', 'kind', 'sniffed', 'referenced_by', 'unsafe_index')

    def __init__(self, rel, file_id, stat, status, reason):
        self.rel = rel
        self.file_id = file_id
        self.stat = stat
        self.status = status
        self.reason = reason
        self.kind = None
        self.sniffed = False
        self.referenced_by: List[tuple] = []
        self.unsafe_index = -1


class _Job:
    __slots__ = ('id', 'line', 'raw', 'created', 'created_iso', 'date', 'epoch', 'prompt', 'search',
                 'full_command', 'flags', 'flags_source', 'profile_default', 'unknown_flags', 'kind',
                 'kind_source', 'version', 'width', 'height', 'parent_id', 'parent_index', 'parent_grid',
                 'job_type', 'event_type', 'references', 'flag_codes', 'image_likes', 'video', 'slots',
                 'unknown_fields', 'board_links', 'profile_links', 'resolved_refs', 'codes', 'unmatched_codes',
                 'liked', 'children', 'parent_status', 'parent_hidden')


class _Board:
    __slots__ = ('id', 'line', 'raw', 'name', 'code', 'codes', 'version', 'created_iso', 'created_raw',
                 'generated', 'generated_state', 'generated_left_out', 'slots', 'unknown_fields', 'links',
                 'unresolved_generated', 'omitted', 'raw_withheld')


class _Profile:
    __slots__ = ('id', 'line', 'raw', 'name', 'type', 'version', 'service', 'codes',
                 'ratings_count', 'created_iso', 'created_raw', 'unknown_fields', 'links')


def _raw_scalar(value: Any) -> Any:
    """A date value exactly as recorded (string or number), or None for anything else."""
    if isinstance(value, str) or _is_int(value) or (isinstance(value, float) and math.isfinite(value)):
        return value
    return None


def _reference(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    return {'type': _text(raw.get('type')), 'id': _as_id(raw.get('id')),
            'code': _text(raw.get('code')), 'user_id': _as_id(raw.get('user_id'))}


# ------------------------------------------------------------------- index

class ExportIndex:
    """Everything the API serves for one export root, built once at load.

    hidden (hosted mode only): what to leave out, from hidden_view() of the full index of the same root. None
    reads everything, as local mode always does.
    """

    def __init__(self, view: RootView, export_id: str, source: dict, limits: Limits = Limits(),
                 hidden: Optional['Hidden'] = None):
        self.id = export_id
        self.view = view
        self.root = view.root
        self.source = source
        self.limits = limits
        self.hidden = hidden
        # What each export_meta.selection pair (available, exported) lost to hidden items: the numbers as
        # written are these plus the ones shown.
        self.selection_reduced = {'jobs': 0, 'moodboards': 0}
        self._offspring: Optional[Dict[str, List[str]]] = None
        self.meta: Optional[dict] = None
        self.file_states: Dict[str, dict] = {}
        self.parse_errors: List[dict] = []
        self.row_problems: List[dict] = []
        self.duplicates: List[dict] = []
        self.unknown: Dict[str, Counter] = defaultdict(Counter)
        self.bad_dates: List[dict] = []
        self.files: Dict[str, FileRec] = {}
        self.file_ids: Dict[str, FileRec] = {}
        self.unsafe_refs: List[dict] = []  # one per distinct unsafe value, with every reference to it
        self._unsafe: Dict[tuple, FileRec] = {}
        self._checks_lock = threading.Lock()
        self.jobs: Dict[str, _Job] = {}
        self.boards: Dict[str, _Board] = {}
        self.profiles: Dict[str, _Profile] = {}
        self.likes: List[dict] = []
        with _gc_paused():
            self._load()

    # ---------------------------------------------------------- reading

    def _read_meta(self) -> None:
        state = {'file': META_FILE, 'state': 'ok', 'bytes': 0, 'detail': ''}
        self.file_states[META_FILE] = state
        try:
            info = self.view.stat(META_FILE)
            if info is None:
                state['state'] = 'absent'
                return
            data = self.view.read_capped(META_FILE, self.limits.max_meta_bytes)
        except TooLarge as exc:
            state.update(state='too_large', detail=exc.message)
            return
        except MemberError as exc:
            state.update(state='unreadable', detail=exc.message)
            return
        state['bytes'] = len(data)
        if not data.strip():
            state['state'] = 'empty'
            return
        try:
            value = loads(data.decode('utf-8-sig'))
        except UnicodeDecodeError as exc:
            state.update(state='invalid', detail='invalid UTF-8 at byte %d' % exc.start)
            return
        except (ValueError, RecursionError) as exc:
            state.update(state='invalid', detail='invalid JSON (%s)' % _json_error(exc))
            return
        if not isinstance(value, dict):
            state.update(state='invalid', detail='top level is %s, not an object' % _json_type(value))
            return
        if self.hidden is not None:  # what the overview, the counts, the selection and the checks read
            value, self.selection_reduced = _redact_meta(value, self.hidden)
        self.meta = value

    def _contradiction(self, item: dict) -> None:
        """A selection value that contradicts another one or the files: listed with the warning, but still used."""
        self.selection_problems.append(item)
        self.selection_contradicted = True

    def _read_selection(self) -> None:
        """export_meta.selection: absent or null when the designer did not choose. A value that is missing, of
        the wrong type or above MAX_COUNT is ignored and listed (a warning); the readable rest is used. Values
        that contradict each other or the files are listed too, and shown as recorded."""
        self.selection: Optional[dict] = None
        self.selection_problems: List[dict] = []
        self.selection_contradicted = False
        # export_meta.json says that the designer did not choose, so nothing should be marked as left out.
        self.no_selection = self.meta is not None and self.meta.get('selection') is None
        raw = self.meta.get('selection') if self.meta is not None else None
        if raw is None:
            return
        problems = self.selection_problems

        def problem(field: str, text: str) -> None:
            problems.append({'file': META_FILE, 'field': field, 'problem': text})

        if not isinstance(raw, dict):
            problem('selection', 'is %s, not an object; ignored' % _json_type(raw))
            return
        self._note_unknown(META_FILE, ['selection.' + k for k in raw if k not in KNOWN_SELECTION])
        chosen = parse_date(raw.get('chosen_at'))
        if chosen is None:
            problem('selection.chosen_at', 'missing or not a date')
        selection: dict = {'chosen_at': iso_utc(chosen)}
        for group, keys in SELECTION_COUNTS:
            block = raw.get(group)
            counts = dict.fromkeys(keys)
            if isinstance(block, dict):
                self._note_unknown(META_FILE, ['selection.%s.%s' % (group, k) for k in block if k not in keys])
                for key in keys:
                    value = block.get(key)
                    counts[key] = _count(value)
                    if counts[key] is None:
                        problem('selection.%s.%s' % (group, key), TOO_LARGE if _is_int(value) and value > MAX_COUNT
                                else 'missing or not a whole number of 0 or more')
            else:
                problem('selection.' + group, 'missing or not an object')
            selection[group] = counts
            # What was exported, left out or not reviewed was all in scope, so it cannot add up to more.
            parts = [key for key in keys[1:] if counts[key] is not None]
            total = sum(counts[key] for key in parts)
            if counts['available'] is not None and total > counts['available']:
                names = _and_list([key.replace('_', ' ') for key in parts])
                self._contradiction({'file': META_FILE, 'field': 'selection.' + group,
                                     'problem': '{} {} {:,}, more than the {:,} available; shown as recorded'.format(
                                         names, 'is' if len(parts) == 1 else 'add up to', total, counts['available'])})
        ids = raw.get('left_out_moodboard_ids')
        if isinstance(ids, list):
            selection['left_out_moodboard_ids'] = _dedupe(x for x in (_as_id(v) for v in ids) if x)
            bad = sum(1 for v in ids if _as_id(v) is None)
            if bad:
                problem('selection.left_out_moodboard_ids', '%d value(s) are not ids; ignored' % bad)
        else:
            selection['left_out_moodboard_ids'] = []
            problem('selection.left_out_moodboard_ids', 'missing or not a list')
        self.selection = selection

    def _not_exported(self) -> Optional[Tuple[int, str]]:
        """The most generations the selection can have kept out of the export, and what that number is."""
        jobs = self.selection['jobs']
        bounds = []
        if jobs['left_out'] is not None and jobs['not_reviewed'] is not None:
            bounds.append((jobs['left_out'] + jobs['not_reviewed'],
                           'generations not exported because of the selection '
                           '(selection.jobs left_out + not_reviewed)'))
        if jobs['available'] is not None:
            # The number as written: hidden items were in scope as well, so the bound is the full index's.
            bounds.append((jobs['available'] + self.selection_reduced['jobs'],
                           'generations in scope (selection.jobs.available)'))
        return min(bounds, key=lambda bound: bound[0]) if bounds else None

    def _iter_lines(self, name: str):
        pieces: List[bytes] = []
        line = 0
        for chunk in self.view.iter_chunks(name, cap=self.limits.max_jsonl_bytes):
            start = 0
            while True:
                newline = chunk.find(b'\n', start)
                if newline < 0:
                    if start < len(chunk):
                        pieces.append(chunk[start:])
                    break
                pieces.append(chunk[start:newline])
                line += 1
                yield line, b''.join(pieces)
                pieces = []
                start = newline + 1
        if pieces:
            line += 1
            yield line, b''.join(pieces)

    def _read_jsonl(self, name: str) -> List[Tuple[int, dict]]:
        state = {'file': name, 'state': 'ok', 'bytes': 0, 'detail': ''}
        self.file_states[name] = state
        rows: List[Tuple[int, dict]] = []
        try:
            info = self.view.stat(name)
            if info is None:
                state['state'] = 'absent'
                return rows
            if info.size > self.limits.max_jsonl_bytes:
                raise TooLarge('too_large', 'larger than the %d byte limit' % self.limits.max_jsonl_bytes)
            for line, raw in self._iter_lines(name):
                state['bytes'] += len(raw) + 1
                text = raw.strip()
                if line == 1 and text.startswith(b'\xef\xbb\xbf'):
                    text = text[3:].strip()
                if not text:
                    continue
                try:
                    value = loads(text.decode('utf-8'))
                except UnicodeDecodeError as exc:
                    self.parse_errors.append({'file': name, 'line': line, 'message': 'invalid UTF-8 at byte %d' % exc.start})
                    continue
                except (ValueError, RecursionError) as exc:
                    self.parse_errors.append({'file': name, 'line': line, 'message': 'invalid JSON (%s)' % _json_error(exc)})
                    continue
                if not isinstance(value, dict):
                    self.parse_errors.append({'file': name, 'line': line, 'message': 'row is %s, not an object' % _json_type(value)})
                    continue
                rows.append((line, value))
        except TooLarge as exc:
            state.update(state='too_large', detail=exc.message + '; the file was not loaded')
            return []
        except MemberError as exc:
            state.update(state='unreadable', detail=exc.message + ('; %d rows read before the error were kept' % len(rows) if rows else ''))
            return rows
        if not rows and not any(e['file'] == name for e in self.parse_errors):
            state['state'] = 'empty'
        return rows

    def _note_unknown(self, name: str, fields: Iterable[str]) -> List[str]:
        names = sorted(set(fields))
        for field in names:
            self.unknown[name][field] += 1
        return names

    # ------------------------------------------------------------ files

    def _ref_file(self, value: Any, owner: tuple) -> FileRec:
        """Register a referenced path; owner is (kind, id, index), kept compact for large exports."""
        reason = unsafe_reason(value)
        if reason:
            key = ('path', value) if isinstance(value, str) else ('value', repr(value))
            rec = self._unsafe.get(key)
            if rec is None:
                rec = self._unsafe[key] = FileRec(value if isinstance(value, str) else None, None, None, 'unsupported',
                                                  'unsafe path refused (%s)' % reason)
                self.unsafe_refs.append({'path': display_name(value), 'reason': rec.reason, 'referenced_by': []})
                rec.unsafe_index = len(self.unsafe_refs) - 1
            rec.referenced_by.append(owner)
            self.unsafe_refs[rec.unsafe_index]['referenced_by'].append(_owner(owner))
            return rec
        rec = self.files.get(value)
        if rec is None:
            file_id = hashlib.sha256(('%s\0%s' % (self.id, value)).encode('utf-8', 'surrogatepass')).hexdigest()[:20]
            try:
                info = self.view.stat(value)
                if info is None:
                    rec = FileRec(value, None, None, 'missing', 'listed in the export metadata but not found in the export')
                else:
                    rec = FileRec(value, file_id, info, 'present', None)
            except MemberError as exc:
                rec = FileRec(value, None, None, 'unsupported', exc.message)
            self.files[value] = rec
            if rec.file_id:
                self.file_ids[rec.file_id] = rec
        rec.referenced_by.append(owner)
        return rec

    def sniff(self, rec: FileRec) -> FileRec:
        """Check magic bytes once; unsupported or unreadable files stop being served."""
        if rec.sniffed or rec.status != 'present':
            return rec
        try:
            head = self.view.head(rec.rel, 64)
            kind = detect_image_type(head)
            if kind is None:
                rec.reason = 'not a supported image type (%s)' % describe_bytes(head)
                rec.status = 'unsupported'
            rec.kind = kind
        except MemberError as exc:
            rec.reason = exc.message
            rec.status = 'unsupported'
        rec.sniffed = True
        return rec

    def file(self, file_id: str) -> Optional[FileRec]:
        rec = self.file_ids.get(file_id)
        return self.sniff(rec) if rec is not None else None

    def verify_files(self) -> int:
        """Read every servable file to its end, where a ZIP checks each member's CRC (--check: the server
        reads files on demand). Files that fail become unsupported and the checks are rebuilt; returns how many."""
        failed = 0
        for rec in list(self.files.values()):
            if rec.status != 'present':
                continue
            try:
                for _ in self.view.iter_chunks(rec.rel, cap=rec.stat.size):
                    pass
            except MemberError as exc:
                rec.status, rec.reason = 'unsupported', exc.message
                failed += 1
        if failed:
            self._refresh_checks()
        return failed

    def mark_unreadable(self, rec: FileRec, reason: str) -> None:
        """A file that failed while it was read (for example a CRC error at its end) is no longer served,
        and the overview and the summary count it as unreadable."""
        if rec.status == 'present':
            rec.status, rec.reason = 'unsupported', reason
            self._refresh_checks()

    def _refresh_checks(self) -> None:
        with self._checks_lock:
            self._build_checks()
            observed = self._observed_counts()
            summary = dict(self.summary)  # a copy keeps a label set after loading (hosted catalog labels)
            summary['counts'] = {k: observed[k] for k in SUMMARY_COUNTS}
            summary['warnings'] = sum(1 for c in self.checks if c['level'] == 'warning')
            summary['errors'] = sum(1 for c in self.checks if c['level'] == 'error')
            self.summary = summary

    def _image_ref(self, slot: tuple) -> dict:
        index, rec, file_value, source_url, width, height, reason = slot
        url = None
        if rec is None:
            status = 'unavailable' if reason is UNAVAILABLE and self.hidden is not None else 'not_exported'
        else:
            status, reason = rec.status, rec.reason
            if status == 'present':
                url = '/files/%s/%s' % (self.id, rec.file_id)
        return {'index': index, 'status': status, 'url': url,
                'thumb_url': url + '?size=thumb' if url else None,
                'width': width, 'height': height, 'file': file_value,
                'source_url': source_url, 'reason': reason}

    # ----------------------------------------------------------- loading

    def _load(self) -> None:
        self._read_meta()
        rows = {name: self._read_jsonl(name) for name in JSONL_FILES}
        meta = self.meta or {}
        self.scope = meta.get('scope') if isinstance(meta.get('scope'), dict) else None
        self.scope_images = self.scope.get('images') if self.scope and isinstance(self.scope.get('images'), bool) else None
        if self.meta is not None:
            self._note_unknown(META_FILE, [k for k in self.meta if k not in KNOWN_META])
            if self.scope:
                self._note_unknown(META_FILE, ['scope.' + k for k in self.scope if k not in KNOWN_SCOPE])
            if isinstance(self.meta.get('counts'), dict):
                self._note_unknown(META_FILE, ['counts.' + k for k in self.meta['counts'] if k not in KNOWN_COUNTS])
        self._read_selection()
        self._build_jobs(rows[JOBS])
        self._build_boards(rows[BOARDS])
        self._build_profiles(rows[PROFILES])
        self._build_likes(rows[LIKES])
        self._scan_files()
        self._link()
        self._lineage()
        self._build_cards()
        self._build_facets()
        self._build_checks()
        self._build_summary()

    def _build_jobs(self, rows) -> None:
        hidden = self.hidden
        for line, row in rows:
            try:
                job_id = _as_id(row.get('job_id'))
                if job_id is None:
                    self.row_problems.append({'file': JOBS, 'line': line, 'message': 'row has no job_id; skipped'})
                    continue
                if hidden is not None and job_id in hidden.jobs:
                    continue  # as if never exported: no duplicate, date, unknown-field or row problem either
                if job_id in self.jobs:
                    self.duplicates.append({'file': JOBS, 'line': line, 'id': job_id, 'first_line': self.jobs[job_id].line})
                    continue
                job = _Job()
                job.id, job.line, job.raw = job_id, line, row
                full_command = row.get('full_command') if isinstance(row.get('full_command'), str) else None
                parsed = None
                if not isinstance(row.get('flags'), dict) or not isinstance(row.get('prompt'), str):
                    parsed = parse_command(full_command) if full_command is not None else None
                job.full_command = full_command
                if isinstance(row.get('prompt'), str):
                    job.prompt = row['prompt']
                else:
                    job.prompt = parsed['prompt'] if parsed else ''
                job.search = job.prompt.casefold()
                if isinstance(row.get('flags'), dict):
                    job.flags, job.flags_source = row['flags'], 'export'
                elif parsed:
                    job.flags, job.flags_source = parsed['flags'], 'viewer'
                else:
                    job.flags, job.flags_source = empty_flags(), 'none'
                if isinstance(row.get('profile_default'), bool):
                    job.profile_default = row['profile_default']
                else:
                    job.profile_default = parsed['profile_default'] if parsed else False
                if isinstance(row.get('unknown_flags'), dict):
                    job.unknown_flags = row['unknown_flags']
                else:
                    job.unknown_flags = parsed['unknown_flags'] if parsed else {}
                job.created = parse_date(row.get('created_at'))
                if job.created is None and row.get('created_at') is not None:
                    self.bad_dates.append({'file': JOBS, 'line': line, 'id': job_id})
                job.created_iso = iso_utc(job.created)
                job.date = _date_key(job.created)
                job.epoch = job.created.timestamp() if job.created else None
                job.width, job.height = _int_or_none(row.get('width')), _int_or_none(row.get('height'))
                job.parent_id = _as_id(row.get('parent_id'))
                job.parent_grid = row.get('parent_grid')
                if _is_int(row.get('parent_index')):
                    job.parent_index = row['parent_index']
                elif _is_int(row.get('parent_grid')):
                    job.parent_index = row['parent_grid']
                else:
                    job.parent_index = None
                job.job_type = _scalar_text(row.get('job_type'))
                job.event_type = _scalar_text(row.get('event_type'))
                refs = row.get('personalization_codes')
                job.references = [r for r in (_reference(x) for x in refs) if r] if isinstance(refs, list) else []
                profile_flag = job.flags.get('profile')
                job.flag_codes = _dedupe(profile_flag.split() if isinstance(profile_flag, str) else _str_list(profile_flag))
                likes = row.get('image_likes')
                job.image_likes = likes if isinstance(likes, list) else []
                job.video = row.get('video') is True
                files = row.get('image_files') if isinstance(row.get('image_files'), list) else []
                urls = row.get('image_urls') if isinstance(row.get('image_urls'), list) else []
                image_count = max(len(files), len(urls))
                kind = row.get('kind')
                if isinstance(kind, str) and kind.strip():
                    job.kind, job.kind_source = kind.strip(), 'export'
                else:
                    job.kind = compute_kind(job.job_type, job.event_type, job.parent_id, image_count)
                    job.kind_source = 'viewer'
                job.version = compute_version(job.flags, job.job_type)
                job.slots = self._job_slots(job, files, urls)
                unknown = [k for k in row if k not in KNOWN_JOB]
                unknown += ['flags.' + k for k in job.flags if k not in KNOWN_FLAGS] if job.flags_source == 'export' else []
                if isinstance(refs, list):
                    unknown += ['personalization_codes[].' + k for r in refs if isinstance(r, dict) for k in r if k not in KNOWN_REFERENCE]
                job.unknown_fields = self._note_unknown(JOBS, unknown)
                job.parent_hidden = False
                if hidden is not None and job.parent_id is not None and job.parent_id in hidden.jobs:
                    # Made from a hidden job: kept, with its kind (computed above from the real parent), but no
                    # trace of the parent's id anywhere, the raw row included.
                    job.parent_hidden = True
                    job.parent_id = None
                    raw = dict(row, parent_id=None)
                    if not _is_int(job.parent_grid):
                        job.parent_grid = None
                        if 'parent_grid' in raw:
                            raw['parent_grid'] = None
                    job.raw = raw
                job.board_links, job.profile_links = {}, {}
                self.jobs[job_id] = job
            except Exception as exc:  # a value nothing anticipated skips its row, not the export root
                self.row_problems.append({'file': JOBS, 'line': line,
                                          'message': 'row could not be read (%s); skipped' % type(exc).__name__})

    def _job_slots(self, job: _Job, files: list, urls: list) -> list:
        placed: Dict[int, Any] = {}
        unplaced = []
        # A file name's index places it only within the job's own images, so a name such as
        # images/<job>_999.png cannot pad the job with hundreds of empty slots.
        limit = max(len(urls), len(files), GRID_SIZE)
        for value in files:
            match = _IMAGE_NAME.match(value) if isinstance(value, str) else None
            index = int(match.group(2)) if match and match.group(1) == job.id else None
            if index is not None and index < limit and index not in placed:
                placed[index] = value
            else:
                unplaced.append(value)
        for value in unplaced:
            index = 0
            while index in placed:
                index += 1
            placed[index] = value
        count = max([len(urls)] + [i + 1 for i in placed])
        slots = []
        for index in range(count):
            source_url = urls[index] if index < len(urls) and isinstance(urls[index], str) else None
            value = placed.get(index)
            rec = None
            reason = None
            if value is not None:
                rec = self._ref_file(value, ('job', job.id, index))
            elif self.scope_images is False:
                reason = 'metadata-only export: images were not included'
            elif job.video:
                reason = 'video media is not exported'
            else:
                reason = 'no file was recorded for this image'
            slots.append((index, rec, value if isinstance(value, str) else None, source_url,
                          job.width, job.height, reason))
        return slots

    def _build_boards(self, rows) -> None:
        bound = self._not_exported() if self.selection is not None else None
        hidden = self.hidden
        for line, row in rows:
            try:
                board_id = _as_id(row.get('id'))
                if board_id is None:
                    self.row_problems.append({'file': BOARDS, 'line': line, 'message': 'row has no id; skipped'})
                    continue
                if hidden is not None and board_id in hidden.moodboards:
                    continue  # as if never exported
                if board_id in self.boards:
                    self.duplicates.append({'file': BOARDS, 'line': line, 'id': board_id, 'first_line': self.boards[board_id].line})
                    continue
                board = _Board()
                board.id, board.line, board.raw = board_id, line, row
                board.raw_withheld = False
                withheld = None  # the copy of the row that the API shows, once something in it is withheld
                board.name = row.get('name') if isinstance(row.get('name'), str) else None
                board.code = _text(row.get('code'))
                board.codes = _dedupe(([board.code] if board.code else []) + _str_list(row.get('codes')))
                board.version = _scalar_text(row.get('version'))
                created = parse_date(row.get('created_at'))
                if created is None and row.get('created_at') is not None:
                    self.bad_dates.append({'file': BOARDS, 'line': line, 'id': board_id})
                board.created_iso = iso_utc(created)
                board.created_raw = _raw_scalar(row.get('created_at'))
                if 'generated_job_ids' not in row:
                    board.generated, board.generated_state = None, 'absent'
                elif isinstance(row['generated_job_ids'], list):
                    board.generated = _dedupe(x for x in (_as_id(v) for v in row['generated_job_ids']) if x)
                    board.generated_state = 'present'
                    if hidden is not None and any(x in hidden.jobs for x in board.generated):
                        # Hidden jobs leave the list quietly: never 'not in this export', never in the raw row.
                        board.generated = [x for x in board.generated if x not in hidden.jobs]
                        withheld = dict(row, generated_job_ids=[v for v in row['generated_job_ids']
                                                                if _as_id(v) not in hidden.jobs])
                else:
                    board.generated, board.generated_state = None, 'invalid'
                # Written only when the designer chose what to export: the board's generations in scope that the
                # export does not have because of that choice, whether left out or made after choosing.
                left_out = row.get('generated_left_out')
                board.generated_left_out = _count(left_out)
                images = row.get('images') if isinstance(row.get('images'), list) else []
                board.slots = []
                shown = images  # the image entries whose fields count as this board's (unknown fields)
                for index, image in enumerate(images):
                    if not isinstance(image, dict):
                        board.slots.append((index, None, None, None, None, None, 'image entry is not an object'))
                        continue
                    if hidden is not None and (board_id, index) in hidden.withheld:
                        # A picture of a hidden job pinned to this board: its address names the job, so neither
                        # it, its file nor its size is shown, and the file is never registered.
                        board.slots.append((index, None, None, None, None, None, UNAVAILABLE))
                        if withheld is None:
                            withheld = dict(row)
                        if shown is images:
                            withheld['images'] = shown = list(images)
                        shown[index] = None
                        continue
                    value = image.get('file')
                    rec = None
                    reason = None
                    if value is not None:
                        rec = self._ref_file(value, ('moodboard', board_id, index))
                    elif self.scope_images is False:
                        reason = 'metadata-only export: images were not included'
                    else:
                        reason = 'no file was recorded for this reference image'
                    board.slots.append((index, rec, value if isinstance(value, str) else None,
                                        _text(image.get('source_url')), _int_or_none(image.get('width')),
                                        _int_or_none(image.get('height')), reason))
                unknown = [k for k in row if k not in KNOWN_BOARD]
                unknown += ['images[].' + k for im in shown if isinstance(im, dict) for k in im if k not in KNOWN_BOARD_IMAGE]
                board.unknown_fields = self._note_unknown(BOARDS, unknown)
                if withheld is not None:
                    board.raw, board.raw_withheld = withheld, True
                self.boards[board_id] = board
                if 'generated_left_out' in row:
                    where = {'file': BOARDS, 'line': line, 'id': board_id, 'field': 'generated_left_out'}
                    if board.generated_left_out is None:
                        too_large = _is_int(left_out) and left_out > MAX_COUNT
                        self.selection_problems.append(
                            dict(where, problem=TOO_LARGE if too_large else 'not a whole number of 0 or more; ignored'))
                    elif self.no_selection:
                        self._contradiction(dict(where, problem='recorded, but export_meta.json records no selection; '
                                                                'shown as recorded'))
                    elif bound is not None and board.generated_left_out > bound[0]:
                        self._contradiction(dict(where, problem='is {:,}, more than the {:,} {}; shown as '
                                                                'recorded'.format(board.generated_left_out, *bound)))
            except Exception as exc:  # a value nothing anticipated skips its row, not the export root
                self.row_problems.append({'file': BOARDS, 'line': line,
                                          'message': 'row could not be read (%s); skipped' % type(exc).__name__})

    def _build_profiles(self, rows) -> None:
        for line, row in rows:
            try:
                profile_id = _as_id(row.get('id'))
                if profile_id is None:
                    self.row_problems.append({'file': PROFILES, 'line': line, 'message': 'row has no id; skipped'})
                    continue
                if profile_id in self.profiles:
                    self.duplicates.append({'file': PROFILES, 'line': line, 'id': profile_id, 'first_line': self.profiles[profile_id].line})
                    continue
                profile = _Profile()
                profile.id, profile.line, profile.raw = profile_id, line, row
                profile.name = row.get('name') if isinstance(row.get('name'), str) else None
                profile.type = _text(row.get('type'))
                profile.version = _scalar_text(row.get('version'))
                profile.service = _text(row.get('service'))
                profile.codes = _dedupe(_str_list(row.get('codes')) + ([row['code']] if _text(row.get('code')) else []))
                profile.ratings_count = _int_or_none(row.get('ratings_count'))
                created = parse_date(row.get('created_at'))
                if created is None and row.get('created_at') is not None:
                    self.bad_dates.append({'file': PROFILES, 'line': line, 'id': profile_id})
                profile.created_iso = iso_utc(created)
                profile.created_raw = _raw_scalar(row.get('created_at'))
                profile.unknown_fields = self._note_unknown(PROFILES, [k for k in row if k not in KNOWN_PROFILE])
                self.profiles[profile_id] = profile
            except Exception as exc:  # a value nothing anticipated skips its row, not the export root
                self.row_problems.append({'file': PROFILES, 'line': line,
                                          'message': 'row could not be read (%s); skipped' % type(exc).__name__})

    def _build_likes(self, rows) -> None:
        hidden = self.hidden
        for line, row in rows:
            job_id = _as_id(row.get('job_id'))
            if job_id is None:
                self.row_problems.append({'file': LIKES, 'line': line, 'message': 'row has no job_id; skipped'})
                continue
            if hidden is not None and job_id in hidden.jobs:
                continue
            self.likes.append({'job_id': job_id, 'image_likes': row.get('image_likes'), 'line': line})
            self._note_unknown(LIKES, [k for k in row if k not in KNOWN_LIKE])
        liked = {like['job_id'] for like in self.likes if like['image_likes']}
        for job in self.jobs.values():
            job.liked = bool(job.image_likes) or job.id in liked

    def _scan_files(self) -> None:
        listing, issues = self.view.list_files('images')
        if self.hidden is not None:  # a file named for a hidden item, or only it uses, is never listed
            issues = [issue for issue in issues if not self._hidden_file(issue.raw)]
            listing = {rel: info for rel, info in listing.items() if not self._hidden_file(rel)}
        self.listing_issues = issues
        self.unreferenced = sorted(rel for rel in listing if rel not in self.files)
        present = [rec for rec in self.files.values() if rec.status == 'present']
        self.sniffed_all = len(present) <= self.limits.sniff_limit
        if self.sniffed_all:
            for rec in present:
                self.sniff(rec)

    # ------------------------------------------------------------ links

    def _link(self) -> None:
        board_codes: Dict[str, List[str]] = defaultdict(list)
        for board in self.boards.values():
            for code in board.codes:
                board_codes[code].append(board.id)
        profile_codes: Dict[str, List[str]] = defaultdict(list)
        for profile in self.profiles.values():
            for code in profile.codes:
                profile_codes[code].append(profile.id)
        explicit_boards: Dict[str, List[str]] = defaultdict(list)
        coded_boards: Dict[str, Dict[str, str]] = defaultdict(dict)
        explicit_profiles: Dict[str, Dict[str, str]] = defaultdict(dict)
        coded_profiles: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.unmatched: Dict[str, dict] = {}
        self.unresolved_refs: List[dict] = []
        self.left_out_refs: List[dict] = []
        left_out_ids = self.selection['left_out_moodboard_ids'] if self.selection else []
        left_out_boards = set(left_out_ids)
        in_export = [board_id for board_id in left_out_ids if board_id in self.boards]
        if in_export:
            self._contradiction({
                'file': META_FILE, 'field': 'selection.left_out_moodboard_ids', 'ids': in_export,
                'problem': '%d id(s) name moodboards that are in this export; they are shown as exported' % len(in_export)})
        # The exporter lists only left-out moodboards, so the list cannot be longer than their count.
        outside = len(left_out_ids) - len(in_export)
        counted = self.selection['moodboards']['left_out'] if self.selection else None
        if counted is not None and outside > counted:
            self._contradiction({'file': META_FILE, 'field': 'selection.left_out_moodboard_ids',
                                 'problem': 'names {} outside this export, more than the {:,} left out '
                                            '(selection.moodboards.left_out); shown as recorded'.format(
                                                _things(outside, 'moodboard'), counted)})

        def unmatched(code: str, kind: str, job_id: str) -> None:
            entry = self.unmatched.setdefault(code, {'types': [], 'job_ids': []})
            if kind not in entry['types']:
                entry['types'].append(kind)
            if job_id not in entry['job_ids']:
                entry['job_ids'].append(job_id)

        for job in self.jobs.values():
            job.resolved_refs = []
            seen_codes = set()
            for ref in job.references:
                entry = dict(ref, source='reference', link='unresolved', target=None, reason=None)
                matched = []
                if ref['id'] is not None:
                    if ref['id'] in self.boards:
                        matched.append('moodboard')
                    if ref['id'] in self.profiles:
                        matched.append('profile')
                    if ref['type'] == 'profile':
                        matched.sort(key=lambda kind: kind != 'profile')
                if matched:
                    reason = 'reference id equals the %s id' % matched[0]
                    if len(matched) > 1:
                        reason += '; it also equals a %s id' % matched[1]
                    if 'moodboard' not in matched and ref['id'] in left_out_boards:
                        reason += '; it also equals the id of a moodboard the designer left out of the export'
                    entry.update(link='explicit', target=self._target(matched[0], ref['id']), reason=reason)
                    # Like the exporter (lib/normalize.js), an id links to every record that has it,
                    # whatever the reference type says.
                    if 'moodboard' in matched:
                        explicit_boards[ref['id']].append(job.id)
                    if 'profile' in matched:
                        explicit_profiles[ref['id']][job.id] = "the job's personalization reference id equals the profile id"
                elif ref['id'] in left_out_boards:
                    # The id names the board itself, so a code that happens to match another record is not used.
                    entry.update(link='left_out', reason='the designer left this moodboard out of the export')
                    self.left_out_refs.append({'job_id': job.id, 'type': ref['type'], 'id': ref['id'],
                                               'code': ref['code'], 'source': 'reference', 'reason': entry['reason']})
                elif ref['code']:
                    boards = board_codes.get(ref['code'], [])
                    profiles = profile_codes.get(ref['code'], [])
                    if boards or profiles:
                        why = 'the reference has no id' if ref['id'] is None else \
                            'its id matches no exported moodboard or profile'
                        first = ('moodboard', boards[0]) if boards else ('profile', profiles[0])
                        entry.update(link='inferred_code', target=self._target(*first),
                                     reason='inferred: code %s matches; %s' % (ref['code'], why))
                        for board_id in boards:
                            coded_boards[board_id].setdefault(job.id, "inferred: reference code %s is one of the board's codes (%s)" % (ref['code'], why))
                        for profile_id in profiles:
                            coded_profiles[profile_id].setdefault(job.id, "inferred: reference code %s is one of the profile's codes (%s)" % (ref['code'], why))
                if entry['link'] == 'unresolved':
                    if ref['id'] is None and ref['code'] is None:
                        reason = 'the reference has neither an id nor a code'
                    elif ref['id'] is None:
                        reason = "type '%s' reference without an id; its code matches no exported moodboard or profile" % (ref['type'] or 'unknown')
                    elif ref['code'] is None:
                        reason = 'its id matches no exported moodboard or profile'
                    else:
                        reason = 'neither its id nor its code matches an exported moodboard or profile'
                    entry['reason'] = reason
                    if ref['code']:
                        unmatched(ref['code'], ref['type'] or 'unknown', job.id)
                    self.unresolved_refs.append({'job_id': job.id, 'type': ref['type'], 'id': ref['id'],
                                                 'code': ref['code'], 'source': 'reference', 'reason': reason})
                if ref['code']:
                    seen_codes.add(ref['code'])
                job.resolved_refs.append(entry)
            for code in job.flag_codes:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                entry = {'type': 'flag', 'id': None, 'code': code, 'user_id': None, 'source': 'flag',
                         'link': 'unresolved', 'target': None, 'reason': None}
                boards = board_codes.get(code, [])
                profiles = profile_codes.get(code, [])
                if boards or profiles:
                    first = ('moodboard', boards[0]) if boards else ('profile', profiles[0])
                    entry.update(link='inferred_code', target=self._target(*first),
                                 reason='inferred: --profile flag code %s matches; the job has no matching reference' % code)
                    for board_id in boards:
                        coded_boards[board_id].setdefault(job.id, "inferred: --profile flag code %s is one of the board's codes" % code)
                    for profile_id in profiles:
                        coded_profiles[profile_id].setdefault(job.id, "inferred: --profile flag code %s is one of the profile's codes" % code)
                else:
                    entry['reason'] = '--profile flag code matches no exported moodboard or profile'
                    unmatched(code, 'flag', job.id)
                    self.unresolved_refs.append({'job_id': job.id, 'type': 'flag', 'id': None, 'code': code,
                                                 'source': 'flag', 'reason': entry['reason']})
                job.resolved_refs.append(entry)
            codes = []
            seen = set()
            for entry in job.resolved_refs:
                key = (entry['code'], entry['type'])
                if entry['code'] and key not in seen:
                    seen.add(key)
                    codes.append({'code': entry['code'], 'type': entry['type'],
                                  'resolved': entry['link'] != 'unresolved'})
            job.codes = codes
            job.unmatched_codes = {entry['code'] for entry in job.resolved_refs
                                   if entry['code'] and entry['link'] == 'unresolved'}
        if self.selection is not None:
            # The left-out moodboards exported jobs use, counted from the references labelled left_out:
            # a listed id that names an exported board, or that no exported job references, is not one.
            self.selection['left_out_moodboards_used'] = len({ref['id'] for ref in self.left_out_refs})

        for board in self.boards.values():
            board.links = {}
            board.unresolved_generated = []
            board.omitted = []
            explicit = _dedupe(explicit_boards.get(board.id, []))
            explicit_set = set(explicit)
            if board.generated is not None:
                for job_id in board.generated:
                    if job_id in self.jobs:
                        evidence = "listed in the board's generated_job_ids"
                        if job_id in explicit_set:
                            evidence += '; the job also references the board id'
                        board.links[job_id] = ('exporter', evidence)
                    else:
                        board.unresolved_generated.append(job_id)
            for job_id in explicit:
                if job_id in board.links:
                    continue
                if board.generated is None:
                    board.links[job_id] = ('derived', "the job's personalization reference id equals the board id; "
                                                      'this board row has no usable generated_job_ids')
                else:
                    board.links[job_id] = ('derived', "the job's personalization reference id equals the board id, "
                                                      'but generated_job_ids omits the job')
                    board.omitted.append(job_id)
            for job_id, evidence in coded_boards.get(board.id, {}).items():
                board.links.setdefault(job_id, ('inferred_code', evidence))
            for job_id, (link, evidence) in board.links.items():
                self.jobs[job_id].board_links[board.id] = (link, evidence)

        for profile in self.profiles.values():
            profile.links = {}
            for job_id, evidence in explicit_profiles.get(profile.id, {}).items():
                profile.links[job_id] = ('explicit', evidence)
            for job_id, evidence in coded_profiles.get(profile.id, {}).items():
                profile.links.setdefault(job_id, ('inferred_code', evidence))
            for job_id, (link, evidence) in profile.links.items():
                self.jobs[job_id].profile_links[profile.id] = (link, evidence)

    def _target(self, kind: str, target_id: str) -> dict:
        if kind == 'moodboard':
            return {'kind': 'moodboard', 'id': target_id, 'name': self.boards[target_id].name}
        return {'kind': 'profile', 'id': target_id, 'name': self.profiles[target_id].name}

    # ---------------------------------------------------------- lineage

    def _lineage(self) -> None:
        children: Dict[str, List[str]] = defaultdict(list)
        for job in self.jobs.values():
            if job.parent_hidden:  # neither a gap nor a missing parent: nothing about it is shown
                job.parent_status = 'unavailable'
            elif job.parent_id is None:
                job.parent_status = 'none'
            elif job.parent_id in self.jobs:
                job.parent_status = 'in_export'
                if job.parent_id != job.id:
                    children[job.parent_id].append(job.id)
            else:
                job.parent_status = 'missing'
        for job in self.jobs.values():
            kids = children.get(job.id, [])
            kids.sort(key=lambda k: (self.jobs[k].parent_index is None, self.jobs[k].parent_index or 0,
                                     self.jobs[k].epoch is None, self.jobs[k].epoch or 0, k))
            job.children = kids
        # One pass over the parent pointers finds every cycle.
        state: Dict[str, int] = {}
        self.cycle_members: List[str] = []
        for start in self.jobs:
            if start in state:
                continue
            path: List[str] = []
            position: Dict[str, int] = {}
            node: Optional[str] = start
            while node is not None and node in self.jobs and node not in state:
                state[node] = 0
                position[node] = len(path)
                path.append(node)
                node = self.jobs[node].parent_id
            if node is not None and node in position:
                self.cycle_members.extend(path[position[node]:])
            for item in path:
                state[item] = 1
        # Without export_meta.parent_gaps there is nothing to compare with: reported_by_exporter is None.
        self.gaps_listed = self.meta is not None and isinstance(self.meta.get('parent_gaps'), list)
        meta_gaps = self.meta['parent_gaps'] if self.gaps_listed else []
        reported = {(_as_id(g.get('job_id')), _as_id(g.get('parent_id'))) for g in meta_gaps if isinstance(g, dict)}
        # The exporter marks a parent that was in scope but not exported because of the designer's selection.
        left_out = {(_as_id(g.get('job_id')), _as_id(g.get('parent_id'))) for g in meta_gaps
                    if isinstance(g, dict) and g.get('left_out') is True}
        self.gaps = []
        self.left_out_gaps = []
        for job in self.jobs.values():
            if job.parent_status == 'missing' and (job.id, job.parent_id) in left_out:
                job.parent_status = 'left_out'
                self.left_out_gaps.append({'job_id': job.id, 'parent_id': job.parent_id,
                                           'reason': 'the designer left the parent out of this export'})
            elif job.parent_status == 'missing':
                self.gaps.append({'job_id': job.id, 'parent_id': job.parent_id,
                                  'reported_by_exporter': (job.id, job.parent_id) in reported if self.gaps_listed else None,
                                  'reason': 'the parent job is not in this export'})
        if self.left_out_gaps:
            parents = len({gap['parent_id'] for gap in self.left_out_gaps})
            bound = self._not_exported() if self.selection is not None else None
            problem = None
            if self.no_selection:
                problem = 'marks %s left_out, but export_meta.json records no selection' % _things(parents, 'parent')
            elif bound is not None and parents > bound[0]:
                problem = 'marks {} left_out, more than the {:,} {}'.format(_things(parents, 'parent'), *bound)
            if problem:
                self._contradiction({'file': META_FILE, 'field': 'parent_gaps',
                                     'problem': problem + '; shown as left out'})
        self.stale_gaps = []
        for gap in meta_gaps:
            if not isinstance(gap, dict):
                continue
            job_id, parent_id = _as_id(gap.get('job_id')), _as_id(gap.get('parent_id'))
            if job_id not in self.jobs:
                self.stale_gaps.append({'job_id': job_id, 'parent_id': parent_id, 'reason': 'the job is not in this export'})
            elif parent_id in self.jobs:
                self.stale_gaps.append({'job_id': job_id, 'parent_id': parent_id, 'reason': 'the parent is present in this export'})

    def _ancestors(self, job: _Job) -> Tuple[List[str], bool]:
        chain: List[str] = []
        seen = {job.id}
        parent_id = job.parent_id
        while parent_id is not None:
            if parent_id in seen:
                return list(reversed(chain)), True
            parent = self.jobs.get(parent_id)
            if parent is None:
                break
            chain.append(parent.id)
            seen.add(parent.id)
            parent_id = parent.parent_id
        return list(reversed(chain)), False

    def _descendants(self, job: _Job) -> int:
        seen = {job.id}
        queue = list(job.children)
        count = 0
        while queue:
            job_id = queue.pop()
            if job_id in seen:
                continue
            seen.add(job_id)
            count += 1
            queue.extend(self.jobs[job_id].children)
        return count

    # ------------------------------------------------------------ cards

    def _board_label(self, board: _Board) -> str:
        return board.name or board.code or board.id

    def _profile_label(self, profile: _Profile) -> str:
        return profile.name or profile.id

    def card(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        return self._card(job) if job is not None else None

    def _card(self, job: _Job) -> dict:
        """JobCard, built on demand (cheap) so large exports keep a small footprint."""
        return {
            'job_id': job.id,
            'created_at': job.created_iso,
            'prompt': job.prompt,
            'kind': job.kind,
            'kind_source': job.kind_source,
            'version': dict(job.version),
            'width': job.width,
            'height': job.height,
            'images': [self._image_ref(slot) for slot in job.slots],
            'moodboards': [{'id': board_id, 'name': self.boards[board_id].name, 'link': link}
                           for board_id, (link, _) in job.board_links.items()],
            'profiles': [{'id': profile_id, 'name': self.profiles[profile_id].name, 'link': link}
                         for profile_id, (link, _) in job.profile_links.items()],
            'codes': [dict(code) for code in job.codes],
            'parent_id': job.parent_id,
            'parent_status': job.parent_status,
            'child_count': len(job.children),
            'liked': job.liked,
        }

    def _build_cards(self) -> None:
        dated = [j for j in self.jobs.values() if j.epoch is not None]
        undated = sorted((j for j in self.jobs.values() if j.epoch is None), key=lambda j: j.id)
        self.order_newest = sorted(dated, key=lambda j: (-j.epoch, j.id)) + undated
        self.order_oldest = sorted(dated, key=lambda j: (j.epoch, j.id)) + undated

    def _build_facets(self) -> None:
        kinds = Counter(job.kind for job in self.jobs.values())
        versions = Counter(job.version['value'] for job in self.jobs.values())
        labels = {job.version['value']: job.version['label'] for job in self.jobs.values()}
        dates = [job.date for job in self.jobs.values() if job.date]
        profiles = [{'id': p.id, 'label': self._profile_label(p), 'count': len(p.links), 'unresolved': False}
                    for p in self.profiles.values()]
        for code, entry in sorted(self.unmatched.items()):
            profiles.append({'id': 'code:' + code, 'label': '%s (unmatched %s code)' % (code, '/'.join(entry['types'])),
                             'count': len(entry['job_ids']), 'unresolved': True})
        self.facets = {
            'kinds': [{'value': k, 'count': n} for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
            'versions': [{'value': v, 'label': labels[v], 'count': versions[v]}
                         for v in sorted(versions, key=_version_sort_key)],
            'moodboards': [{'id': b.id, 'label': self._board_label(b), 'count': len(b.links)} for b in self.boards.values()],
            'profiles': profiles,
            'date_min': min(dates) if dates else None,
            'date_max': max(dates) if dates else None,
        }

    # ----------------------------------------------------------- checks

    def _check(self, check_id: str, level: str, title: str, detail: str = '', items: Optional[list] = None) -> None:
        items = items or []
        shown, note = _capped(items)
        self._building.append({'id': check_id, 'level': level, 'title': title,
                               'detail': (detail + note).strip(), 'items': shown})

    def _build_checks(self) -> None:
        self._building: List[dict] = []
        meta = self.meta
        version = meta.get('extension_version') if meta else None
        if isinstance(version, str) and version in SUPPORTED_VERSIONS:
            self.version_support = 'supported'
            self._check('extension_version', 'ok', 'Extension version %s is supported' % version)
        elif version is not None:
            self.version_support = 'unknown'
            self._check('extension_version', 'warning', 'Unknown extension version',
                        'This viewer was checked against exports from versions %s; %s was read with best effort.'
                        % (_and_list(list(SUPPORTED_VERSIONS)), display_name(str(version), 40)))
        else:
            self.version_support = 'missing'
            self._check('extension_version', 'warning', 'Extension version is missing',
                        'export_meta.json has no extension_version; the export was read with best effort.')

        meta_state = self.file_states[META_FILE]
        if meta_state['state'] != 'ok':
            # The exporter writes export_meta.json last, after the JSONL files, so a root without a
            # readable one comes from an interrupted run (or an incomplete copy). Its data is still shown.
            detail = meta_state['detail'] + '. ' if meta_state['detail'] else ''
            self._check('export_meta', 'error', 'export_meta.json is %s' % meta_state['state'].replace('_', ' '),
                        detail + 'The exporter writes this file last, so the run was probably interrupted. '
                        'Scope, status, counts and failures are unavailable; the rest is shown as found.')

        counts_meta = meta.get('counts') if meta and isinstance(meta.get('counts'), dict) else {}
        for name in JSONL_FILES:
            state = self.file_states[name]
            if state['state'] == 'ok':
                continue
            count = counts_meta.get(name.split('.')[0])
            if state['state'] in ('too_large', 'unreadable'):
                level, detail = 'error', state['detail']
            elif state['state'] == 'absent':
                level = 'warning' if name != LIKES or self.meta is None else 'info'
                detail = 'Treated as having no rows.'
            else:  # empty
                level = 'warning' if (_is_int(count) and count > 0) or name == JOBS else 'info'
                detail = 'Treated as having no rows.'
            if _is_int(count) and state['state'] in ('absent', 'empty'):
                detail += ' export_meta.json reports %d.' % count
            self._check('file:' + name, level, '%s is %s' % (name, state['state'].replace('_', ' ')), detail,
                        [{'file': name, 'state': state['state'], 'detail': state['detail']}])

        status = meta.get('status') if meta else None
        if status == 'complete':
            self._check('status', 'ok', 'Export completed')
        elif status == 'completed_with_failures':
            self._check('status', 'warning', 'Export completed with failures', 'See the failure list.')
        elif status in PARTIAL_STATUS:
            self._check('status', 'warning', 'Export is partial (status: %s)' % status,
                        'The run did not finish; jobs, files and links may be incomplete.')
        elif status in RUN_STATES:
            self._check('status', 'warning', 'Export is incomplete (status: %s)' % status,
                        'The exporter only uses this status while a run is in progress and never writes it '
                        'when a run ends, so this export did not finish normally.')
        elif status is not None:
            self._check('status', 'warning', 'Unknown export status', display_name(str(status), 60))
        elif meta is not None:
            self._check('status', 'warning', 'Export status is missing')

        mode = self.scope.get('mode') if self.scope else None
        if meta is not None:
            feed = meta.get('feed_exhausted')
            if feed is False:
                if mode == 'max':
                    self._check('feed', 'info', 'Stopped at the selected maximum number of jobs',
                                'Older jobs were not read.')
                else:
                    self._check('feed', 'warning', 'The job feed was not read to the end',
                                'Jobs older than the last page read are not in this export.')
            elif feed is not True:
                self._check('feed', 'warning', 'Not recorded whether the job feed was read to the end',
                            'export_meta.json has no usable feed_exhausted value. The exporter leaves it out when '
                            'a run stops before the feed was read once, so more jobs may exist.')

        if self.scope:
            self._check('scope', 'info', 'Export scope', self._scope_text() + '.')
            if self.scope.get('testSubset') is True:
                self._check('test_subset', 'warning', 'Small test export: one moodboard and one profile at most',
                            'Small test was on, so only the first moodboard and the first profile were exported. '
                            'References to the others stay unresolved.')
        if self.selection is not None:
            boards, jobs = self.selection['moodboards'], self.selection['jobs']
            if (boards['available'] == 0 and jobs['available'] == 0 and not self.selection_contradicted
                    and not self.jobs and not self.boards):
                # A run that stops before it reads the feed writes this all-zero selection beside empty files.
                self._check('selection', 'info',
                            'The designer chose what to export, but this export has no moodboards or generations')
            else:
                self._check('selection', 'info', 'The designer chose what to export',
                            self._selection_text() or 'export_meta.json records the choice, but not its numbers.')
        if self.selection_problems:
            self._check('selection_invalid', 'warning', 'Some selection details are unreadable or do not add up',
                        "Values about the designer's selection are missing, not understood, or contradict each other "
                        'or the files. Each item says whether the viewer ignored the value or shows it as recorded.',
                        self.selection_problems)

        failures = meta.get('failures') if meta else None
        if isinstance(failures, list) and failures:
            items = [f if isinstance(f, dict) else {'value': f} for f in failures]
            self._check('failures', 'warning', '%d failure(s) reported by the exporter' % len(failures), '', items)
        elif isinstance(failures, list):
            self._check('failures', 'ok', 'No failures reported by the exporter')
        elif meta is not None and failures is not None:
            self._check('failures', 'warning', 'Failure list is not a list')

        observed = self._observed_counts()
        counts = meta.get('counts') if meta and isinstance(meta.get('counts'), dict) else None
        if counts is not None:
            pairs = [('jobs', observed['jobs']), ('moodboards', observed['moodboards']),
                     ('profiles', observed['profiles']), ('likes', observed['likes']),
                     ('files', observed['images_present'])]
            mismatches = [{'field': field, 'meta': counts.get(field), 'observed': value}
                          for field, value in pairs if _is_int(counts.get(field)) and counts.get(field) != value]
            if mismatches:
                self._check('counts', 'warning', 'Counts differ from export_meta.json',
                            "'files' is compared with referenced image files present in the export.", mismatches)
            else:
                self._check('counts', 'ok', 'Counts match export_meta.json')

        problems = self.parse_errors + self.row_problems
        if problems:
            self._check('parse_errors', 'warning', '%d unreadable or invalid row(s) skipped' % len(problems),
                        'Other rows loaded normally.', sorted(problems, key=lambda p: (p['file'], p['line'])))
        if self.duplicates:
            self._check('duplicates', 'warning', '%d duplicate id(s); the first row is used' % len(self.duplicates),
                        '', self.duplicates)
        if self.unknown:
            items = [{'file': name, 'field': field, 'rows': count}
                     for name in sorted(self.unknown) for field, count in sorted(self.unknown[name].items())]
            self._check('unknown_fields', 'info', 'Unknown fields preserved',
                        'Fields this viewer does not know are kept in each record\'s raw data.', items)
        if self.bad_dates:
            self._check('dates', 'info', '%d date value(s) could not be parsed' % len(self.bad_dates),
                        'Those records sort last and do not match date filters.', self.bad_dates)

        missing = self._missing_files()
        if missing:
            self._check('files_missing', 'warning', '%d referenced file(s) are missing' % len(missing),
                        'These images are listed in the metadata but are not in the export.', missing)
        unreadable = self._unreadable_files()
        if unreadable:
            self._check('files_unreadable', 'warning', '%d file(s) refused or unreadable' % len(unreadable),
                        'Unsafe names, links, encrypted or unsupported entries and non-image data are never served.',
                        unreadable)
        if self.unreferenced:
            self._check('files_unreferenced', 'info', '%d file(s) under images/ are not referenced' % len(self.unreferenced),
                        'They are not linked to any job or moodboard.', self.unreferenced)
        if not self.sniffed_all:
            self._check('files_unchecked', 'info', 'Image types are checked on demand',
                        'This export has more than %d files; unsupported files are reported when opened.' % self.limits.sniff_limit)

        if self.scope_images is False:
            kept = len(self.files) + len(self.unsafe_refs)
            self._check('metadata_only', 'info', 'Metadata-only export',
                        'Images were not included in this run; images without a file are shown as not exported.'
                        + (' %d image file(s) recorded by an earlier run are still listed and shown.' % kept
                           if kept else ''))
        elif (self.jobs or self.boards) and not self.files and not self.unsafe_refs:
            self._check('metadata_only', 'info', 'No image files in this export',
                        'No job or moodboard references an image file.')
        else:
            no_files = [job.id for job in self.jobs.values()
                        if job.slots and all(slot[1] is None for slot in job.slots) and not job.video]
            if no_files:
                self._check('jobs_without_files', 'warning' if self.scope_images else 'info',
                            '%d job(s) have no exported image files' % len(no_files),
                            'Typical for paused or stopped exports.', no_files)

        videos = [job.id for job in self.jobs.values() if job.video]
        if videos:
            self._check('video', 'info', '%d video job(s)' % len(videos), 'Video media is not exported.', videos)

        if self.unresolved_refs:
            self._check('unresolved_references', 'info',
                        '%d personalization reference(s) could not be linked' % len(self.unresolved_refs),
                        'They stay visible as unmatched codes and can be used as filters.', self.unresolved_refs)
        if self.left_out_refs:
            self._check('references_left_out', 'info',
                        '%d personalization reference(s) point to moodboards the designer left out' % len(self.left_out_refs),
                        'Those moodboards are not in this export, so the references are labelled left out, not unresolved.',
                        self.left_out_refs)

        absent = [b.id for b in self.boards.values() if b.generated_state == 'absent']
        invalid = [b.id for b in self.boards.values() if b.generated_state == 'invalid']
        dangling = [{'board_id': b.id, 'job_id': j} for b in self.boards.values() for j in b.unresolved_generated]
        omitted = [{'board_id': b.id, 'job_id': j} for b in self.boards.values() for j in b.omitted]
        if absent:
            self._check('generated_job_ids_absent', 'info', 'Moodboard generations derived from job references',
                        '%d moodboard row(s) have no generated_job_ids (older export); links come from explicit job references.' % len(absent),
                        absent)
        if invalid:
            self._check('generated_job_ids_invalid', 'warning', 'generated_job_ids is not a list',
                        'Links for these moodboards come from explicit job references.', invalid)
        if dangling:
            self._check('generated_job_ids_unresolved', 'warning', '%d generated job id(s) are not in this export' % len(dangling),
                        'Listed by a moodboard but absent from jobs.jsonl.', dangling)
        if omitted:
            self._check('generated_job_ids_omitted', 'warning', '%d explicit job reference(s) missing from generated_job_ids' % len(omitted),
                        'Shown as derived links.', omitted)
        if self.boards and not (absent or invalid or dangling or omitted):
            self._check('generated_job_ids', 'ok', 'Moodboard generation links are consistent')

        if self.gaps:
            unreported = [g for g in self.gaps if g['reported_by_exporter'] is False]
            if not self.gaps_listed:
                detail = 'export_meta.json has no parent_gaps list to compare them with.'
            elif unreported:
                detail = '%d of them are not listed in export_meta.parent_gaps.' % len(unreported)
            else:
                detail = 'All are listed in export_meta.parent_gaps.'
            self._check('parent_gaps', 'warning' if unreported else 'info',
                        '%d job(s) have a parent outside this export' % len(self.gaps), detail, self.gaps)
        if self.left_out_gaps:
            self._check('parent_gaps_left_out', 'info', '%d job(s) have a parent the designer left out' % len(self.left_out_gaps),
                        "The parent was in the export's scope, but the designer chose not to export it.", self.left_out_gaps)
        if self.stale_gaps:
            self._check('parent_gaps_stale', 'warning', 'export_meta.parent_gaps lists entries that do not apply',
                        '', self.stale_gaps)
        if self.cycle_members:
            self._check('lineage_cycle', 'warning', 'Parent links form a cycle',
                        'Lineage walks stop at the repeat.', self.cycle_members)
        self._building.sort(key=lambda c: LEVEL_ORDER[c['level']])
        self.checks = self._building

    def _scope_text(self) -> str:
        """Plain wording of export_meta.scope. max is only meaningful in 'max' mode, since in 'since' mode."""
        scope = self.scope or {}
        mode = scope.get('mode')
        if mode == 'all':
            parts = ['All jobs the own-job feed returned']
        elif mode == 'max':
            parts = ['The first %s jobs in feed order' % display_name(str(scope.get('max')), 20)]
        elif mode == 'since':
            parts = ['Jobs created on or after %s (00:00 UTC)' % display_name(str(scope.get('since') or '?'), 30)]
        else:
            parts = ['Scope mode %s' % display_name(str(mode), 20)]
        images = scope.get('images')
        parts.append('images included' if images is True else 'metadata only, no images'
                     if images is False else 'images setting not recorded')
        if scope.get('testSubset') is True:
            parts.append('small test on (one moodboard and one profile at most)')
        return '; '.join(parts)

    def _selection_text(self) -> str:
        """Plain wording of export_meta.selection; a number that was not readable is left out of it."""
        boards, jobs = self.selection['moodboards'], self.selection['jobs']
        chosen = ['%s of %s' % ('{:,}'.format(c['exported']), _things(c['available'], noun))
                  for c, noun in ((boards, 'moodboard'), (jobs, 'generation'))
                  if c['exported'] is not None and c['available'] is not None]
        sentences = [' and '.join(chosen) + '.'] if chosen else []
        left = [(n, noun) for n, noun in ((jobs['left_out'], 'generation'), (boards['left_out'], 'moodboard')) if n]
        clauses = []
        if left:
            clauses.append('%s %s left out' % (' and '.join(_things(n, noun) for n, noun in left),
                                               'was' if len(left) == 1 and left[0][0] == 1 else 'were'))
        elif (jobs['left_out'] == 0 and boards['left_out'] == 0 and not self.selection_contradicted
              and not (jobs['available'] == 0 and boards['available'] == 0)):
            # Not when selection_invalid says the record contradicts itself, nor when there was nothing to choose from.
            clauses.append('Nothing was left out')
        late = jobs['not_reviewed']
        if late:
            clauses.append('%s appeared after choosing and %s not exported' % (_things(late, 'generation'),
                                                                              'was' if late == 1 else 'were'))
        if clauses:
            sentences.append('; '.join(clauses) + '.')
        return ' '.join(sentences)

    def _missing_files(self) -> List[dict]:
        return [{'path': rec.rel, 'referenced_by': [_owner(o) for o in rec.referenced_by]}
                for rec in self.files.values() if rec.status == 'missing']

    def _unreadable_files(self) -> List[dict]:
        out = []
        for rec in self.files.values():
            if rec.status == 'unsupported':
                out.append({'path': display_name(rec.rel), 'reason': rec.reason})
        for ref in self.unsafe_refs:
            out.append({'path': ref['path'], 'reason': ref['reason']})
        seen = {item['path'] for item in out}
        for issue in list(self.view.archive_issues()) + list(self.listing_issues):
            if self.hidden is not None and self._hidden_file(issue.raw):
                continue
            if issue.path not in seen:
                out.append({'path': issue.path, 'reason': issue.reason})
                seen.add(issue.path)
        return out

    def _observed_counts(self) -> dict:
        present = [rec for rec in self.files.values() if rec.status == 'present']
        referenced = list(self.files.values())
        return {
            'jobs': len(self.jobs),
            'moodboards': len(self.boards),
            'profiles': len(self.profiles),
            'likes': len(self.likes),
            'images_expected': len(referenced) + len(self.unsafe_refs),
            'images_present': len(present),
            'images_missing': sum(1 for rec in referenced if rec.status == 'missing'),
            'images_unsupported': sum(1 for rec in referenced if rec.status == 'unsupported') + len(self.unsafe_refs),
            'images_unreferenced': len(self.unreferenced),
            'bytes_present': sum(rec.stat.size for rec in present),
        }

    # ---------------------------------------------------------- summary

    def _build_summary(self) -> None:
        meta = self.meta or {}
        export_time = iso_utc(parse_date(meta.get('export_time')))
        if self.root:
            label = self.root.rsplit('/', 1)[-1]
        else:
            name = self.source.get('name') or 'export'
            label = name[:-4] if name.lower().endswith('.zip') else name
        observed = self._observed_counts()
        self.summary = {
            'id': self.id,
            'label': label,
            'source': dict(self.source),
            'root': self.root,
            'extension_version': meta.get('extension_version') if isinstance(meta.get('extension_version'), str) else None,
            'version_support': self.version_support,
            'export_time': export_time,
            'username': meta.get('username') if isinstance(meta.get('username'), str) else None,
            'status': meta.get('status') if isinstance(meta.get('status'), str) else None,
            'counts': {k: observed[k] for k in SUMMARY_COUNTS},
            'warnings': sum(1 for c in self.checks if c['level'] == 'warning'),
            'errors': sum(1 for c in self.checks if c['level'] == 'error'),
        }

    # -------------------------------------------------------------- API

    def overview(self) -> dict:
        meta = self.meta
        observed = self._observed_counts()
        listing = lambda key: meta.get(key) if meta and isinstance(meta.get(key), list) else []  # noqa: E731
        out = {
            'summary': self.summary,
            'meta': meta,
            'scope': self.scope,
            'status': meta.get('status') if meta else None,
            'feed_exhausted': meta.get('feed_exhausted') if meta and isinstance(meta.get('feed_exhausted'), bool) else None,
            'counts': {'meta': meta.get('counts') if meta and isinstance(meta.get('counts'), dict) else None,
                       'observed': observed},
            'failures': listing('failures'),
            'parent_gaps': listing('parent_gaps'),
            'limitations': [x for x in listing('limitations') if isinstance(x, str)],
            'checks': self.checks,
            'facets': self.facets,
            'unresolved': {
                'references': self.unresolved_refs,
                'generated_job_ids': [{'board_id': b.id, 'job_id': j} for b in self.boards.values()
                                      for j in b.unresolved_generated],
                'parent_gaps': self.gaps,
            },
            'files': {
                'missing': self._missing_files(),
                'unreferenced': self.unreferenced,
                'unreadable': self._unreadable_files(),
            },
        }
        if self.selection is not None:  # only exports where the designer chose what to export carry one
            out['selection'] = self.selection
        return out

    def query_jobs(self, params: Dict[str, str]) -> dict:
        terms = [t.casefold() for t in (params.get('q') or '').split()]
        kind = params.get('kind') or ''
        version = params.get('version') or ''
        board = params.get('moodboard') or ''
        profile = params.get('profile') or ''
        links = params.get('links') or 'all'
        if links not in ('all', 'explicit'):
            raise QueryError('bad_parameter', "links must be 'all' or 'explicit'")
        date_from = _query_date(params.get('date_from'), 'date_from')
        date_to = _query_date(params.get('date_to'), 'date_to')
        sort = params.get('sort') or 'newest'
        if sort not in ('newest', 'oldest'):
            raise QueryError('bad_parameter', "sort must be 'newest' or 'oldest'")
        offset = _query_int(params.get('offset'), 'offset', 0, 0, 10 ** 9)
        limit = _query_int(params.get('limit'), 'limit', 60, 1, MAX_LIMIT)
        explicit_only = links == 'explicit'
        matched = []
        for job in (self.order_newest if sort == 'newest' else self.order_oldest):
            if terms and not all(term in job.search for term in terms):
                continue
            if kind and job.kind != kind:
                continue
            if version and job.version['value'] != version:
                continue
            if board:
                link = job.board_links.get(board)
                if link is None or (explicit_only and link[0] == 'inferred_code'):
                    continue
            if profile:
                if profile.startswith('code:'):  # the jobs on which this code matched nothing
                    if profile[5:] not in job.unmatched_codes:
                        continue
                else:
                    link = job.profile_links.get(profile)
                    if link is None or (explicit_only and link[0] == 'inferred_code'):
                        continue
            if date_from and (job.date is None or job.date < date_from):
                continue
            if date_to and (job.date is None or job.date > date_to):
                continue
            matched.append(job)
        page = matched[offset:offset + limit]
        return {'total': len(matched), 'offset': offset, 'limit': limit, 'items': [self._card(job) for job in page]}

    def job_detail(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        card = self._card(job)
        card.update({
            'full_command': job.full_command,
            'flags': job.flags,
            'flags_source': job.flags_source,
            'profile_default': job.profile_default,
            'unknown_flags': job.unknown_flags,
            'job_type': job.job_type,
            'event_type': job.event_type,
            'parent_index': job.parent_index,
            'parent_grid': job.parent_grid,
            'video': job.video,
            'image_likes': job.image_likes,
        })
        ancestors, cycle = self._ancestors(job)
        children = []
        for child_id in job.children:
            child = self._card(self.jobs[child_id])
            child['parent_index'] = self.jobs[child_id].parent_index
            children.append(child)
        detail = {
            'job': card,
            'raw': job.raw,
            'unknown_fields': job.unknown_fields,
            'images': card['images'],
            'lineage': {
                'ancestors': [self._card(self.jobs[a]) for a in ancestors],
                'parent': {'id': job.parent_id, 'status': job.parent_status, 'index': job.parent_index},
                'children': children,
                'descendant_count': self._descendants(job),
                'cycle': cycle or job.id in self.cycle_members,
            },
            'references': job.resolved_refs,
            'moodboards': [{'id': board_id, 'name': self.boards[board_id].name, 'link': link, 'evidence': evidence}
                           for board_id, (link, evidence) in job.board_links.items()],
            'profiles': [{'id': profile_id, 'name': self.profiles[profile_id].name, 'link': link, 'evidence': evidence}
                         for profile_id, (link, evidence) in job.profile_links.items()],
            'likes': [{'image_likes': like['image_likes'], 'line': like['line']}
                      for like in self.likes if like['job_id'] == job.id],
        }
        if job.parent_hidden:
            detail['raw_withheld'] = True  # the raw row lost the id of a job that is not available here
        return detail

    def _generation_counts(self, board: _Board) -> dict:
        counts = Counter(link for link, _ in board.links.values())
        return {'exporter': counts['exporter'], 'derived': counts['derived'], 'inferred_code': counts['inferred_code']}

    def _board_notes(self) -> List[str]:
        notes = ['Reference images are the pictures on the moodboard at export time, which may differ from the set '
                 'a job was made with. Generations are your jobs that used the moodboard; they are never mixed '
                 'with the references.',
                 "'exporter' links come from generated_job_ids, 'derived' links from a job's explicit "
                 "personalization reference id, and 'inferred_code' links only from a matching code.",
                 'generated_job_ids covers only jobs in this export; it is not a full usage history of the board.']
        if any(b.generated_state != 'present' for b in self.boards.values()):
            notes.append('Some moodboard rows have no generated_job_ids (older export); their generations are derived.')
        return notes

    def moodboards(self) -> dict:
        items = []
        for board in self.boards.values():
            refs = [self._image_ref(slot) for slot in board.slots]
            items.append({
                'id': board.id, 'name': board.name, 'code': board.code, 'codes': board.codes,
                'version': board.version, 'created_at': board.created_iso, 'created_at_raw': board.created_raw,
                'reference_count': len(refs),
                'references_present': sum(1 for r in refs if r['status'] == 'present'),
                'generations': self._generation_counts(board),
                'cover': [r for r in refs if r['status'] == 'present'][:4],
            })
            if board.generated_left_out is not None:
                items[-1]['generated_left_out'] = board.generated_left_out
        return {'items': items, 'notes': self._board_notes()}

    def moodboard_detail(self, board_id: str) -> Optional[dict]:
        board = self.boards.get(board_id)
        if board is None:
            return None
        generations = []
        order = {job.id: n for n, job in enumerate(self.order_newest)}
        for job_id, (link, evidence) in sorted(board.links.items(), key=lambda kv: order.get(kv[0], 0)):
            generations.append({'job': self._card(self.jobs[job_id]), 'link': link, 'evidence': evidence})
        notes = self._board_notes()
        left_out = board.generated_left_out
        if left_out:
            # One number covers both causes, so the note names both.
            notes.insert(0, "{:,} {}{} made with this moodboard {} not in this export because of the designer's "
                            'choice: left out, or made after choosing.'.format(
                                left_out, 'more ' if generations else '', 'generation' if left_out == 1 else 'generations',
                                'is' if left_out == 1 else 'are'))
        if board.generated_state == 'absent':
            notes.append('This moodboard row has no generated_job_ids; generations are derived from explicit job references.')
        if board.unresolved_generated:
            notes.append('%d generated job id(s) point to jobs that are not in this export.' % len(board.unresolved_generated))
        moodboard = {'id': board.id, 'name': board.name, 'code': board.code, 'codes': board.codes,
                     'version': board.version, 'created_at': board.created_iso, 'created_at_raw': board.created_raw}
        if board.generated_left_out is not None:
            moodboard['generated_left_out'] = board.generated_left_out
        detail = {
            'moodboard': moodboard,
            'raw': board.raw,
            'unknown_fields': board.unknown_fields,
            'references': [self._image_ref(slot) for slot in board.slots],
            'generations': generations,
            'unresolved_generated_job_ids': board.unresolved_generated,
            'notes': notes,
        }
        if board.raw_withheld:
            detail['raw_withheld'] = True  # ids or pictures of items not available here left the raw row
        return detail

    def profiles_list(self) -> dict:
        items = []
        for profile in self.profiles.values():
            items.append({
                'id': profile.id, 'name': profile.name, 'type': profile.type, 'version': profile.version,
                'service': profile.service, 'codes': profile.codes, 'ratings_count': profile.ratings_count,
                'created_at': profile.created_iso, 'created_at_raw': profile.created_raw,
                'raw': profile.raw, 'unknown_fields': profile.unknown_fields,
                'jobs': [{'job_id': job_id, 'link': link, 'evidence': evidence}
                         for job_id, (link, evidence) in profile.links.items()],
            })
        unmatched = [{'code': code, 'type': '/'.join(entry['types']), 'job_ids': entry['job_ids']}
                     for code, entry in sorted(self.unmatched.items())]
        notes = ['ratings_count is the aggregate number of visible ratings; individual ratings are not exported.',
                 "Global profiles ('global') are synthesized by the exporter from aggregate counts and usually have no codes, "
                 'so jobs link to them only through an explicit id.']
        if unmatched:
            notes.append('Unmatched codes come from job references or --profile flags that match no exported '
                         'moodboard or profile; they are listed as-is and never guessed.')
        return {'items': items, 'unmatched_codes': unmatched, 'notes': notes}

    def likes_list(self) -> dict:
        items = []
        if self.file_states[LIKES]['state'] in ('ok', 'empty'):
            for like in self.likes:
                job = self.jobs.get(like['job_id'])
                items.append({'job_id': like['job_id'], 'job': self._card(job) if job else None,
                              'image_likes': like['image_likes'], 'source': LIKES})
            note = 'Likes cover the exported own jobs only, not liked work by other creators.'
        else:
            note = 'likes.jsonl is %s; likes are taken from the jobs\' image_likes. ' \
                   'They cover the exported own jobs only.' % self.file_states[LIKES]['state'].replace('_', ' ')
        listed = {item['job_id'] for item in items}
        for job in self.jobs.values():
            if job.image_likes and job.id not in listed:
                items.append({'job_id': job.id, 'job': self._card(job), 'image_likes': job.image_likes, 'source': JOBS})
        return {'items': items, 'note': note}

    # ---------------------------------------------------- hidden items

    def _hidden_file(self, rel: Any) -> bool:
        """A path that only hidden items (or withheld pictures) use, or that the exporter named for a hidden item."""
        return rel in self.hidden.files or _named_for(rel, self.hidden)

    def hidden_view(self, closure: 'Closure') -> 'Hidden':
        """What this root leaves out for a closure (call it on the full index): the hidden jobs and moodboards, the
        reference pictures of visible boards that are hidden jobs' pictures, the files only those use, and the
        amounts export_meta.json counted for them."""
        jobs, boards = closure.jobs, closure.moodboards
        withheld = frozenset((board.id, slot[0]) for board in self.boards.values() if board.id not in boards
                             for slot in board.slots if _url_names(slot[3], jobs))

        def hidden_owner(kind: str, owner_id: str, index: int) -> bool:
            if kind == 'job':
                return owner_id in jobs
            return owner_id in boards or (owner_id, index) in withheld

        files = frozenset(rec.rel for rec in self.files.values()
                          if rec.referenced_by and all(hidden_owner(*owner) for owner in rec.referenced_by))
        present = [self.files[rel] for rel in files if self.files[rel].status == 'present']
        here = [self.jobs[job_id] for job_id in jobs if job_id in self.jobs]
        n_boards = sum(1 for board_id in boards if board_id in self.boards)
        return Hidden(jobs=jobs, moodboards=boards, files=files, withheld=withheld,
                      present={'jobs': len(here), 'moodboards': n_boards, 'images': len(present)},
                      delta={'jobs': len(here), 'moodboards': n_boards,
                             'completed_jobs': sum(1 for job in here if not job.video),
                             'likes': sum(1 for like in self.likes if like['job_id'] in jobs),
                             'files': len(present), 'download_bytes': sum(rec.stat.size for rec in present)})

    def without(self, hidden: 'Hidden') -> 'ExportIndex':
        """This root built again from the same view with the hidden items left out: the same id, file ids and
        label (Registry.open sets the label after building)."""
        index = ExportIndex(self.view, self.id, self.source, self.limits, hidden=hidden)
        index.summary['label'] = self.summary['label']
        if hasattr(self, 'source_real'):
            index.source_real = self.source_real
        return index

    def offspring(self) -> Dict[str, List[str]]:
        """parent id -> the ids of this root's jobs made from it, also when the parent is not in this root.
        Built once, on first use (the index does not change after loading)."""
        found = self._offspring
        if found is None:
            built: Dict[str, List[str]] = defaultdict(list)
            for job in self.jobs.values():
                if job.parent_id is not None and job.parent_id != job.id:
                    built[job.parent_id].append(job.id)
            self._offspring = found = dict(built)
        return found


def _owner(owner: tuple) -> dict:
    return {'kind': owner[0], 'id': owner[1], 'index': owner[2]}


def _query_date(value: Optional[str], name: str) -> Optional[str]:
    if not value:
        return None
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        raise QueryError('bad_parameter', '%s must be YYYY-MM-DD' % name)
    try:
        datetime(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise QueryError('bad_parameter', '%s is not a valid date' % name) from None
    return value


def _query_int(value: Optional[str], name: str, default: int, low: int, high: int) -> int:
    if value is None or value == '':
        return default
    if not re.match(r'^\d{1,10}$', value):
        raise QueryError('bad_parameter', '%s must be a non-negative integer' % name)
    number = int(value)
    if number < low or number > high:
        raise QueryError('bad_parameter', '%s must be between %d and %d' % (name, low, high))
    return number


def _json_error(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return '%s at column %d' % (exc.msg, exc.colno)
    if isinstance(exc, RecursionError):
        return 'nested too deeply'
    return display_name(str(exc), 80)


def _json_type(value: Any) -> str:
    if isinstance(value, list):
        return 'an array'
    if isinstance(value, str):
        return 'a string'
    if isinstance(value, bool):
        return 'a boolean'
    if value is None:
        return 'null'
    return 'a number'


# ------------------------------------------------------------ hidden items
#
# Hosted mode can hide jobs and moodboards of a published ZIP from everyone but the export's managers. The record
# keeps only ids; everything below is recomputed from the immutable full indexes of every export root of the ZIP.

Closure = namedtuple('Closure', 'job_ids board_ids jobs moodboards by_board key')
Closure.__doc__ = """What a hidden list hides in one ZIP. job_ids and board_ids are the listed ids; jobs adds every job
made with a listed moodboard, in any root (by_board: moodboard id -> those jobs); moodboards is board_ids. key
changes only when jobs or moodboards change, so two closures with one key leave out exactly the same."""

Hidden = namedtuple('Hidden', 'jobs moodboards files withheld present delta')
Hidden.__doc__ = """What one root leaves out (ExportIndex.hidden_view): the closure's jobs and moodboards, the files
(paths) only those use, the withheld (board id, slot index) reference pictures, what of it is present in this root
({jobs, moodboards, images}), and the amounts export_meta.json counted for it."""

_NO_RECORD = {'jobs': {}, 'moodboards': {}, 'counts': None, 'key': ''}
_BOARD_FILE = 'moodboard_'


def _closure_key(jobs: Iterable[str], boards: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps([sorted(jobs), sorted(boards)]).encode('ascii')).hexdigest()


def hidden_closure(roots: List['ExportIndex'], job_ids: Iterable[str], board_ids: Iterable[str]) -> Closure:
    """The closure of a hidden list over the full indexes of every root of one ZIP. A moodboard takes (a) every
    generation its page lists in each root that has it, all link kinds, (b) the ids of its generated_job_ids,
    which may name jobs of another root, and (c) in every root, the jobs whose reference id is its id (the
    exporter's own rule), which matters in a root that does not have the moodboard. Pure, no I/O."""
    job_ids, board_ids = frozenset(job_ids), frozenset(board_ids)
    by_board = {board_id: set() for board_id in board_ids}
    if by_board:
        for index in roots:
            for board_id, found in by_board.items():
                board = index.boards.get(board_id)
                if board is not None:
                    found.update(board.links)
                    found.update(board.generated or ())
            for job in index.jobs.values():
                for ref in job.references:
                    if ref['id'] in by_board:
                        by_board[ref['id']].add(job.id)
    jobs = frozenset(job_ids.union(*by_board.values()))
    by_board = {board_id: frozenset(found) for board_id, found in by_board.items()}
    return Closure(job_ids, board_ids, jobs, board_ids, by_board, _closure_key(jobs, board_ids))


EMPTY_CLOSURE = hidden_closure((), (), ())


def _url_names(url: Any, ids) -> bool:
    """Whether the first path segment of an address (on any host), as written or percent-decoded, is one of ids:
    the exporter keeps Midjourney's https://cdn.midjourney.com/<job id>/0_<i>.<ext>."""
    if not isinstance(url, str):
        return False
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    segment = (path[1:] if path.startswith('/') else path).split('/', 1)[0]
    return bool(segment) and (segment in ids or urllib.parse.unquote(segment) in ids)


_NAMED_INDEX = re.compile(r'_\d{1,3}\.')


def _named_for(rel: Any, hidden: Hidden) -> bool:
    """Whether rel is a file named for a hidden item: its last path segment starts with <job id>_<i>. or
    moodboard_<moodboard id>_<i>., the names the exporter gives their pictures (written or not, as a failure names
    the file it could not write), whatever follows: a partial download (.crdownload) or a second extension too."""
    if not isinstance(rel, str):
        return False
    name = rel.rsplit('/', 1)[-1]
    for match in _NAMED_INDEX.finditer(name):
        stem = name[:match.start()]
        if stem in hidden.jobs or (stem.startswith(_BOARD_FILE) and stem[len(_BOARD_FILE):] in hidden.moodboards):
            return True
    return False


def _names_hidden(value: Any, hidden: Hidden) -> bool:
    """Whether an id or path names a hidden job or moodboard, a file only they use, or one named for them."""
    found = _as_id(value)
    return found is not None and (found in hidden.jobs or found in hidden.moodboards or found in hidden.files
                                  or _named_for(found, hidden))


def _redact_meta(meta: dict, hidden: Hidden) -> Tuple[dict, dict]:
    """A copy of export_meta.json as if the hidden items had never been exported, and what each selection pair
    lost: parent gaps and failures that name them go, counts lose what they counted (only whole numbers, never
    below 0), each selection pair (available, exported) loses them when both are readable counts that stay 0 or
    more (so its difference and its checks are unchanged), and left_out_moodboard_ids loses hidden moodboards.
    Everything else, unknown keys included, is kept as written; meta itself is never changed."""
    out = dict(meta)
    reduced = {'jobs': 0, 'moodboards': 0}
    if isinstance(meta.get('parent_gaps'), list):
        out['parent_gaps'] = [gap for gap in meta['parent_gaps'] if not (isinstance(gap, dict) and (
            _as_id(gap.get('job_id')) in hidden.jobs or _as_id(gap.get('parent_id')) in hidden.jobs))]
    if isinstance(meta.get('failures'), list):
        out['failures'] = [item for item in meta['failures']
                           if not (isinstance(item, dict) and _names_hidden(item.get('id'), hidden))]
    if isinstance(meta.get('counts'), dict):
        counts = dict(meta['counts'])
        for key in HIDDEN_META_COUNTS:
            value, less = counts.get(key), hidden.delta[key]
            if less and _is_int(value) and value >= 0:
                counts[key] = max(0, value - less)
        out['counts'] = counts
    if isinstance(meta.get('selection'), dict):
        selection = dict(meta['selection'])
        for group in ('jobs', 'moodboards'):
            block, less = selection.get(group), hidden.delta[group]
            if not less or not isinstance(block, dict):
                continue
            available, exported = _count(block.get('available')), _count(block.get('exported'))
            if available is not None and exported is not None and available >= less and exported >= less:
                selection[group] = dict(block, available=available - less, exported=exported - less)
                reduced[group] = less
        if isinstance(selection.get('left_out_moodboard_ids'), list):
            selection['left_out_moodboard_ids'] = [value for value in selection['left_out_moodboard_ids']
                                                   if _as_id(value) not in hidden.moodboards]
        out['selection'] = selection
    return out, reduced


def _present(roots: List['ExportIndex'], job_id: str) -> bool:
    return any(job_id in index.jobs for index in roots)


def _board_name(roots: List['ExportIndex'], board_id: str) -> Optional[str]:
    """The moodboard's name in the first root that has it, else None."""
    for index in roots:
        board = index.boards.get(board_id)
        if board is not None:
            return board.name
    return None


def _made_from(roots: List['ExportIndex'], start: Iterable[str], hidden) -> int:
    """How many distinct jobs made from start (children, their children and so on, in any root) are not in
    hidden. A job id names the same generation in every root."""
    seen = set(start)
    queue = list(seen)
    count = 0
    while queue:
        parent = queue.pop()
        for index in roots:
            for child in index.offspring().get(parent, ()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                    if child not in hidden:
                        count += 1
    return count


def hide_preview(roots: List['ExportIndex'], closure: Optional[Closure], kind: str, item_id: str) -> dict:
    """What hiding one visible item would change, over every root of the ZIP and against the current list, for
    the dialogs. A job: {made_from_it}, the generations made from it that stay visible. A moodboard:
    {generations, made_from_them}, how many generations it would hide that are not hidden already, and how many
    generations made from those stay visible."""
    closure = closure or EMPTY_CLOSURE
    if kind == 'job':
        after = closure.jobs | {item_id}
    else:
        after = hidden_closure(roots, closure.job_ids, closure.board_ids | {item_id}).jobs
    new = [job_id for job_id in after - closure.jobs if _present(roots, job_id)]
    made_from = _made_from(roots, new, after)
    if kind == 'job':
        return {'made_from_it': made_from}
    return {'generations': len(new), 'made_from_them': made_from}


def hidden_note(roots: List['ExportIndex'], closure: Optional[Closure], record: Optional[dict], kind: str,
                item_id: str) -> dict:
    """Why a hidden item is hidden, for its managers' page. A job: {on_its_own: {hidden_at, hidden_by} or None,
    with_moodboards: [{id, name, hidden_at, hidden_by}]}. A moodboard: {hidden_at, hidden_by, generations,
    stay_hidden}: how many generations present in the ZIP it hides, and how many of those stay hidden when it is
    shown again (listed on their own or with another hidden moodboard)."""
    record, closure = record or _NO_RECORD, closure or EMPTY_CLOSURE
    if kind == 'job':
        own = record['jobs'].get(item_id)
        return {'on_its_own': dict(own) if own else None,
                'with_moodboards': [{'id': board_id, 'name': _board_name(roots, board_id),
                                     'hidden_at': who['hidden_at'], 'hidden_by': who['hidden_by']}
                                    for board_id, who in record['moodboards'].items()
                                    if item_id in closure.by_board.get(board_id, ())]}
    who = record['moodboards'].get(item_id) or {'hidden_at': None, 'hidden_by': None}
    present = [job_id for job_id in closure.by_board.get(item_id, ()) if _present(roots, job_id)]
    others = set(record['jobs']).union(*(found for board_id, found in closure.by_board.items() if board_id != item_id))
    return {'hidden_at': who['hidden_at'], 'hidden_by': who['hidden_by'], 'generations': len(present),
            'stay_hidden': sum(1 for job_id in present if job_id in others)}


def _newest_first(rows: List[dict]) -> List[dict]:
    """Newest hidden_at first, unknown last, then by id (the sort is stable)."""
    return sorted(sorted(rows, key=lambda row: row['id']), key=lambda row: row['hidden_at'] or '', reverse=True)


def hidden_listing(roots: List['ExportIndex'], closure: Optional[Closure], record: Optional[dict]) -> dict:
    """The Hidden section of the overview, over every root of the ZIP: the listed moodboards (each with how many
    generations present in the ZIP it hides) and the jobs hidden on their own. export_id is the first root that
    has the item, None when no root has it; such a row has no card or picture."""
    record, closure = record or _NO_RECORD, closure or EMPTY_CLOSURE
    boards = []
    for board_id, who in record['moodboards'].items():
        home = next((index for index in roots if board_id in index.boards), None)
        made_with = sum(1 for job_id in closure.by_board.get(board_id, ()) if _present(roots, job_id))
        row = {'id': board_id, 'export_id': None, 'name': None, 'code': None, 'codes': [], 'reference_count': 0,
               'cover': [], 'generations': made_with, 'hidden_at': who['hidden_at'], 'hidden_by': who['hidden_by']}
        if home is not None:
            board = home.boards[board_id]
            refs = [home._image_ref(slot) for slot in board.slots]
            row.update(export_id=home.id, name=board.name, code=board.code, codes=list(board.codes),
                       reference_count=len(refs), cover=[ref for ref in refs if ref['status'] == 'present'][:4])
        boards.append(row)
    jobs = []
    for job_id, who in record['jobs'].items():
        home = next((index for index in roots if job_id in index.jobs), None)
        jobs.append({'id': job_id, 'export_id': home.id if home is not None else None,
                     'job': home.card(job_id) if home is not None else None,
                     'hidden_at': who['hidden_at'], 'hidden_by': who['hidden_by'],
                     'with_moodboards': [{'id': board_id, 'name': _board_name(roots, board_id)}
                                         for board_id in record['moodboards']
                                         if job_id in closure.by_board.get(board_id, ())]})
    counts = record.get('counts')
    return {'counts': dict(counts) if counts else None, 'moodboards': _newest_first(boards),
            'jobs': _newest_first(jobs)}
