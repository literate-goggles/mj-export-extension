#!/usr/bin/env python3
"""Package an explicit source allowlist; never include personal exports or .git."""
from pathlib import Path
import json
import zipfile

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / 'manifest.json').read_text())
output = root / 'dist' / f"mj-export-extension-{manifest['version']}.zip"
output.parent.mkdir(exist_ok=True)
files = [root / name for name in ['manifest.json', 'package.json', 'README.md']]
for directory in ['src', 'lib', 'ui', 'docs', 'scripts', 'tests']:
    files.extend(p for p in (root / directory).rglob('*')
                 if p.is_file() and p.suffix in {'.js', '.mjs', '.html', '.css', '.md', '.py'})
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, str(Path('mj-export-extension') / path.relative_to(root)))
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    names = set(archive.namelist())
    resources = [manifest['background']['service_worker'], manifest['action']['default_popup']]
    resources += [name for script in manifest['content_scripts'] for name in script['js']]
    for resource in resources:
        assert 'mj-export-extension/' + resource in names, resource
    assert not any('/test-exports/' in name or '/.git/' in name for name in names)
print(output)
