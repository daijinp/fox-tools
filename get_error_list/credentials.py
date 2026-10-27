"""Optional Windows-user-bound encryption; never distribute saved credentials."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import uuid


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise OSError("保存登录信息仅支持 Windows。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    protect = crypt32.CryptProtectData
    unprotect = crypt32.CryptUnprotectData
    protect.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    unprotect.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    protect.restype = unprotect.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = DataBlob(len(data), buffer)
    outgoing = DataBlob()
    function = unprotect if decrypt else protect
    # Flag 1 = CRYPTPROTECT_UI_FORBIDDEN, current-user scope (no machine scope flag).
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, domain, directory=None):
        base = Path(directory) if directory is not None else Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        ) / "FoxAlarmExport"
        digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:16]
        self.path = base / f"credentials-{digest}.bin"
        self.domain = domain

    def load(self):
        if not self.path.exists():
            return None
        try:
            value = json.loads(_crypt(self.path.read_bytes(), decrypt=True).decode("utf-8"))
            if (not isinstance(value, dict) or value.get("domain") != self.domain
                    or not isinstance(value.get("username"), str)
                    or not isinstance(value.get("password"), str)):
                raise ValueError()
            return value["username"], value["password"]
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError("保存的登录信息无法读取，请重新输入或清除已保存信息。") from exc

    def save(self, username, password):
        data = json.dumps({"domain": self.domain, "username": username, "password": password},
                          ensure_ascii=False).encode("utf-8")
        encrypted = _crypt(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self):
        self.path.unlink(missing_ok=True)
