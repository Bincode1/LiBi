"""Handle-relative filesystem operations for Windows snapshots."""

from __future__ import annotations

import ctypes as c
import msvcrt
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

_kernel = c.WinDLL("kernel32", use_last_error=True)
_ntdll = c.WinDLL("ntdll")
_share_read_write = 3
_obj_case_insensitive = 0x0040
_obj_dont_reparse = 0x1000
_list_directory = 0x0001
_read_attributes = 0x0080
_delete = 0x00010000
_synchronize = 0x00100000
_generic_read = 0x80000000
_generic_write = 0x40000000
_directory_file = 0x00000001
_non_directory_file = 0x00000040
_synchronous_io = 0x00000020
_open_reparse_point = 0x00200000
_file_open = 1
_file_create = 2
_file_open_if = 3
_attribute_tag_info = 9
_full_directory_info = 14
_full_directory_restart_info = 15
_error_no_more_files = 18
_error_sharing_violation = 32
_file_rename_information_ex = 65
_replace_if_exists = 0x00000001
_ignore_readonly = 0x00000040
_file_disposition_info = 4
_file_disposition_information_ex = 64
_file_basic_info = 0
_file_standard_info = 1
_file_id_info = 18


class _UnicodeString(c.Structure):
    _fields_ = [("length", w.USHORT), ("maximum_length", w.USHORT), ("buffer", w.LPWSTR)]


class _ObjectAttributes(c.Structure):
    _fields_ = [
        ("length", w.ULONG),
        ("root", w.HANDLE),
        ("name", c.POINTER(_UnicodeString)),
        ("attributes", w.ULONG),
        ("security_descriptor", w.LPVOID),
        ("security_qos", w.LPVOID),
    ]


class _IOStatus(c.Structure):
    _fields_ = [("status", c.c_void_p), ("information", c.c_size_t)]


class _DirectoryEntry(c.Structure):
    _fields_ = [
        ("next", w.DWORD),
        ("index", w.DWORD),
        ("created", c.c_int64),
        ("accessed", c.c_int64),
        ("written", c.c_int64),
        ("changed", c.c_int64),
        ("size", c.c_int64),
        ("allocated", c.c_int64),
        ("attributes", w.DWORD),
        ("name_bytes", w.DWORD),
        ("ea_size", w.DWORD),
        ("name", w.WCHAR * 1),
    ]


class _RenameInformation(c.Structure):
    _fields_ = [
        ("flags", w.ULONG),
        ("directory", w.HANDLE),
        ("name_bytes", w.ULONG),
        ("name", w.WCHAR * 1),
    ]


class _BasicInformation(c.Structure):
    _fields_ = [
        ("created", c.c_int64),
        ("accessed", c.c_int64),
        ("written", c.c_int64),
        ("changed", c.c_int64),
        ("attributes", w.DWORD),
    ]


class _FileIdInformation(c.Structure):
    _fields_ = [("volume", c.c_uint64), ("identifier", c.c_ubyte * 16)]


class _StandardInformation(c.Structure):
    _fields_ = [
        ("allocated", c.c_int64),
        ("size", c.c_int64),
        ("links", w.DWORD),
        ("delete_pending", c.c_ubyte),
        ("directory", c.c_ubyte),
    ]


_kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
_kernel.CreateFileW.restype = w.HANDLE
_kernel.CloseHandle.argtypes = [w.HANDLE]
_kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]
_kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]
_ntdll.RtlNtStatusToDosError.argtypes = [w.LONG]
_ntdll.RtlNtStatusToDosError.restype = w.ULONG
_ntdll.NtCreateFile.argtypes = [
    c.POINTER(w.HANDLE),
    w.ULONG,
    c.POINTER(_ObjectAttributes),
    c.POINTER(_IOStatus),
    w.LPVOID,
    w.ULONG,
    w.ULONG,
    w.ULONG,
    w.ULONG,
    w.LPVOID,
    w.ULONG,
]
_ntdll.NtSetInformationFile.argtypes = [w.HANDLE, c.POINTER(_IOStatus), w.LPVOID, w.ULONG, w.ULONG]


def _check(status: int) -> None:
    if status < 0:
        raise c.WinError(_ntdll.RtlNtStatusToDosError(status))


