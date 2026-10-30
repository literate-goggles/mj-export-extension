"""Synthetic MJ Export fixtures for the viewer tests.

Everything here is invented: ids, prompts, codes and pictures. Images are tiny
real PNG/JPEG files built in code so decoders and magic-byte checks see
genuine formats. Never put data from a real export into this module.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import struct
import zipfile
import zlib
from typing import Dict, Iterable, List, Optional

ROOT = 'mj_export_synthetic_20260301'
USER_ID = 'synthetic-user-0001'
MISSING = object()

JPEG_8X8 = bytes.fromhex(
    'ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e121110'
    '1318281a181616183123251d283a333d3c3933383740485c4e404457453738506d51575f6267'
    '68673e4d71797064785c656763ffdb0043011112121815182f1a1a2f63423842636363636363'
    '6363636363636363636363636363636363636363636363636363636363636363636363636363'
    '636363636363ffc00011080008000803012200021101031101ffc4001f000001050101010101'
    '0100000000000000000102030405060708090a0bffc400b51000020103030204030505040400'
    '00017d01020300041105122131410613516107227114328191a1082342b1c11552d1f0243362'
    '7282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a'
    '636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6'
    'a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7'
    'e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f010003010101010101010101000000000000010203'
    '0405060708090a0bffc400b51100020102040403040705040400010277000102031104052131'
    '061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a'
    '262728292a35363738393a434445464748494a535455565758595a636465666768696a737475'
    '767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7'
    'b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9'
    'faffda000c03010002110311003f00a7451457b27927ffd9'
)

SVG_BYTES = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
             b'<script>document.title="x"</script></svg>')
TEXT_BYTES = b'this is plain text, not an image\n'


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)


@functools.lru_cache(maxsize=None)
def png_bytes(width: int = 4, height: int = 4, color: tuple = (200, 80, 40), noise: bool = False) -> bytes:
    """A valid RGB PNG. noise=True gives incompressible pixels (for thumbnail tests)."""
    raw = bytearray()
    state = 12345
    for _ in range(height):
        raw.append(0)
        if noise:
            for _ in range(width):
                state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                raw += bytes(((state >> 7) & 255, (state >> 13) & 255, (state >> 19) & 255))
        else:
            raw += bytes(color) * width
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + _chunk(b'IEND', b'')


def flags_dict(**updates) -> dict:
    flags = {'ar': None, 'v': None, 'stylize': None, 'chaos': None, 'weird': None, 'sref': [],
             'profile': [], 'niji': None, 'raw': False, 'tile': False, 'iw': None}
    flags.update(updates)
    return flags


def ref(kind: str, ref_id: Optional[str], code: Optional[str]) -> dict:
    """A personalization reference as the exporter stores it (id omitted when None)."""
    out = {'code': code}
    if ref_id is not None:
        out['id'] = ref_id
    out['type'] = kind
    out['user_id'] = USER_ID
    return out


def make_job(job_id: str, created_at, prompt: str, *, v: Optional[str] = '8.2', niji: Optional[str] = None,
             profile: Iterable[str] = (), kind: Optional[str] = 'grid', images: int = 4, ext: str = 'png',
             parent_id: Optional[str] = None, parent_index=None, parent_grid=None, job_type=MISSING,
             event_type: str = 'diffusion', refs: Iterable[dict] = (), image_likes: Iterable = (),
             video: bool = False, with_files: bool = True) -> dict:
    profile = list(profile)
    command = prompt + (' --v %s' % v if v else '') + (' --niji %s' % niji if niji else '')
    command += ''.join(' --profile %s' % code for code in profile)
    if job_type is MISSING:
        if niji:
            job_type = 'niji%s_diffusion' % niji
        elif v:
            major, _, minor = v.partition('.')
            job_type = 'v%s%s_diffusion' % (major, '-' + minor if minor and minor != '0' else '')
        else:
            job_type = None
    files = ['images/%s_%d.%s' % (job_id, i, ext) for i in range(images)] if with_files else []
    return {
        'job_id': job_id, 'created_at': created_at, 'prompt': prompt, 'full_command': command,
        'flags': flags_dict(v=v, niji=niji, profile=profile), 'profile_default': False, 'unknown_flags': {},
        'parent_id': parent_id, 'parent_index': parent_index, 'kind': kind, 'image_files': files,
        'image_urls': ['https://cdn.example.invalid/%s/0_%d.%s' % (job_id, i, ext) for i in range(images)],
        'width': 64, 'height': 64, 'job_type': job_type, 'event_type': event_type, 'parent_grid': parent_grid,
        'personalization_codes': list(refs), 'image_likes': list(image_likes), 'video': video,
    }


def make_board(board_id: str, name: str, code: Optional[str], *, generated=MISSING, images: int = 2,
               ext: str = 'png', created_at='2026-02-01 10:00:00.000000', with_files: bool = True) -> dict:
    row = {'id': board_id, 'code': code, 'codes': [code] if code else [], 'name': name, 'version': None,
           'created_at': created_at}
    if generated is not MISSING:
        row['generated_job_ids'] = list(generated)
    row['images'] = [{'source_url': 'https://cdn.example.invalid/boards/%s/%d.%s' % (board_id, i, ext),
                      'file': 'images/moodboard_%s_%d.%s' % (board_id, i, ext) if with_files else None,
                      'width': 4, 'height': 4} for i in range(images)]
    return row


def make_profile(profile_id: str, name: str, *, codes: Iterable[str] = (), kind: str = 'profile',
                 version: str = '8', ratings: int = 42, created_at=MISSING) -> dict:
    row = {'id': profile_id, 'codes': list(codes), 'name': name, 'version': version}
    if created_at is not MISSING:
        row['created_at'] = created_at
    row.update({'ratings_count': ratings, 'service': 'main', 'type': kind})
    return row


def jsonl(rows: List[dict]) -> bytes:
    return ''.join(json.dumps(r) + '\n' for r in rows).encode('utf-8')


class Export:
    """Rows plus file bytes; written as a folder or a ZIP by the helpers below."""

    def __init__(self, meta, jobs, boards, profiles, likes):
        self.meta = meta
        self.jobs = jobs
        self.boards = boards
        self.profiles = profiles
        self.likes = likes
        self.files: Dict[str, bytes] = {}      # overrides and extra files, by relative path
        self.raw: Dict[str, bytes] = {}        # raw metadata file bytes, by name
        self.absent = set()                    # metadata files (or images) to leave out
        self.skip_images = set()               # referenced images to leave out (missing files)

    def copy(self) -> 'Export':
        return copy.deepcopy(self)

    def referenced(self) -> List[str]:
        out = []
        for job in self.jobs:
            files = job.get('image_files')
            out += [f for f in files if isinstance(f, str)] if isinstance(files, list) else []
        for board in self.boards:
            images = board.get('images')
            out += [i['file'] for i in images if isinstance(i, dict) and isinstance(i.get('file'), str)] \
                if isinstance(images, list) else []
        return out

    def image_bytes(self, rel: str) -> bytes:
        if rel in self.files:
            return self.files[rel]
        if rel.endswith(('.jpeg', '.jpg')):
            return JPEG_8X8
        seed = sum(rel.encode()) % 200
        return png_bytes(4, 4, (seed, 255 - seed, 90))

    def sync_counts(self) -> 'Export':
        if isinstance(self.meta, dict):
            present = [r for r in _dedupe(self.referenced()) if r not in self.skip_images]
            self.meta['counts'] = {
                'jobs': len(self.jobs), 'moodboards': len(self.boards), 'profiles': len(self.profiles),
                'likes': len(self.likes), 'files': len(present), 'completed_jobs': len(self.jobs),
                'download_bytes': sum(len(self.image_bytes(r)) for r in present),
            }
        return self

    def members(self) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        if self.meta is not None:
            out['export_meta.json'] = (json.dumps(self.meta, indent=2) + '\n').encode('utf-8')
        out['jobs.jsonl'] = jsonl(self.jobs)
        out['moodboards.jsonl'] = jsonl(self.boards)
        out['profiles.jsonl'] = jsonl(self.profiles)
        out['likes.jsonl'] = jsonl(self.likes)
        out.update(self.raw)
        for rel in _dedupe(self.referenced()):
            if rel not in self.skip_images:
                out[rel] = self.image_bytes(rel)
        for rel, data in self.files.items():
            out.setdefault(rel, data)
        for name in self.absent:
            out.pop(name, None)
        return out


def _dedupe(items):
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def make_meta(*, version: Optional[str] = '0.3.1', status: str = 'complete', images: bool = True,
              mode: str = 'all', failures=(), parent_gaps=(), feed_exhausted: bool = True) -> dict:
    meta = {
        'export_time': '2026-03-20T12:00:00.000Z', 'username': 'synthetic_user',
        'scope': {'mode': mode, 'max': 20, 'since': '', 'images': images, 'testSubset': False},
        'status': status, 'counts': {}, 'failures': list(failures), 'parent_gaps': list(parent_gaps),
        'feed_exhausted': feed_exhausted,
        'limitations': ['Likes cover the selected own jobs only.'],
    }
    if version is not None:
        meta = dict({'extension_version': version}, **meta)
    return meta


def standard_export() -> Export:
    """Nine jobs, two moodboards, two profiles, one like. See test_model for the expected links."""
    jobs = [
        make_job('job-grid-a', '2026-03-10T12:00:00.000000+00:00', 'A Red fox in deep snow', v='8.2',
                 profile=['mbcodeA'], refs=[ref('moodboard', 'board-1', 'mbcodeA')]),
        make_job('job-grid-b', '2026-03-11T09:30:00+00:00', 'blue whale under ice', v='7.0', ext='jpeg',
                 profile=['pcode1'], refs=[ref('profile', 'profile-1', 'pcode1')], image_likes=[1]),
        make_job('job-grid-c', '2026-03-12T23:59:59+00:00', 'green FOX portrait', v='7',
                 profile=['ucode9'], refs=[ref('user', None, 'ucode9')]),
        make_job('job-grid-d', '2026-03-13 08:00:00', 'quiet harbor at dawn', v=None, job_type='v6-1_diffusion',
                 profile=['mbcodeB'], refs=[ref('moodboard', None, 'mbcodeB')]),
        make_job('job-niji', '2026-03-14T01:00:00+05:00', 'paper lanterns', v=None, niji='6'),
        make_job('job-var', '2026-03-15T10:00:00+02:00', 'A Red fox in deep snow', kind='variation',
                 event_type='variation', parent_id='job-grid-a', parent_index=2, parent_grid=2),
        make_job('job-up', '2026-03-16T10:00:00+00:00', 'A Red fox in deep snow', kind='upscale', images=1,
                 event_type='upscale', parent_id='job-var', parent_grid=1),
        make_job('job-gap', '2026-03-17T10:00:00+00:00', 'orphan detail', v='8.1', kind='upscale', images=1,
                 event_type='upscale', parent_id='job-not-exported', parent_index=0, parent_grid=0),
        make_job('job-flag-only', '2026-03-18T10:00:00+00:00', 'stone bridge in fog', v='8.1', ext='jpeg',
                 profile=['pcode1']),
    ]
    boards = [
        make_board('board-1', 'Synthetic board one', 'mbcodeA', generated=['job-grid-a'], images=2),
        make_board('board-2', 'Synthetic board two', 'mbcodeB', generated=[], images=1),
    ]
    profiles = [
        make_profile('profile-1', 'Synthetic profile', codes=['pcode1'], created_at=1767225600000),
        make_profile(USER_ID + '_7_main', 'Global V7 Profile', kind='global', version='7', ratings=10),
    ]
    likes = [{'job_id': 'job-grid-b', 'image_likes': [1]}]
    meta = make_meta(parent_gaps=[{'job_id': 'job-gap', 'parent_id': 'job-not-exported',
                                   'reason': 'Parent is outside the selected subset or unavailable in the own feed'}])
    export = Export(meta, jobs, boards, profiles, likes)
    export.files['images/job-grid-a_0.png'] = png_bytes(640, 480, noise=True)
    return export.sync_counts()


GALLERY_PALETTE = [(46, 94, 170), (214, 120, 52), (58, 150, 112), (176, 62, 92), (122, 98, 182), (226, 190, 70),
                   (70, 160, 190), (150, 84, 50), (96, 120, 70), (200, 90, 140), (40, 40, 60), (230, 220, 200)]
GALLERY_WORDS = ['amber', 'basalt', 'cobalt', 'drift', 'ember', 'fjord', 'glacier', 'harbor', 'indigo', 'juniper']
GALLERY_SUBJECTS = ['lighthouse', 'orchard', 'canyon', 'tram', 'kite', 'cello', 'meadow', 'glasshouse']
GALLERY_SIZES = [(64, 64), (96, 54), (54, 96), (80, 64)]


def gallery_job_id(i: int) -> str:
    return 'gjob-%03d' % i


def gallery_export(n_jobs: int = 150) -> Export:
    """A larger synthetic export for paging, lazy images and browser tests.

    Jobs are listed newest first, seven hours apart, from 2026-04-30T12:00Z back.
    Job i has the prompt '<word> <subject> study <iii>'; jobs 30-32 also start
    with 'zephyr'. Every 10th job (9, 19, ...) is V7, then V8.1 for i % 3 == 2,
    else V8.2. Lineage: gjob-006 (grid) -> gjob-005 (variation, image 2) ->
    gjob-004 (upscale, image 3); gjob-020 is an upscale whose parent is not
    exported. Board gboard-1 (5 references) lists gjob-000..002 in
    generated_job_ids; gboard-2 (3 references) has none. Jobs 3 and 7 use the
    custom profile gprofile-1; jobs 40-42 carry an unmatched 'user' code.
    """
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    jobs = []
    for i in range(n_jobs):
        job_id = gallery_job_id(i)
        created = (start - timedelta(hours=7 * i)).isoformat()
        prompt = '%s %s study %03d' % (GALLERY_WORDS[i % len(GALLERY_WORDS)],
                                       GALLERY_SUBJECTS[(i // len(GALLERY_WORDS)) % len(GALLERY_SUBJECTS)], i)
        if 30 <= i <= 32:
            prompt = 'zephyr ' + prompt
        v = '7' if i % 10 == 9 else '8.1' if i % 3 == 2 else '8.2'
        options = dict(v=v, kind='grid', images=4)
        if i <= 2:
            options.update(profile=['gcodeA'], refs=[ref('moodboard', 'gboard-1', 'gcodeA')])
        elif i in (3, 7):
            options.update(profile=['gpcode'], refs=[ref('profile', 'gprofile-1', 'gpcode')])
        elif 40 <= i <= 42:
            options.update(profile=['gucode'], refs=[ref('user', None, 'gucode')])
        if i == 5 and n_jobs > 6:
            options.update(kind='variation', event_type='variation', parent_id=gallery_job_id(6), parent_index=1,
                           parent_grid=1)
        elif i == 4 and n_jobs > 6:
            options.update(kind='upscale', event_type='upscale', images=1, parent_id=gallery_job_id(5),
                           parent_index=2, parent_grid=2)
        elif i == 20:
            options.update(kind='upscale', event_type='upscale', images=1, parent_id='gjob-not-exported',
                           parent_index=0, parent_grid=0)
        job = make_job(job_id, created, prompt, **options)
        job['width'], job['height'] = GALLERY_SIZES[i % len(GALLERY_SIZES)]
        jobs.append(job)
    boards = [
        make_board('gboard-1', 'Synthetic coast board', 'gcodeA', generated=[gallery_job_id(i) for i in range(min(3, n_jobs))],
                   images=5, created_at='2026-04-01 09:30:00.000000'),
        make_board('gboard-2', 'Synthetic studio board', 'gcodeB', generated=[], images=3,
                   created_at='2026-04-02 16:45:00.000000'),
    ]
    for board in boards:
        for n, image in enumerate(board['images']):
            image['width'], image['height'] = GALLERY_SIZES[n % len(GALLERY_SIZES)]
    profiles = [
        make_profile('gprofile-1', 'Synthetic custom profile', codes=['gpcode'], ratings=321, created_at=1775000000000),
        make_profile(USER_ID + '_7_main', 'Global V7 Profile', kind='global', version='7', ratings=12),
    ]
    gaps = [{'job_id': gallery_job_id(20), 'parent_id': 'gjob-not-exported',
             'reason': 'Parent is outside the selected subset or unavailable in the own feed'}] if n_jobs > 20 else []
    export = Export(make_meta(parent_gaps=gaps), jobs, boards, profiles, [])
    for n, rel in enumerate(export.referenced()):
        color = GALLERY_PALETTE[n % len(GALLERY_PALETTE)]
        export.files[rel] = png_bytes(6, 6, color)
    return export.sync_counts()


# ------------------------------------------------------------------ writers

def write_folder(export: Export, base: str, root: str = ROOT) -> str:
    """Write the export under base/root; returns that folder."""
    folder = os.path.join(base, *root.split('/')) if root else base
    for rel, data in export.members().items():
        path = os.path.join(folder, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(data)
    return folder


def zip_bytes(members: Dict[str, bytes], extra: Iterable = (), compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        for name_or_info, data in extra:
            archive.writestr(name_or_info, data)
    return buffer.getvalue()


def export_zip_bytes(export: Export, root: str = ROOT, extra: Iterable = (),
                     compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    prefix = root + '/' if root else ''
    return zip_bytes({prefix + rel: data for rel, data in export.members().items()}, extra, compression)


def flip_member_tail(data: bytes, name: str) -> bytes:
    """Flip one byte near the end of a stored member: it still starts like an image, and only reading it
    to its end (where zipfile checks the CRC) shows the damage."""
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(name)
    assert info.compress_type == zipfile.ZIP_STORED and info.file_size > 128, name
    buffer = bytearray(data)
    name_len, extra_len = struct.unpack_from('<HH', buffer, info.header_offset + 26)
    buffer[info.header_offset + 30 + name_len + extra_len + info.file_size - 10] ^= 0xFF
    return bytes(buffer)


def write_zip(export: Export, path: str, root: str = ROOT, extra: Iterable = ()) -> str:
    with open(path, 'wb') as handle:
        handle.write(export_zip_bytes(export, root, extra))
    return path


def patch_central_directory(data: bytes, name: str, *, flag_bits: Optional[int] = None,
                            compress_type: Optional[int] = None, file_size: Optional[int] = None) -> bytes:
    """Rewrite fields of one central-directory entry (to forge encrypted, odd or lying entries)."""
    buffer = bytearray(data)
    encoded = name.encode('utf-8')
    pos = 0
    while True:
        pos = buffer.find(b'PK\x01\x02', pos)
        if pos < 0:
            raise KeyError(name)
        name_len = struct.unpack_from('<H', buffer, pos + 28)[0]
        if bytes(buffer[pos + 46:pos + 46 + name_len]) == encoded:
            if flag_bits is not None:
                struct.pack_into('<H', buffer, pos + 8, flag_bits)
            if compress_type is not None:
                struct.pack_into('<H', buffer, pos + 10, compress_type)
            if file_size is not None:
                struct.pack_into('<L', buffer, pos + 24, file_size)
            return bytes(buffer)
        pos += 4


def symlink_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    return info
