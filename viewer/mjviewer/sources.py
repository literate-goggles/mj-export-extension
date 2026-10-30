"""Read-only access to MJ Export folders and ZIP archives.

Nothing in this module writes to an export. ZIP members are never extracted:
metadata is read through a streaming reader with a hard cap on the *actual*
decompressed size, and images are streamed in chunks. Folder paths are checked
with the same rules as ZIP member names and must resolve inside the export
root's real path, which blocks symlink escapes.
"""
from __future__ import annotations

import os
import re
import stat
import struct
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import BinaryIO, Dict, Iterator, List, Optional, Tuple

ROOT_MARKERS = ('export_meta.json', 'jobs.jsonl')
CHUNK_SIZE = 256 * 1024
_DRIVE = re.compile(r'^[A-Za-z]:')

ZIP_SUPPORTED = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
try:  # optional modules in some Python builds
    import bz2  # noqa: F401
    ZIP_SUPPORTED.add(zipfile.ZIP_BZIP2)
except ImportError:  # pragma: no cover
    pass
try:
    import lzma  # noqa: F401
    ZIP_SUPPORTED.add(zipfile.ZIP_LZMA)
except ImportError:  # pragma: no cover
    pass

IMAGE_MIME = {
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'avif': 'image/avif',
}


@dataclass(frozen=True)
class Limits:
    """Hard limits for untrusted input. Tests pass smaller values."""
    max_entries: int = 500_000                  # ZIP members, or files listed under images/
    max_central_directory: int = 128 * 1024 * 1024
    max_meta_bytes: int = 16 * 1024 * 1024      # export_meta.json
    max_jsonl_bytes: int = 256 * 1024 * 1024    # each *.jsonl
    max_image_bytes: int = 128 * 1024 * 1024    # largest image read whole (thumbnails)
    sniff_limit: int = 50_000                   # pre-check image types when an export has at most this many files
    max_scan_dirs: int = 2_000                  # folder root discovery
    max_scan_depth: int = 3


class SourceError(Exception):
    """The whole source is unusable (not a ZIP, corrupt, too many entries...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MemberError(Exception):
    """One file inside an export is missing, refused or unreadable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TooLarge(MemberError):
    pass


@dataclass(frozen=True)
class FileStat:
    size: int
    version: str  # CRC-32 for ZIP members, mtime_ns for folder files (cache keys)


@dataclass(frozen=True)
class Issue:
    path: str     # display-safe name
    code: str     # unsafe_name | symlink | not_regular | encrypted | compression | duplicate | escapes_root | unreadable | too_many
    reason: str
    raw: str = ''  # original name, for assigning archive issues to roots


def display_name(name: object, limit: int = 300) -> str:
    """Escape control and format characters so a hostile name renders as plain text."""
    text = name if isinstance(name, str) else repr(name)
    out = []
    for ch in text[:limit]:
        if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs'):
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    if len(text) > limit:
        out.append('…')
    return ''.join(out)


# Unicode categories Cc (C0, DEL, C1) and Cs (surrogates), as one class.
_UNSAFE_CHARS = re.compile('[\x00-\x1f\x7f-\x9f\ud800-\udfff]')
_DOT_SEGMENT = re.compile(r'(?:^|/)\.\.?(?:/|$)')


def unsafe_reason(name: object) -> Optional[str]:
    """Return why a relative path is unsafe, or None. Same rules for ZIP and folders."""
    if not isinstance(name, str):
        return 'path is not a string'
    if name == '':
        return 'empty path'
    if len(name) > 4096:
        return 'path too long'
    if _UNSAFE_CHARS.search(name):
        return 'control character in path'
    if '\\' in name:
        return 'backslash in path'
    if name.startswith('/'):
        return 'absolute path'
    if _DRIVE.match(name):
        return 'drive letter in path'
    if name.endswith('/') or '//' in name:
        return 'empty path segment'
    if _DOT_SEGMENT.search(name):
        return "'.' or '..' path segment"
    return None


def is_ignored(name: str) -> bool:
    """macOS archive metadata (__MACOSX/, AppleDouble ._*) and .DS_Store."""
    parts = name.split('/')
    return '__MACOSX' in parts or parts[-1].startswith('._') or parts[-1] == '.DS_Store'


