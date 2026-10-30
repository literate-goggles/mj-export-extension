#!/usr/bin/env python3
"""Offline acceptance check. Reads only the supplied export folder; no network."""
import collections
import hashlib
import json
from pathlib import Path
import struct
import sys

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
    raise ValueError('Unrecognized image signature')

jobs, boards, profiles, likes = map(rows, ['jobs.jsonl', 'moodboards.jsonl', 'profiles.jsonl', 'likes.jsonl'])
meta = json.loads((root / 'export_meta.json').read_text())
ids = {j['job_id'] for j in jobs}
assert len(ids) == len(jobs), 'Duplicate jobs'
missing_parents = [j['job_id'] for j in jobs if j['parent_id'] and j['parent_id'] not in ids]
assert len(missing_parents) == len(meta['parent_gaps']), 'Parent-gap report is inconsistent'
references = []
for job in jobs:
    if meta['scope']['images'] and not job.get('video'):
        assert len(job['image_files']) == len(job['image_urls']), f"Missing job files: {job['job_id']}"
    references.extend((f, job['width'], job['height']) for f in job['image_files'])
for board in boards:
    references.extend((i['file'], i['width'], i['height']) for i in board['images'] if i['file'])
checksums, types, dimension_mismatches = {}, collections.Counter(), []
for name, width, height in references:
    path = (root / name).resolve()
    assert path.is_relative_to(root), 'Path escapes export folder'
    data = path.read_bytes()
    assert data, f'Empty image: {name}'
    kind, actual = dimensions(data)
    assert path.suffix.lower() in ('.' + kind, '.jpg' if kind == 'jpeg' else '.' + kind), f'Wrong suffix: {name}'
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
    'image_types': dict(types), 'dimension_mismatches': dimension_mismatches,
    'image_bytes': sum((root / name).stat().st_size for name in checksums),
    'failures': meta['failures'], 'sha256': checksums,
}
print(json.dumps(result, indent=2))