def _attributes(handle: int) -> int:
    info = (w.DWORD * 2)()
    if not _kernel.GetFileInformationByHandleEx(handle, _attribute_tag_info, info, c.sizeof(info)):
        raise c.WinError(c.get_last_error())
    return info[0]


def _kind(attributes: int) -> str:
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "link"
    if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
        return "dir"
    return "file"


def _version(handle: int) -> tuple[int, ...]:
    identity = _FileIdInformation()
    if not _kernel.GetFileInformationByHandleEx(
        handle, _file_id_info, c.byref(identity), c.sizeof(identity)
    ):
        raise c.WinError(c.get_last_error())
    version = (identity.volume, int.from_bytes(bytes(identity.identifier), "little"))
    basic = _BasicInformation()
    if not _kernel.GetFileInformationByHandleEx(
        handle, _file_basic_info, c.byref(basic), c.sizeof(basic)
    ):
        raise c.WinError(c.get_last_error())
    if basic.attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
        return version
    standard = _StandardInformation()
    if not _kernel.GetFileInformationByHandleEx(
        handle, _file_standard_info, c.byref(standard), c.sizeof(standard)
    ):
        raise c.WinError(c.get_last_error())
    return (*version, basic.attributes, standard.size, basic.written, standard.links, basic.changed)


def _open_at(
    parent: int,
    name: str,
    access: int,
    directory: bool | None,
    disposition: int = _file_open,
    *,
    share: int = _share_read_write,
) -> int:
    _validate_component(name)
    buffer = c.create_unicode_buffer(name)
    length = c.sizeof(buffer) - c.sizeof(w.WCHAR)
    unicode_name = _UnicodeString(length, c.sizeof(buffer), c.cast(buffer, w.LPWSTR))
    # Case-insensitive lookup and rejection of reparses apply to this single component.
    attributes = _ObjectAttributes(
        c.sizeof(_ObjectAttributes),
        parent,
        c.pointer(unicode_name),
        _obj_case_insensitive | _obj_dont_reparse,
        None,
        None,
    )
    options = {True: _directory_file, False: _non_directory_file, None: 0}[directory]
    handle = w.HANDLE()
    status = _ntdll.NtCreateFile(
        c.byref(handle),
        access | _read_attributes | _synchronize,
        c.byref(attributes),
        c.byref(_IOStatus()),
        None,
        stat.FILE_ATTRIBUTE_NORMAL,
        share,
        disposition,
        options | _synchronous_io | _open_reparse_point,
        None,
        0,
    )
    _check(status)
    try:
        if _kind(_attributes(handle.value)) == "link":
            raise ValueError(f"protected link: {name}")
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    return handle.value


def _dispose(handle: int) -> None:
    if _attributes(handle) & stat.FILE_ATTRIBUTE_READONLY:
        # Delete this entry without changing attributes on a possible hard-link target.
        flags = w.ULONG(0x11)
        _check(
            _ntdll.NtSetInformationFile(
                handle,
                c.byref(_IOStatus()),
                c.byref(flags),
                c.sizeof(flags),
                _file_disposition_information_ex,
            )
        )
        return
    delete = w.BOOL(True)
    if not _kernel.SetFileInformationByHandle(
        handle, _file_disposition_info, c.byref(delete), c.sizeof(delete)
    ):
        raise c.WinError(c.get_last_error())


@contextmanager
def open_anchor(path: Path):
    handle = _kernel.CreateFileW(
        str(path), _list_directory | _read_attributes, _share_read_write, None, 3, 0x02200000, None
    )
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        if _kind(_attributes(handle)) != "dir":
            raise ValueError(f"invalid snapshot filesystem root: {path}")
        yield Directory(handle)
    finally:
        _kernel.CloseHandle(handle)