def detect_image_type(head: bytes) -> Optional[str]:
    """Raster type from magic bytes: jpeg, png, gif, webp, avif, or None."""
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if len(head) >= 16 and head[4:8] == b'ftyp':
        box = int.from_bytes(head[0:4], 'big')
        end = min(len(head), box if box >= 16 else len(head))
        brands = [head[8:12]] + [head[i:i + 4] for i in range(16, end - 3, 4)]
        if b'avif' in brands or b'avis' in brands:
            return 'avif'
    return None


def describe_bytes(head: bytes) -> str:
    """Short, content-free description of an unsupported file for reports."""
    probe = head.lstrip()[:64].lower()
    if probe.startswith(b'<?xml') or b'<svg' in probe:
        return 'SVG/XML document'
    if probe.startswith(b'<'):
        return 'HTML/XML document'
    if head[4:8] == b'ftyp':
        return 'ISO media file that is not AVIF'
    if not head:
        return 'empty file'
    if all(32 <= b < 127 or b in (9, 10, 13) for b in head[:64]):
        return 'text file'
    return 'unknown binary data'


class _Stream:
    """A read-only stream wrapper whose close() runs under a lock."""

    def __init__(self, handle: BinaryIO, lock: threading.Lock):
        self._handle = handle
        self._lock = lock

    def read(self, n: int = -1) -> bytes:
        return self._handle.read(n)

    def close(self) -> None:
        with self._lock:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RootView:
    """One export root inside a source. Paths are relative to the root."""

    root: str = ''

    def stat(self, rel: str) -> Optional[FileStat]:
        raise NotImplementedError

    def open(self, rel: str):
        raise NotImplementedError

    def list_files(self, sub: str) -> Tuple[Dict[str, FileStat], List[Issue]]:
        raise NotImplementedError

    def archive_issues(self) -> List[Issue]:
        return []

    def iter_chunks(self, rel: str, chunk_size: int = CHUNK_SIZE,
                    cap: Optional[int] = None) -> Iterator[bytes]:
        """Yield the member's bytes. Never reads more than cap + 1 bytes."""
        stream = self.open(rel)
        try:
            total = 0
            while True:
                want = chunk_size if cap is None else max(1, min(chunk_size, cap - total + 1))
                try:
                    chunk = stream.read(want)
                except MemberError:
                    raise
                except Exception as exc:  # BadZipFile (CRC), zlib.error, OSError, EOFError...
                    raise MemberError('unreadable', 'read failed: %s' % _short(exc)) from None
                if not chunk:
                    return
                total += len(chunk)
                if cap is not None and total > cap:
                    raise TooLarge('too_large', 'larger than the %d byte limit' % cap)
                yield chunk
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover - closing a read stream
                pass

    def read_capped(self, rel: str, cap: int) -> bytes:
        info = self.stat(rel)
        if info is None:
            raise MemberError('missing', 'not found')
        if info.size > cap:  # declared size; the streaming cap below covers lying sizes
            raise TooLarge('too_large', 'larger than the %d byte limit' % cap)
        return b''.join(self.iter_chunks(rel, cap=cap))

    def head(self, rel: str, n: int = 64) -> bytes:
        """First n bytes (for magic-byte sniffing)."""
        stream = self.open(rel)
        try:
            data = b''
            while len(data) < n:
                try:
                    chunk = stream.read(n - len(data))
                except Exception as exc:
                    raise MemberError('unreadable', 'read failed: %s' % _short(exc)) from None
                if not chunk:
                    break
                data += chunk
            return data
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return display_name(text, 200)


# ---------------------------------------------------------------- ZIP sources

def _precheck_zip(path: str, size: int, limits: Limits) -> None:
    """Read the end-of-central-directory record before zipfile loads the directory."""
    try:
        with open(path, 'rb') as handle:
            tail_len = min(size, 65536 + 22)
            handle.seek(size - tail_len)
            tail = handle.read(tail_len)
            pos = tail.rfind(b'PK\x05\x06')
            if pos < 0 or len(tail) - pos < 22:
                return  # zipfile reports the precise error
            eocd_at = size - tail_len + pos
            fields = struct.unpack('<4s4H2LH', tail[pos:pos + 22])
            entries, cd_size, cd_offset = fields[4], fields[5], fields[6]
            if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
                found = False
                loc = pos - 20
                if loc >= 0 and tail[loc:loc + 4] == b'PK\x06\x07':
                    offset = struct.unpack('<4sLQL', tail[loc:loc + 20])[2]
                    if 0 <= offset < size:
                        handle.seek(offset)
                        rec = handle.read(56)
                        if len(rec) == 56 and rec[:4] == b'PK\x06\x06':
                            z64 = struct.unpack('<4sQ2H2L4Q', rec)
                            entries, cd_size, found = z64[7], z64[8], True
                if not found and (cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF):
                    raise SourceError('bad_zip', 'the archive end record is inconsistent; the archive is corrupt')
    except (OSError, struct.error):
        return
    if cd_size > eocd_at:
        raise SourceError('bad_zip', 'the archive directory is larger than the file; the archive is corrupt')
    if entries > limits.max_entries:
        raise SourceError('too_many_entries', 'archive declares %d entries; the limit is %d' % (entries, limits.max_entries))
    if cd_size > limits.max_central_directory:
        raise SourceError('too_many_entries', 'archive directory is %d bytes; the limit is %d' % (cd_size, limits.max_central_directory))


