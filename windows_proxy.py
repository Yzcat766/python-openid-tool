"""Current-user Windows proxy and temporary CA trust, without PowerShell."""
import ctypes
from ctypes import wintypes
import hashlib
import os
import ssl
import subprocess
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import winreg

REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
PROXY_KEYS = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect")


def require_windows():
    if os.name != "nt":
        raise RuntimeError("自动设置系统代理和证书仅支持 Windows；其它平台请使用 --manual-proxy --skip-cert-install。")


def read_proxy():
    require_windows()
    result = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
        for name in PROXY_KEYS:
            try:
                value, kind = winreg.QueryValueEx(key, name)
                result[name] = {"value": value, "kind": kind}
            except FileNotFoundError:
                result[name] = None
    return result


def notify_proxy_changed():
    require_windows()
    api = ctypes.WinDLL("wininet", use_last_error=True).InternetSetOptionW
    api.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
    api.restype = ctypes.c_int
    for option in (39, 37):  # SETTINGS_CHANGED, REFRESH
        if not api(None, option, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())


def write_proxy(settings):
    require_windows()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        for name in PROXY_KEYS:
            item = settings.get(name)
            if item is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, item["kind"], item["value"])
    notify_proxy_changed()


def capture_proxy(original, port):
    current = original.get("ProxyOverride")
    bypass = current["value"] if current else ""
    bypass = ";".join(dict.fromkeys([item for item in bypass.split(";") if item] + ["localhost", "127.0.0.1", "<local>"]))
    return {
        "ProxyEnable": {"value": 1, "kind": 4},
        "ProxyServer": {"value": f"127.0.0.1:{port}", "kind": 1},
        "ProxyOverride": {"value": bypass, "kind": 1},
        "AutoConfigURL": None,
        "AutoDetect": {"value": 0, "kind": 4},
    }


def cert_thumbprint(path):
    pem = Path(path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha1(der).hexdigest().upper()


def cert_is_trusted(thumbprint):
    require_windows()
    return any(hashlib.sha1(cert).hexdigest().upper() == thumbprint for cert, encoding, trust in ssl.enum_certificates("ROOT") if encoding == "x509_asn")


def certutil(*args):
    require_windows()
    result = subprocess.run(["certutil.exe", "-user", *args], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        output = (result.stdout + result.stderr).decode(errors="replace")
        raise RuntimeError(f"证书操作失败：{output.strip()}")


def _process_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    api.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    return api


def _identity(api, handle, pid):
    name = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(name))
    times = [wintypes.FILETIME() for _ in range(4)]
    if not api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) or not api.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return {"pid": pid, "image": name.value.casefold(), "created": created}


def process_identity(pid):
    if os.name != "nt":
        return None
    api = _process_api()
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _identity(api, handle, pid)
    finally:
        api.CloseHandle(handle)


def stop_saved_process(expected):
    if not expected or os.name != "nt":
        return
    api = _process_api()
    handle = api.OpenProcess(0x1000 | 0x1 | 0x100000, False, expected["pid"])
    if not handle:
        if ctypes.get_last_error() == 87:  # exited process
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Match creation time and executable too, so PID reuse cannot kill another app.
        if _identity(api, handle, expected["pid"]) == expected:
            if not api.TerminateProcess(handle, 0):
                # The child may have exited between the identity check and termination.
                if api.WaitForSingleObject(handle, 0) != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
            if api.WaitForSingleObject(handle, 5000) != 0:
                raise RuntimeError("代理进程未在 5 秒内退出，请重试恢复。")
    finally:
        api.CloseHandle(handle)


@contextmanager
def exclusive_capture():
    """Prevent two copies of this standalone tool from changing the proxy at once."""
    if os.name != "nt":
        yield
        return
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    api.CreateMutexW.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    mutex = api.CreateMutexW(None, False, r"Local\CSUFTStandaloneOpenIdCapture")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        status = api.WaitForSingleObject(mutex, 0)
        if status not in (0, 0x80):  # signaled or abandoned
            raise RuntimeError("另一份 Python 捕获工具正在运行，请先结束该次捕获。")
        acquired = True
        yield
    finally:
        if acquired:
            api.ReleaseMutex(mutex)
        api.CloseHandle(mutex)
