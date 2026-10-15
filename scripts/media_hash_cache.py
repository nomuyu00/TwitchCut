"""Full SHA-256 results for unchanged local media; never imports caller digests.

Cache records are local performance hints, not a boundary against a malicious
user who can rewrite this workspace. --force-rehash audits content independently.
On Windows, cache hits require NTFS FILE_BASIC_INFO.ChangeTime and FILE_ID_INFO;
Python's Windows st_ctime may mean creation time, so it is never used for hits.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


@contextmanager
def _record_lock(path):
    """OS-released nonblocking lock; fail closed if another process owns it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        # Keep the lock file: unlinking it creates split-lock races on Unix.
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OSError(f'Media hash cache busy; retry after the current hash finishes: {path}') from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise OSError(f'Media hash cache busy; retry after the current hash finishes: {path}') from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _media_open(path):
    if os.name != 'nt':
        with path.open('rb') as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    text = str(path)
    if not text.startswith('\\\\?\\'):
        text = '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    # Deny write/delete sharing, including an already open writable file mapping.
    # Even a cache hit must reject a media file still being rendered or replaced.
    handle = create(text, 0x80000000, 0x00000001, None, 3, 0x08000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        yield stream


def _identity(stream):
    """Return a snapshot and whether the filesystem can safely supply cache hits."""
    value = os.fstat(stream.fileno())
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('Hash only regular media files')
    portable = {'backend': 'posix-stat-v1', 'device': value.st_dev, 'file_id': value.st_ino,
                'size': value.st_size, 'mtime_ns': value.st_mtime_ns, 'ctime_ns': value.st_ctime_ns}
    if os.name != 'nt':
        return portable, bool(value.st_ino and value.st_ctime_ns)

    import ctypes
    from ctypes import wintypes
    import msvcrt

    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ('creation', 'access', 'write', 'change')] + [('attributes', wintypes.DWORD)]

    class IdInfo(ctypes.Structure):
        _fields_ = [('volume', ctypes.c_ulonglong), ('identifier', ctypes.c_ubyte * 16)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    query.restype = wintypes.BOOL
    volume = kernel.GetVolumeInformationByHandleW
    volume.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       wintypes.LPWSTR, wintypes.DWORD)
    volume.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    basic, ident = BasicInfo(), IdInfo()
    filesystem = ctypes.create_unicode_buffer(261)
    ok_basic = query(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic))
    ok_id = query(handle, 18, ctypes.byref(ident), ctypes.sizeof(ident))
    ok_fs = volume(handle, None, 0, None, None, None, filesystem, len(filesystem))
    if not (ok_basic and ok_id and ok_fs and filesystem.value.upper() == 'NTFS'
            and basic.change > 0 and any(ident.identifier)):
        # No weaker Windows hit: unsupported filesystem/API => full read each time.
        portable['backend'] = 'windows-uncached-v1'
        return portable, False
    return {'backend': 'ntfs-handle-v1', 'volume': ident.volume,
            'file_id': bytes(ident.identifier).hex(), 'size': value.st_size,
            'creation_100ns': basic.creation, 'write_100ns': basic.write,
            'change_100ns': basic.change, 'attributes': basic.attributes}, True


def _full_hash(stream):
    digest = hashlib.sha256()
    count = 0
    for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
        digest.update(block)
        count += len(block)
    return digest.hexdigest(), count


def _check_path(path, identity):
    if os.name != 'nt':
        current = path.stat()
        if (current.st_dev, current.st_ino) != (identity['device'], identity['file_id']):
            raise ValueError(f'Media replaced during SHA-256 validation; hash rejected: {path}')


def _write_record(path, record):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('wb') as out:
            out.write(_canonical(record) + b'\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class MediaHashCache:
    def __init__(self, root, *, enabled=True, force=False):
        self.root = Path(root).resolve()
        self.directory = self.root / 'editing' / 'cache' / 'media-sha256-v1'
        self.enabled = enabled
        self.force = force
        self.stats = {'hits': 0, 'full_hashes': 0, 'bytes_read': 0,
                      'invalid_records': 0, 'bypassed': 0}

    def record_path(self, path):
        key = os.path.normcase(str(Path(path).resolve(strict=True)))
        return self.directory / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    def _load(self, record_path, key, identity):
        if not record_path.exists():
            return None
        try:
            if record_path.stat().st_size > 16 * 1024:
                raise ValueError('Oversized cache record')
            record = json.loads(record_path.read_text(encoding='utf-8'))
            signature = record.pop('record_sha256')
            if (set(record) != {'schema', 'path', 'identity', 'sha256', 'verified_at'}
                    or record['schema'] != 1 or record['path'] != key
                    or not isinstance(record['verified_at'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
                    or signature != hashlib.sha256(_canonical(record)).hexdigest()):
                raise ValueError('Invalid cache record')
            return record['sha256'] if record['identity'] == identity else None
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            self.stats['invalid_records'] += 1
            return None

    def hash(self, value):
        path = Path(value).resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError(f'Media cache path must stay inside workspace: {path}')
        key = os.path.normcase(str(path))
        record_path = self.record_path(path)
        if not self.enabled:
            with _media_open(path) as stream:
                return self._calculate(stream, path, None, key)
        with _record_lock(record_path.with_suffix('.lock')):
            with _media_open(path) as stream:
                before, cacheable = _identity(stream)
                if cacheable and not self.force:
                    digest = self._load(record_path, key, before)
                    if digest is not None:
                        after, still_cacheable = _identity(stream)
                        if after != before or not still_cacheable:
                            raise ValueError(f'Media changed during cached hash validation: {path}')
                        _check_path(path, after)
                        self.stats['hits'] += 1
                        return digest
                return self._calculate(stream, path, record_path if cacheable else None, key, before)

    def _calculate(self, stream, path, record_path, key, before=None):
        if before is None:
            before, _ = _identity(stream)
        digest, count = _full_hash(stream)
        self.stats['full_hashes'] += 1
        self.stats['bytes_read'] += count
        after, cacheable = _identity(stream)
        if before != after or count != before['size']:
            raise ValueError(f'Media changed during full SHA-256 read; hash rejected: {path}')
        # Detect a Unix pathname replaced while the original handle was read.
        _check_path(path, after)
        if record_path is not None and cacheable:
            record = {'schema': 1, 'path': key, 'identity': after, 'sha256': digest,
                      'verified_at': datetime.now(timezone.utc).isoformat()}
            record['record_sha256'] = hashlib.sha256(_canonical(record)).hexdigest()
            _write_record(record_path, record)
        else:
            self.stats['bypassed'] += 1
        return digest