class ZipSource:
    kind = 'zip'

    def __init__(self, path: str, limits: Limits = Limits()):
        self.path = os.path.abspath(path)
        self.real_path = os.path.realpath(path)
        self.name = os.path.basename(self.path)
        self.limits = limits
        self.issues: List[Issue] = []
        self.ignored = 0
        self._lock = threading.Lock()
        self._members: Dict[str, zipfile.ZipInfo] = {}
        self._unreadable: Dict[str, Tuple[str, str]] = {}
        self._roots: Optional[List[str]] = None
        try:
            info = os.stat(self.real_path)
        except OSError as exc:
            raise SourceError('not_found', 'cannot read %s: %s' % (display_name(self.name), exc.strerror or exc)) from None
        if not stat.S_ISREG(info.st_mode):
            raise SourceError('not_a_file', '%s is not a regular file' % display_name(self.name))
        self.size = info.st_size
        self.mtime_ns = info.st_mtime_ns
        _precheck_zip(self.real_path, info.st_size, limits)
        try:
            self._zf: Optional[zipfile.ZipFile] = zipfile.ZipFile(self.real_path)
        except Exception as exc:  # BadZipFile, OSError, EOFError, struct.error, UnicodeDecodeError...
            raise SourceError('bad_zip', '%s is not a readable ZIP archive (%s)' % (display_name(self.name), _short(exc))) from None
        try:
            self._index(self._zf.infolist())
        except BaseException:
            self._zf.close()
            raise

    def _refuse(self, name: str, code: str, reason: str, keep: bool) -> None:
        self.issues.append(Issue(display_name(name), code, reason, name))
        if keep:  # a safe name we know about but cannot read
            self._unreadable.setdefault(name, (code, reason))

    def _index(self, infos: List[zipfile.ZipInfo]) -> None:
        if len(infos) > self.limits.max_entries:
            raise SourceError('too_many_entries', 'archive has %d entries; the limit is %d' % (len(infos), self.limits.max_entries))
        for info in infos:
            name = info.orig_filename
            mode = (info.external_attr >> 16) & 0xFFFF
            ftype = stat.S_IFMT(mode)
            if name.endswith('/') or ftype == stat.S_IFDIR:
                continue  # directory entries carry no data
            if is_ignored(name):
                self.ignored += 1
                continue
            reason = unsafe_reason(name)
            if reason is None and info.filename != name:
                reason = 'name altered by the ZIP reader'
            if reason:
                self._refuse(name, 'unsafe_name', 'unsafe entry name refused (%s)' % reason, keep=False)
                continue
            if name in self._members or name in self._unreadable:
                self._refuse(name, 'duplicate', 'duplicate entry name; the first entry is used', keep=False)
                continue
            if ftype == stat.S_IFLNK:
                self._refuse(name, 'symlink', 'symbolic link entry skipped', keep=True)
            elif ftype not in (0, stat.S_IFREG):
                self._refuse(name, 'not_regular', 'special file entry skipped', keep=True)
            elif info.flag_bits & 0x1:
                self._refuse(name, 'encrypted', 'encrypted entry cannot be read', keep=True)
            elif info.compress_type not in ZIP_SUPPORTED:
                self._refuse(name, 'compression', 'unsupported compression method %d' % info.compress_type, keep=True)
            else:
                self._members[name] = info

    def roots(self) -> List[str]:
        if self._roots is None:
            found = set()
            for name in list(self._members) + list(self._unreadable):
                parent, _, base = name.rpartition('/')
                if base in ROOT_MARKERS:
                    found.add(parent)
            self._roots = sorted(found)
        return list(self._roots)

    def view(self, root: str) -> 'ZipRootView':
        return ZipRootView(self, root)

    def open_member(self, name: str):
        info = self._members.get(name)
        if info is None:
            bad = self._unreadable.get(name)
            if bad:
                raise MemberError(bad[0], bad[1])
            raise MemberError('missing', 'not in the archive')
        with self._lock:
            if self._zf is None:
                raise MemberError('closed', 'archive is closed')
            try:
                handle = self._zf.open(info)
            except Exception as exc:  # local header mismatch, encryption, method...
                raise MemberError('unreadable', 'cannot open entry: %s' % _short(exc)) from None
        return _Stream(handle, self._lock)

    def close(self) -> None:
        with self._lock:
            if self._zf is not None:
                self._zf.close()
                self._zf = None