class Directory:
    def __init__(self, handle: int):
        self.handle = handle

    def mode(self) -> int:
        return 0o700

    def set_mode(self, mode: int) -> None:
        # Windows inherits directory ACLs from the destination.
        pass

    def version(self, name: str) -> tuple[int, ...] | None:
        try:
            handle = _open_at(self.handle, name, 0, None)
        except FileNotFoundError:
            return None
        try:
            return _version(handle)
        finally:
            _kernel.CloseHandle(handle)

    def entries(self) -> dict[str, str]:
        result = {}
        buffer = c.create_string_buffer(65536)
        info_class = _full_directory_restart_info
        while _kernel.GetFileInformationByHandleEx(
            self.handle, info_class, buffer, c.sizeof(buffer)
        ):
            info_class = _full_directory_info
            offset = 0
            while True:
                entry = _DirectoryEntry.from_buffer(buffer, offset)
                start = offset + _DirectoryEntry.name.offset
                name = buffer[start : start + entry.name_bytes].decode("utf-16-le", "surrogatepass")
                if name not in {".", ".."}:
                    result[name] = _kind(entry.attributes)
                if not entry.next:
                    break
                offset += entry.next
        error = c.get_last_error()
        if error != _error_no_more_files:
            raise c.WinError(error)
        return result

    @contextmanager
    def directory(
        self, name: str, *, create: bool = False, allow_mount: bool = False, exclusive=False
    ):
        # Windows mount points are reparses, including while opening a root path.
        del allow_mount
        disposition = _file_create if exclusive else (_file_open_if if create else _file_open)
        handle = _open_at(self.handle, name, _list_directory, True, disposition)
        try:
            yield Directory(handle)
        finally:
            _kernel.CloseHandle(handle)

    @contextmanager
    def read(self, name: str):
        handle = _open_at(self.handle, name, _generic_read, False)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, "rb") as source:
            yield source

    def remove(
        self, name: str, *, directory: bool = False, expected: tuple[int, ...] | None = None
    ) -> None:
        handle = _open_at(self.handle, name, _delete, directory)
        try:
            if expected is not None and _version(handle) != expected:
                raise OSError(f"workspace entry changed during restore: {name}")
            _dispose(handle)
        finally:
            _kernel.CloseHandle(handle)

    @contextmanager
    def lock(self, name: str):
        try:
            handle = _open_at(self.handle, name, _generic_read, False, _file_open_if, share=0)
        except OSError as exc:
            if exc.winerror != _error_sharing_violation:
                raise
            raise OSError("another snapshot operation is running") from exc
        try:
            yield
        finally:
            _kernel.CloseHandle(handle)

    @contextmanager
    def write(
        self,
        name: str,
        temporary: str,
        metadata: os.stat_result,
        expected: tuple[int, ...] | None,
        record: Callable[[tuple[int, ...]], None],
    ):
        _validate_component(name)
        handle = _open_at(
            self.handle, temporary, _generic_write | _delete, False, _file_create, share=0
        )
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, "wb") as target:
            installed = False
            try:
                yield target
                target.flush()
                attributes = stat.FILE_ATTRIBUTE_ARCHIVE
                if not metadata.st_mode & stat.S_IWRITE:
                    attributes |= stat.FILE_ATTRIBUTE_READONLY
                info = _BasicInformation(
                    0,
                    _filetime(metadata.st_atime_ns),
                    _filetime(metadata.st_mtime_ns),
                    0,
                    attributes,
                )
                if not _kernel.SetFileInformationByHandle(
                    handle, _file_basic_info, c.byref(info), c.sizeof(info)
                ):
                    raise c.WinError(c.get_last_error())
                if self.version(name) != expected:
                    raise OSError(f"workspace entry changed during restore: {name}")
                self._replace(handle, name, replace=expected is not None)
                installed = True
                record(_version(handle))
            finally:
                if not installed:
                    _dispose(handle)

    def _replace(self, handle: int, name: str, *, replace: bool) -> None:
        encoded = name.encode("utf-16-le", "surrogatepass")
        offset = _RenameInformation.name.offset
        buffer = c.create_string_buffer(max(c.sizeof(_RenameInformation), offset + len(encoded)))
        info = _RenameInformation.from_buffer(buffer)
        info.flags = _replace_if_exists | _ignore_readonly if replace else 0
        info.directory = self.handle
        info.name_bytes = len(encoded)
        buffer[offset : offset + len(encoded)] = encoded
        _check(
            _ntdll.NtSetInformationFile(
                handle, c.byref(_IOStatus()), buffer, c.sizeof(buffer), _file_rename_information_ex
            )
        )


def _filetime(nanoseconds: int) -> int:
    return nanoseconds // 100 + 116444736000000000


def _validate_component(name: str) -> None:
    if not name or name in {".", ".."} or any(char in name for char in "\\/:\0"):
        raise ValueError(f"invalid snapshot path component: {name!r}")
