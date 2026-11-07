"""Windows security operations used by owner-gated Storage updates."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PROTECTED = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
ERROR_INSUFFICIENT_BUFFER = 122


class WindowsReplaceError(OSError):
    """A Windows replacement error that may require keeping its staged source."""

    def __init__(self, message: str, *, preserve_source: bool = False):
        super().__init__(message)
        self.preserve_source = preserve_source


def storage_owner_matches_current_user(root: Path) -> bool:
    """Compare the Storage directory owner SID with the current process user's SID."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    get_named_security_info = advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    get_named_security_info.restype = wintypes.DWORD
    owner_sid = wintypes.LPVOID()
    security_descriptor = wintypes.LPVOID()
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    equal_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    try:
        error = get_named_security_info(
            str(root),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner_sid),
            None,
            None,
            None,
            ctypes.byref(security_descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        if not owner_sid.value:
            raise OSError("Storage root has no owner SID.")
        _token_user, user_sid = _current_user_sid(advapi32, kernel32)
        return bool(equal_sid(owner_sid, user_sid))
    finally:
        if security_descriptor.value:
            local_free(security_descriptor)


def _current_user_sid(advapi32, kernel32):
    import ctypes
    from ctypes import wintypes

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    try:
        if not open_process_token(get_current_process(), TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        size = wintypes.DWORD()
        has_token_user = get_token_information(token, TOKEN_USER, None, 0, ctypes.byref(size))
        error = ctypes.get_last_error()
        if has_token_user or error != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            raise ctypes.WinError(error or 1)
        token_user = ctypes.create_string_buffer(size.value)
        if not get_token_information(token, TOKEN_USER, token_user, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        user_sid = ctypes.cast(token_user, ctypes.POINTER(wintypes.LPVOID)).contents
        return token_user, user_sid
    finally:
        if token.value:
            close_handle(token)


def create_private_temp_file(directory: Path, prefix: str, suffix: str) -> tuple[int, str]:
    """Create an empty temp file with a protected DACL granting only the current user."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Trustee(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", wintypes.LPVOID),
            ("MultipleTrusteeOperation", wintypes.DWORD),
            ("TrusteeForm", wintypes.DWORD),
            ("TrusteeType", wintypes.DWORD),
            ("ptstrName", wintypes.LPVOID),
        ]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", wintypes.DWORD),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", Trustee),
        ]

    class SecurityDescriptor(ctypes.Structure):
        _fields_ = [
            ("Revision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("Control", wintypes.WORD),
            ("Owner", wintypes.LPVOID),
            ("Group", wintypes.LPVOID),
            ("Sacl", wintypes.LPVOID),
            ("Dacl", wintypes.LPVOID),
        ]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _token_user, user_sid = _current_user_sid(advapi32, kernel32)
    acl = wintypes.LPVOID()
    set_entries_in_acl = advapi32.SetEntriesInAclW
    set_entries_in_acl.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(ExplicitAccess),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    set_entries_in_acl.restype = wintypes.DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    access = ExplicitAccess()
    access.grfAccessPermissions = 0x001F01FF  # FILE_ALL_ACCESS
    access.grfAccessMode = 1  # GRANT_ACCESS
    access.grfInheritance = 0  # NO_INHERITANCE
    access.Trustee.pMultipleTrustee = None
    access.Trustee.MultipleTrusteeOperation = 0
    access.Trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
    access.Trustee.TrusteeType = 1  # TRUSTEE_IS_USER
    access.Trustee.ptstrName = user_sid
    error = set_entries_in_acl(1, ctypes.byref(access), None, ctypes.byref(acl))
    if error:
        raise ctypes.WinError(error)

    try:
        descriptor = SecurityDescriptor()
        initialize = advapi32.InitializeSecurityDescriptor
        initialize.argtypes = [wintypes.LPVOID, wintypes.DWORD]
        initialize.restype = wintypes.BOOL
        set_owner = advapi32.SetSecurityDescriptorOwner
        set_owner.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL]
        set_owner.restype = wintypes.BOOL
        set_dacl = advapi32.SetSecurityDescriptorDacl
        set_dacl.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPVOID, wintypes.BOOL]
        set_dacl.restype = wintypes.BOOL
        set_control = advapi32.SetSecurityDescriptorControl
        set_control.argtypes = [wintypes.LPVOID, wintypes.WORD, wintypes.WORD]
        set_control.restype = wintypes.BOOL
        descriptor_pointer = ctypes.cast(ctypes.byref(descriptor), wintypes.LPVOID)
        if not initialize(descriptor_pointer, 1):
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        if not set_owner(descriptor_pointer, user_sid, False):
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        if not set_dacl(descriptor_pointer, True, acl, False):
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        if not set_control(descriptor_pointer, SE_DACL_PROTECTED, SE_DACL_PROTECTED):
            raise ctypes.WinError(ctypes.get_last_error() or 1)

        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes),
            descriptor_pointer,
            False,
        )
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        invalid_handle = ctypes.c_void_p(-1).value
        for _ in range(10):
            temporary = directory / f"{prefix}{secrets.token_hex(16)}{suffix}"
            handle = create_file(
                str(temporary),
                0x001F01FF,  # FILE_ALL_ACCESS
                0x00000001 | 0x00000002 | 0x00000004,  # FILE_SHARE_READ | WRITE | DELETE
                ctypes.byref(attributes),
                1,  # CREATE_NEW
                0x00000100,  # FILE_ATTRIBUTE_TEMPORARY
                None,
            )
            if handle is None or handle == invalid_handle:
                error = ctypes.get_last_error()
                if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                    continue
                raise ctypes.WinError(error or 1)
            try:
                descriptor_fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except OSError:
                close_handle(handle)
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return descriptor_fd, str(temporary)
        raise OSError("Could not allocate a private Windows temporary file.")
    finally:
        if acl.value:
            local_free(acl)


def copy_file_dacl(source: Path, destination: Path) -> None:
    """Copy the source's effective DACL as protected, without inheriting parent ACEs."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dacl = wintypes.LPVOID()
    security_descriptor = wintypes.LPVOID()

    get_named_security_info = advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    get_named_security_info.restype = wintypes.DWORD
    set_named_security_info = advapi32.SetNamedSecurityInfoW
    set_named_security_info.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    set_named_security_info.restype = wintypes.DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    try:
        error = get_named_security_info(
            str(source),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        error = set_named_security_info(
            str(destination),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        if security_descriptor.value:
            local_free(security_descriptor)


def _new_backup_path(destination: Path) -> Path:
    for _ in range(10):
        backup = destination.with_name(f".{destination.name}.pkm-backup-{secrets.token_hex(16)}")
        if not os.path.lexists(backup):
            return backup
    raise OSError("Could not allocate a Windows replacement backup path.")


def _call_replace_file(destination: Path, source: Path, backup: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(destination), str(source), str(backup), 0, None, None):
        error = ctypes.get_last_error() or 1
        raise ctypes.WinError(error)


def _restore_backup(backup: Path, destination: Path) -> None:
    os.replace(backup, destination)


def _remove_backup(backup: Path) -> None:
    try:
        backup.unlink()
    except PermissionError:
        os.chmod(backup, stat.S_IWRITE)
        backup.unlink()


def replace_file(source: Path, destination: Path) -> None:
    """Replace a file with an ACL-preserving backup and restore on partial failure."""

    backup = _new_backup_path(destination)
    try:
        _call_replace_file(destination, source, backup)
    except OSError as replace_error:
        if not os.path.lexists(backup):
            preserve_source = os.path.lexists(source)
            message = "Windows replacement failed"
            if preserve_source:
                message += f"; staged content remains at {source}"
            raise WindowsReplaceError(message, preserve_source=preserve_source) from replace_error
        if os.path.lexists(destination):
            preserve_source = os.path.lexists(source)
            message = f"Windows replacement failed; original-file backup remains at {backup}"
            if preserve_source:
                message += f" and staged content remains at {source}"
            raise WindowsReplaceError(message, preserve_source=preserve_source) from replace_error
        try:
            _restore_backup(backup, destination)
        except OSError as restore_error:
            preserve_source = os.path.lexists(source)
            message = (
                f"Windows replacement recovery failed; restore the original file from {backup} "
                f"to {destination}"
            )
            if preserve_source:
                message += f"; staged content remains at {source}"
            raise WindowsReplaceError(message, preserve_source=preserve_source) from restore_error
        raise

    if os.path.lexists(backup):
        try:
            _remove_backup(backup)
        except OSError as exc:
            raise OSError(
                f"Windows replacement succeeded, but the original-file backup remains at {backup}."
            ) from exc