class ZipRootView(RootView):
    def __init__(self, source: ZipSource, root: str):
        self.source = source
        self.root = root
        self._prefix = root + '/' if root else ''
        self._issues: Optional[List[Issue]] = None

    def _name(self, rel: str) -> str:
        reason = unsafe_reason(rel)
        if reason:
            raise MemberError('unsafe_path', 'unsafe path refused (%s)' % reason)
        return self._prefix + rel

    def stat(self, rel: str) -> Optional[FileStat]:
        name = self._name(rel)
        info = self.source._members.get(name)
        if info is None:
            bad = self.source._unreadable.get(name)
            if bad:
                raise MemberError(bad[0], bad[1])
            return None
        return FileStat(info.file_size, '%08x' % info.CRC)

    def open(self, rel: str):
        return self.source.open_member(self._name(rel))

    def list_files(self, sub: str) -> Tuple[Dict[str, FileStat], List[Issue]]:
        prefix = self._prefix + sub.strip('/') + '/'
        cut = len(self._prefix)
        out = {}
        for name, info in self.source._members.items():
            if name.startswith(prefix):
                out[name[cut:]] = FileStat(info.file_size, '%08x' % info.CRC)
        return out, []

    def archive_issues(self) -> List[Issue]:
        """Archive issues under this root (paths made root-relative), plus those outside every root."""
        if self._issues is not None:
            return list(self._issues)
        roots = self.source.roots()
        mine = []
        for issue in self.source.issues:
            best = None
            for root in roots:
                prefix = root + '/' if root else ''
                if issue.raw.startswith(prefix) and (best is None or len(root) > len(best)):
                    best = root
            if best is None:
                mine.append(issue)
            elif best == self.root:
                rel = issue.raw[len(self._prefix):]
                mine.append(Issue(display_name(rel), issue.code, issue.reason, rel))
        self._issues = mine
        return list(mine)


# ------------------------------------------------------------- folder sources

class FolderSource:
    kind = 'folder'

    def __init__(self, path: str, limits: Limits = Limits()):
        self.path = os.path.abspath(path)
        self.real_path = os.path.realpath(path)
        self.name = os.path.basename(self.path.rstrip(os.sep)) or self.path
        self.limits = limits
        self.issues: List[Issue] = []
        self.ignored = 0
        if not os.path.isdir(self.real_path):
            raise SourceError('not_a_folder', '%s is not a folder' % display_name(self.name))
        try:
            self.mtime_ns = os.stat(self.real_path).st_mtime_ns
        except OSError as exc:
            raise SourceError('not_found', 'cannot read %s: %s' % (display_name(self.name), exc.strerror or exc)) from None

    def roots(self) -> List[str]:
        found = []
        queue = [(self.real_path, '', 0)]
        visited = 0
        while queue and visited < self.limits.max_scan_dirs:
            directory, rel, depth = queue.pop(0)
            visited += 1
            try:
                with os.scandir(directory) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            names = {e.name for e in entries}
            is_root = any(marker in names for marker in ROOT_MARKERS)
            if is_root:
                found.append(rel)
            if depth >= self.limits.max_scan_depth:
                continue
            for entry in entries:
                if entry.name.startswith('.') or entry.name == '__MACOSX':
                    continue
                if is_root and entry.name == 'images':
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child = entry.name if not rel else rel + '/' + entry.name
                queue.append((entry.path, child, depth + 1))
        return sorted(found)

    def view(self, root: str) -> 'FolderRootView':
        return FolderRootView(self, root)

    def close(self) -> None:
        pass


