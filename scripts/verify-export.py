#!/usr/bin/env python3
"""Offline acceptance check. Reads only the supplied export folder; no network."""
import collections
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import urllib.parse

root = Path(sys.argv[1]).resolve()

def rows(name):
    return [json.loads(line) for line in (root / name).read_text().splitlines() if line.strip()]

def dimensions(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', struct.unpack('>II', data[16:24])
    if data.startswith(b'\xff\xd8'):
        pos = 2
        while pos < len(data):
            if data[pos] != 255:
                pos += 1
                continue
            while pos < len(data) and data[pos] == 255:
                pos += 1
            marker = data[pos]
            pos += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            size = int.from_bytes(data[pos:pos+2], 'big')
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack('>HH', data[pos+3:pos+7])
                return 'jpeg', (width, height)
            if size < 2:
                break
            pos += size
        raise ValueError('JPEG has no readable dimensions')
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp', None
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif', struct.unpack('<HH', data[6:10])
    if len(data) >= 16 and data[4:8] == b'ftyp':
        box = int.from_bytes(data[0:4], 'big')
        end = min(len(data), box if box >= 16 else len(data))
        if {b'avif', b'avis'} & {data[8:12], *(data[i:i + 4] for i in range(16, end - 3, 4))}:
            return 'avif', None
    raise ValueError('Unrecognized image signature')

SUFFIXES = {'jpeg': ('.jpeg', '.jpg'), 'png': ('.png',), 'webp': ('.webp',), 'gif': ('.gif',), 'avif': ('.avif',)}
# 0.4.1 and later: moodboard pictures stored as Midjourney short links (the extension's isShortLink).
SHORT_LINK = re.compile(r'https://s\.mj\.run/[A-Za-z0-9_-]{1,64}')

jobs, boards, profiles, likes = map(rows, ['jobs.jsonl', 'moodboards.jsonl', 'profiles.jsonl', 'likes.jsonl'])
meta = json.loads((root / 'export_meta.json').read_text())
ids = {j['job_id'] for j in jobs}
assert len(ids) == len(jobs), 'Duplicate jobs'
missing_parents = [j['job_id'] for j in jobs if j['parent_id'] and j['parent_id'] not in ids]
assert len(missing_parents) == len(meta['parent_gaps']), 'Parent-gap report is inconsistent'
# 0.4.0 and later: what the designer chose (null, or absent before 0.4.0, when nothing was chosen).
selection = meta.get('selection')
if selection:
    assert selection['jobs']['exported'] == len(jobs), 'Selection job count differs from jobs.jsonl'
    assert selection['moodboards']['exported'] == len(boards), 'Selection moodboard count differs from moodboards.jsonl'
    for key, part in [('jobs', selection['jobs']), ('moodboards', selection['moodboards'])]:
        assert part['exported'] + part['left_out'] + part.get('not_reviewed', 0) <= part['available'], f'Selection counts exceed what was available: {key}'
    assert all(isinstance(b.get('generated_left_out'), int) for b in boards), 'A moodboard row has no generated_left_out'
    assert not {b['id'] for b in boards} & set(selection['left_out_moodboard_ids']), 'A left-out moodboard has a row'
references = []
for job in jobs:
    if meta['scope']['images'] and not job.get('video'):
        assert len(job['image_files']) == len(job['image_urls']), f"Missing job files: {job['job_id']}"
    references.extend((f, job['width'], job['height']) for f in job['image_files'])
failed_images = {f['id'] for f in meta['failures'] if f.get('kind') == 'image'}
short_links = collections.Counter()
for board in boards:
    references.extend((i['file'], i['width'], i['height']) for i in board['images'] if i['file'])
    for n, image in enumerate(board['images']):
        final = image.get('final_url')
        if final is not None:
            parts = urllib.parse.urlsplit(final)
            assert SHORT_LINK.fullmatch(image['source_url'] or '') and image['file'], f"final_url on a picture that is not a downloaded short link: {board['id']} {n}"
            assert parts.scheme == 'https' and parts.netloc and not parts.query and not parts.fragment, f'Unexpected final_url: {final}'
        if SHORT_LINK.fullmatch(image['source_url'] or ''):
            short_links['downloaded' if image['file'] else 'not_downloaded'] += 1
        if meta['scope']['images'] and meta['status'] in ('complete', 'completed_with_failures') and not image['file']:
            assert f"images/moodboard_{board['id']}_{n}.jpeg" in failed_images, f"Picture without a file or a failure: {board['id']} {n}"
checksums, types, dimension_mismatches, suffix_mismatches = {}, collections.Counter(), [], []
for name, width, height in references:
    path = (root / name).resolve()
    assert path.is_relative_to(root), 'Path escapes export folder'
    data = path.read_bytes()
    assert data, f'Empty image: {name}'
    kind, actual = dimensions(data)
    if path.suffix.lower() not in SUFFIXES[kind]:
        # Chrome names a file by the type the server sent, which can differ from the bytes.
        suffix_mismatches.append({'file': name, 'bytes': kind})
    types[kind] += 1
    if actual and width and height and actual != (width, height):
        dimension_mismatches.append({'file': name, 'api': [width, height], 'actual': list(actual)})
    checksums[name] = hashlib.sha256(data).hexdigest()
assert len(checksums) == meta['counts']['files'], 'Downloaded-file count differs from referenced files'
for key, values in [('jobs', jobs), ('moodboards', boards), ('profiles', profiles), ('likes', likes)]:
    assert len(values) == meta['counts'][key], f'Count mismatch: {key}'
result = {
    'status': meta['status'], 'counts': meta['counts'],
    'kinds': dict(collections.Counter(j['kind'] for j in jobs)),
    'parent_links': sum(bool(j['parent_id']) for j in jobs),
    'unresolved_parent_links': len(missing_parents),
    'left_out_parents': sum(bool(g.get('left_out')) for g in meta['parent_gaps']),
    'selection': selection,
    'image_types': dict(types), 'dimension_mismatches': dimension_mismatches, 'suffix_mismatches': suffix_mismatches,
    'short_links': dict(short_links),
    'image_bytes': sum((root / name).stat().st_size for name in checksums),
    'failures': meta['failures'], 'sha256': checksums,
}
print(json.dumps(result, indent=2))