class FolderRootView(RootView):
    def __init__(self, source: FolderSource, root: str):
        self.source = source
        self.root = root
        base = os.path.join(source.real_path, *root.split('/')) if root else source.real_path
        self._real = os.path.realpath(base)

    def _inside(self, real: str) -> bool:
        return real == self._real or real.startswith(self._real.rstrip(os.sep) + os.sep)

    def _resolve(self, rel: str) -> str:
        reason = unsafe_reason(rel)
        if reason:
            raise MemberError('unsafe_path', 'unsafe path refused (%s)' % reason)
        real = os.path.realpath(os.path.join(self._real, *rel.split('/')))
        if not self._inside(real):
            raise MemberError('escapes_root', 'resolves outside the export folder')
        return real

    def stat(self, rel: str) -> Optional[FileStat]:
        real = self._resolve(rel)
        try:
            info = os.stat(real)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise MemberError('unreadable', exc.strerror or str(exc)) from None
        if not stat.S_ISREG(info.st_mode):
            raise MemberError('not_regular', 'not a regular file')
        return FileStat(info.st_size, str(info.st_mtime_ns))

    def open(self, rel: str):
        real = self._resolve(rel)
        flags = (os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        try:
            fd = os.open(real, flags)
        except (FileNotFoundError, NotADirectoryError):
            raise MemberError('missing', 'not found') from None
        except OSError as exc:
            raise MemberError('unreadable', exc.strerror or str(exc)) from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MemberError('not_regular', 'not a regular file')
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

    def list_files(self, sub: str) -> Tuple[Dict[str, FileStat], List[Issue]]:
        out: Dict[str, FileStat] = {}
        issues: List[Issue] = []
        top_rel = sub.strip('/')
        if unsafe_reason(top_rel):
            return out, issues
        top = os.path.join(self._real, *top_rel.split('/'))
        if not os.path.isdir(top) or os.path.islink(top):
            return out, issues
        stack = [(top, top_rel)]
        while stack:
            directory, rel_dir = stack.pop()
            try:
                with os.scandir(directory) as it:
                    entries = list(it)
            except OSError as exc:
                issues.append(Issue(display_name(rel_dir), 'unreadable', 'folder unreadable: %s' % (exc.strerror or exc), rel_dir))
                continue
            for entry in entries:
                rel = rel_dir + '/' + entry.name
                if is_ignored(rel):
                    continue
                reason = unsafe_reason(rel)
                if reason:
                    issues.append(Issue(display_name(rel), 'unsafe_name', 'unsafe file name skipped (%s)' % reason, rel))
                    continue
                try:
                    if entry.is_symlink():
                        real = os.path.realpath(entry.path)
                        if not self._inside(real):
                            issues.append(Issue(display_name(rel), 'escapes_root', 'symbolic link points outside the export folder', rel))
                            continue
                        info = os.stat(real)
                        if stat.S_ISDIR(info.st_mode):
                            continue  # never follow directory links
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, rel))
                        continue
                    else:
                        info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    issues.append(Issue(display_name(rel), 'unreadable', exc.strerror or str(exc), rel))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    issues.append(Issue(display_name(rel), 'not_regular', 'special file skipped', rel))
                    continue
                out[rel] = FileStat(info.st_size, str(info.st_mtime_ns))
                if len(out) > self.source.limits.max_entries:
                    issues.append(Issue(display_name(top_rel), 'too_many', 'more than %d files; listing stopped' % self.source.limits.max_entries, top_rel))
                    return out, issues
        return out, issues


def open_source(path: str, limits: Limits = Limits()):
    """Open a ZIP file or a folder. Raises SourceError with a user-facing message."""
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise SourceError('not_found', 'no file or folder at %s' % display_name(path))
    if os.path.isdir(expanded):
        return FolderSource(expanded, limits)
    if not os.path.isfile(expanded):
        raise SourceError('not_a_file', '%s is not a regular file or folder' % display_name(path))
    name = display_name(os.path.basename(expanded))
    try:  # zipfile.is_zipfile() reports an unreadable file as "not a ZIP"
        with open(expanded, 'rb') as handle:
            head = handle.read(4)
    except OSError as exc:
        raise SourceError('unreadable', 'cannot read %s: %s' % (name, exc.strerror or type(exc).__name__)) from None
    if not zipfile.is_zipfile(expanded):
        if head == b'PK\x03\x04':
            raise SourceError('bad_zip', '%s starts like a ZIP archive but has no end record: it is probably '
                                         'truncated (an interrupted copy or download)' % name)
        raise SourceError('not_a_zip', '%s is neither a ZIP archive nor a folder' % name)
    return ZipSource(expanded, limits)
