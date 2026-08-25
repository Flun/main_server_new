"""Direct Windows CMP 170HX unlock controller using the captured KMD ABI.

The low-level IOCTL protocol was captured from the locally supplied
ga100ctl.exe.  This controller still uses the supplied 170_boot driver package,
but does not execute ga100ctl.exe and does not need its password dialog.

Default invocation is read-only.  Hardware changes require --execute.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import struct
import subprocess
import sys
import time


BASE_DIR = Path(__file__).resolve().parent
TOOL_DIR = Path(r"D:\170_boot_v3")
INF_PATH = TOOL_DIR / "170_boot.inf"
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = LOG_DIR / "cmp170_direct_state.json"
LOG_PATH = LOG_DIR / "cmp170_direct_unlock.log"

TARGET_INSTANCE_PREFIX = r"PCI\VEN_10DE&DEV_20C2"
TARGET_HARDWARE_ID = r"PCI\VEN_10DE&DEV_20F1&SUBSYS_145F10DE"
TARGET_NVIDIA_DESCRIPTION = "NVIDIA A100-PCIE-40GB"
INTERFACE_GUID_TEXT = "3e15c21a-a02f-4d6a-9244-edb58cbe6e2a"

IOCTL_GA100_ABI = 0x00226004
IOCTL_GA100_RUN = 0x0022A000
ABI_VERSION = 18
EXPECTED_HASHES = {
    "170_boot.sys": "9A57777A24683C8AD4A8A3D0DD02487B0A8802F492C3F7B540384CFA6C54519F",
    "170_boot.cat": "9D5D432A8965839FB36E44613D163FDAC0C01C4A84C613C8B4996CD73498DDEC",
    "170_boot.inf": "E127C9466112DE20EE4669C7CF404A241E26FB188E29FD2479496542CE471045",
}

DIGCF_PRESENT = 0x00000002
DIGCF_ALLCLASSES = 0x00000004
DIGCF_DEVICEINTERFACE = 0x00000010
SPDIT_CLASSDRIVER = 0x00000001
DIOD_INHERIT_CLASSDRVS = 0x00000002
SPDRP_PHYSICAL_DEVICE_OBJECT_NAME = 0x0000000E
ERROR_NO_MORE_ITEMS = 259
ERROR_INSUFFICIENT_BUFFER = 122
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
CM_DISABLE_UI_NOT_OK = 0x00000002
CM_DISABLE_HARDWARE = 0x00000004
CM_DISABLE_BITS = CM_DISABLE_UI_NOT_OK | CM_DISABLE_HARDWARE


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> "GUID":
        import uuid

        raw = uuid.UUID(value).bytes_le
        result = cls()
        ctypes.memmove(ctypes.byref(result), raw, 16)
        return result


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class SP_DRVINFO_DATA_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("DriverType", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
        ("Description", wintypes.WCHAR * 256),
        ("MfgName", wintypes.WCHAR * 256),
        ("ProviderName", wintypes.WCHAR * 256),
        ("DriverDate", FILETIME),
        ("DriverVersion", ctypes.c_ulonglong),
    ]


DISPLAY_GUID = GUID.parse("4d36e968-e325-11ce-bfc1-08002be10318")
INTERFACE_GUID = GUID.parse(INTERFACE_GUID_TEXT)


def _configure_apis():
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    newdev = ctypes.WinDLL("newdev", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiCreateDeviceInfoList.restype = ctypes.c_void_p
    setupapi.SetupDiCreateDeviceInfoList.argtypes = [ctypes.POINTER(GUID), wintypes.HWND]
    setupapi.SetupDiOpenDeviceInfoW.restype = wintypes.BOOL
    setupapi.SetupDiOpenDeviceInfoW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiBuildDriverInfoList.restype = wintypes.BOOL
    setupapi.SetupDiBuildDriverInfoList.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD]
    setupapi.SetupDiEnumDriverInfoW.restype = wintypes.BOOL
    setupapi.SetupDiEnumDriverInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SP_DRVINFO_DATA_W)]
    setupapi.SetupDiGetDriverInfoDetailW.restype = wintypes.BOOL
    setupapi.SetupDiGetDriverInfoDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(SP_DRVINFO_DATA_W), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiSetSelectedDriverW.restype = wintypes.BOOL
    setupapi.SetupDiSetSelectedDriverW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(SP_DRVINFO_DATA_W)]
    setupapi.SetupDiDestroyDriverInfoList.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDriverInfoList.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD]
    setupapi.SetupCopyOEMInfW.restype = wintypes.BOOL
    setupapi.SetupCopyOEMInfW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(GUID), wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(SP_DEVINFO_DATA)]

    newdev.DiInstallDevice.restype = wintypes.BOOL
    newdev.DiInstallDevice.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(SP_DRVINFO_DATA_W), wintypes.DWORD, ctypes.POINTER(wintypes.BOOL)]
    newdev.DiUninstallDriverW.restype = wintypes.BOOL
    newdev.DiUninstallDriverW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.BOOL)]

    cfgmgr32.CM_Disable_DevNode.restype = wintypes.DWORD
    cfgmgr32.CM_Disable_DevNode.argtypes = [wintypes.DWORD, wintypes.DWORD]
    cfgmgr32.CM_Enable_DevNode.restype = wintypes.DWORD
    cfgmgr32.CM_Enable_DevNode.argtypes = [wintypes.DWORD, wintypes.DWORD]

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return setupapi, newdev, cfgmgr32, kernel32


def _logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("cmp170-direct")
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
        file_handler.setFormatter(fmt)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        log.addHandler(file_handler)
        log.addHandler(stream_handler)
    return log


LOG = _logger()


def acquire_operation_mutex():
    """Prevent a scheduled and manually requested unlock from overlapping."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "MainServer.CMP170HX.Unlock")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("다른 CMP 170HX 언락 작업이 이미 실행 중입니다")
    return kernel32, handle


def _win_error(label: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), label)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_package() -> None:
    for name, expected in EXPECTED_HASHES.items():
        path = TOOL_DIR / name
        if not path.is_file():
            raise RuntimeError(f"필수 드라이버 파일 없음: {path}")
        actual = _sha256(path)
        if actual != expected:
            raise RuntimeError(f"{name} SHA-256 불일치: {actual}")


class DeviceSet:
    def __init__(self, setupapi, handle, info: SP_DEVINFO_DATA, instance_id: str):
        self.setupapi = setupapi
        self.handle = handle
        self.info = info
        self.instance_id = instance_id
        self.driver_list_built = False

    def close(self) -> None:
        if self.driver_list_built:
            self.setupapi.SetupDiDestroyDriverInfoList(self.handle, ctypes.byref(self.info), SPDIT_CLASSDRIVER)
            self.driver_list_built = False
        if self.handle not in (None, INVALID_HANDLE_VALUE):
            self.setupapi.SetupDiDestroyDeviceInfoList(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_target_device(setupapi) -> DeviceSet:
    # Search every class.  Immediately after removing 170_boot Windows exposes
    # the same present devnode as an Unknown-class "3D video controller".
    handle = setupapi.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if handle == INVALID_HANDLE_VALUE:
        raise _win_error("SetupDiGetClassDevsW")
    index = 0
    try:
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setupapi.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_ITEMS:
                    break
                raise _win_error("SetupDiEnumDeviceInfo")
            index += 1
            buffer = ctypes.create_unicode_buffer(2048)
            if not setupapi.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), buffer, len(buffer), None):
                raise _win_error("SetupDiGetDeviceInstanceIdW")
            if buffer.value.upper().startswith(TARGET_INSTANCE_PREFIX):
                return DeviceSet(setupapi, handle, info, buffer.value)
    except Exception:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
        raise
    setupapi.SetupDiDestroyDeviceInfoList(handle)
    raise RuntimeError("PCI 10DE:20C2 CMP 170HX 장치를 찾지 못했습니다")


def open_device_instance_for_display(setupapi, instance_id: str) -> DeviceSet:
    """Open an exact devnode in a Display-class set, even when its driver is NULL.

    Keeping this set alive across DiUninstallDriver is also critical: once the
    temporary package is removed, rebuilding a class-driver list from the now
    Unknown-class devnode cannot find the forced A100 model.
    """
    # A Display-associated set rejects a NULL/Unknown-class devnode with class
    # mismatch.  Open it in an unassociated set, then supply Display as the
    # class context for building/selecting the class-driver list.  This mirrors
    # the selected-driver handoff retained by ga100ctl across package removal.
    handle = setupapi.SetupDiCreateDeviceInfoList(None, None)
    if handle == INVALID_HANDLE_VALUE:
        raise _win_error("SetupDiCreateDeviceInfoList(Display)")
    info = SP_DEVINFO_DATA()
    info.cbSize = ctypes.sizeof(info)
    if not setupapi.SetupDiOpenDeviceInfoW(handle, instance_id, None, 0, ctypes.byref(info)):
        setupapi.SetupDiDestroyDeviceInfoList(handle)
        raise _win_error("SetupDiOpenDeviceInfoW(CMP)")
    info.ClassGuid = DISPLAY_GUID
    return DeviceSet(setupapi, handle, info, instance_id)


def get_pdo_name(device: DeviceSet) -> str:
    reg_type = wintypes.DWORD()
    required = wintypes.DWORD()
    raw = (ctypes.c_ubyte * 2048)()
    if not device.setupapi.SetupDiGetDeviceRegistryPropertyW(
        device.handle,
        ctypes.byref(device.info),
        SPDRP_PHYSICAL_DEVICE_OBJECT_NAME,
        ctypes.byref(reg_type),
        raw,
        ctypes.sizeof(raw),
        ctypes.byref(required),
    ):
        raise _win_error("SetupDiGetDeviceRegistryPropertyW(PDOName)")
    return ctypes.wstring_at(ctypes.addressof(raw))


def current_driver(instance_id: str) -> dict:
    escaped = instance_id.replace("'", "''")
    command = (
        f"$d=Get-CimInstance Win32_PnPSignedDriver | Where-Object {{$_.DeviceID -eq '{escaped}'}} | "
        "Select-Object -First 1 DeviceName,InfName,DriverVersion,Manufacturer;"
        "$d | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"현재 GPU 드라이버 조회 실패: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def _detail_string(buffer, offset: int, chars: int) -> str:
    return ctypes.wstring_at(ctypes.addressof(buffer) + offset, chars).split("\0", 1)[0]


def enumerate_drivers(device: DeviceSet) -> list[dict]:
    if not device.setupapi.SetupDiBuildDriverInfoList(device.handle, ctypes.byref(device.info), SPDIT_CLASSDRIVER):
        raise _win_error("SetupDiBuildDriverInfoList")
    device.driver_list_built = True
    results: list[dict] = []
    index = 0
    while True:
        driver = SP_DRVINFO_DATA_W()
        driver.cbSize = ctypes.sizeof(driver)
        if not device.setupapi.SetupDiEnumDriverInfoW(device.handle, ctypes.byref(device.info), SPDIT_CLASSDRIVER, index, ctypes.byref(driver)):
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_ITEMS:
                break
            raise _win_error("SetupDiEnumDriverInfoW")
        index += 1
        size = 65536
        detail = ctypes.create_string_buffer(size)
        # sizeof(SP_DRVINFO_DETAIL_DATA_W) on Win64, including padding.
        ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = 1584
        required = wintypes.DWORD()
        ok = device.setupapi.SetupDiGetDriverInfoDetailW(
            device.handle,
            ctypes.byref(device.info),
            ctypes.byref(driver),
            detail,
            size,
            ctypes.byref(required),
        )
        if not ok and ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            raise _win_error("SetupDiGetDriverInfoDetailW")
        results.append(
            {
                "driver": driver,
                "description": driver.Description,
                "provider": driver.ProviderName,
                "manufacturer": driver.MfgName,
                "version": int(driver.DriverVersion),
                "section": _detail_string(detail, 32, 256),
                "inf": _detail_string(detail, 544, 260),
                "driver_description": _detail_string(detail, 1064, 256),
                "hardware_id": _detail_string(detail, 1576, max(1, (required.value - 1576) // 2)),
            }
        )
    return results


def find_driver(candidates: list[dict], *, provider: str | None = None, inf_name: str | None = None, description: str | None = None, hardware_id: str | None = None) -> dict:
    matches = []
    for item in candidates:
        if provider and item["provider"].casefold() != provider.casefold():
            continue
        if inf_name and Path(item["inf"]).name.casefold() != inf_name.casefold():
            continue
        if description and item["description"].casefold() != description.casefold():
            continue
        if hardware_id and not item["hardware_id"].upper().startswith(hardware_id.upper()):
            continue
        matches.append(item)
    if not matches:
        raise RuntimeError(
            f"드라이버 후보 없음 provider={provider!r} inf={inf_name!r} "
            f"description={description!r} hardware_id={hardware_id!r}"
        )
    matches.sort(key=lambda item: item["version"], reverse=True)
    return matches[0]


def install_selected(newdev, device: DeviceSet, selected: dict) -> bool:
    if not device.setupapi.SetupDiSetSelectedDriverW(device.handle, ctypes.byref(device.info), ctypes.byref(selected["driver"])):
        raise _win_error("SetupDiSetSelectedDriverW")
    reboot = wintypes.BOOL()
    if not newdev.DiInstallDevice(None, device.handle, ctypes.byref(device.info), ctypes.byref(selected["driver"]), 0x2, ctypes.byref(reboot)):
        raise _win_error("DiInstallDevice")
    return bool(reboot.value)


def stage_boot_driver(setupapi) -> str | None:
    # Avoid calling SetupCopyOEMInf at all when the package was approved and
    # staged previously.  Even a no-op call can invoke unsigned-driver policy
    # UI on some Windows builds.
    try:
        with open_target_device(setupapi) as device:
            existing = [item for item in enumerate_drivers(device) if item["provider"].casefold() == "170_boot"]
            if existing:
                published = Path(existing[0]["inf"]).name
                LOG.info("기존 170_boot 패키지 재사용: %s", published)
                return published
    except Exception as error:
        LOG.info("기존 170_boot package 조회 생략: %s", error)

    destination = ctypes.create_unicode_buffer(1024)
    required = wintypes.DWORD()
    ok = setupapi.SetupCopyOEMInfW(str(INF_PATH), None, 1, 0, destination, len(destination), ctypes.byref(required), None)
    if not ok:
        error = ctypes.get_last_error()
        # ERROR_FILE_EXISTS: the same package is already staged; enumerate it.
        if error != 80:
            raise _win_error("SetupCopyOEMInfW")
    return Path(destination.value).name if destination.value else None


def restart_device(instance_id: str) -> None:
    result = subprocess.run(
        ["pnputil.exe", "/restart-device", instance_id],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    LOG.info("pnputil restart rc=%s stdout=%r stderr=%r", result.returncode, result.stdout.strip(), result.stderr.strip())
    if result.returncode:
        raise RuntimeError(f"GPU 재시작 실패: {result.stderr or result.stdout}")


def find_interface_path(setupapi, expected_instance: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = setupapi.SetupDiGetClassDevsW(ctypes.byref(INTERFACE_GUID), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if handle != INVALID_HANDLE_VALUE:
            try:
                index = 0
                while True:
                    iface = SP_DEVICE_INTERFACE_DATA()
                    iface.cbSize = ctypes.sizeof(iface)
                    if not setupapi.SetupDiEnumDeviceInterfaces(handle, None, ctypes.byref(INTERFACE_GUID), index, ctypes.byref(iface)):
                        if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                            break
                        raise _win_error("SetupDiEnumDeviceInterfaces")
                    index += 1
                    required = wintypes.DWORD()
                    setupapi.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(iface), None, 0, ctypes.byref(required), None)
                    detail = ctypes.create_string_buffer(required.value)
                    ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                    info = SP_DEVINFO_DATA()
                    info.cbSize = ctypes.sizeof(info)
                    if not setupapi.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(iface), detail, required.value, None, ctypes.byref(info)):
                        raise _win_error("SetupDiGetDeviceInterfaceDetailW")
                    instance = ctypes.create_unicode_buffer(2048)
                    if not setupapi.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), instance, len(instance), None):
                        raise _win_error("SetupDiGetDeviceInstanceIdW(interface)")
                    path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                    if instance.value.casefold() == expected_instance.casefold():
                        return path
            finally:
                setupapi.SetupDiDestroyDeviceInfoList(handle)
        time.sleep(0.5)
    raise RuntimeError("170_boot 장치 인터페이스가 나타나지 않았습니다")


def _ioctl(kernel32, handle, code: int, input_bytes: bytes | None, output_size: int) -> bytes:
    input_buffer = ctypes.create_string_buffer(input_bytes) if input_bytes else None
    output_buffer = ctypes.create_string_buffer(output_size) if output_size else None
    returned = wintypes.DWORD()
    if not kernel32.DeviceIoControl(
        handle,
        code,
        input_buffer,
        len(input_bytes) if input_bytes else 0,
        output_buffer,
        output_size,
        ctypes.byref(returned),
        None,
    ):
        raise _win_error(f"DeviceIoControl(0x{code:08x})")
    return output_buffer.raw[: returned.value] if output_buffer else b""


def run_unlock_ioctls(kernel32, interface_path: str, pdo_name: str) -> tuple[list[int], list[int]]:
    handle = kernel32.CreateFileW(interface_path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE:
        raise _win_error("CreateFileW(170_boot interface)")
    try:
        before_raw = _ioctl(kernel32, handle, IOCTL_GA100_ABI, None, 96)
        if len(before_raw) != 96:
            raise RuntimeError(f"KMD ABI 크기 불일치: {len(before_raw)}")
        before = list(struct.unpack("<24I", before_raw))
        if before[0] != ABI_VERSION or before[6] != 0x10DE or before[7] != 0x20C2 or before[8] != 0x20C2:
            raise RuntimeError(f"KMD ABI/대상 불일치: {[hex(x) for x in before]}")
        encoded_pdo = (pdo_name + "\0").encode("utf-16le")
        if len(encoded_pdo) > 512:
            raise RuntimeError(f"PDO 이름이 너무 깁니다: {pdo_name}")
        payload = struct.pack(
            "<10I",
            ABI_VERSION,
            0x20C2,
            0,
            0x02779000,
            0x0000020B,
            0x88888888,
            0x00000008,
            0,
            0,
            2,
        ) + encoded_pdo.ljust(512, b"\0")
        if len(payload) != 552:
            raise AssertionError(len(payload))
        _ioctl(kernel32, handle, IOCTL_GA100_RUN, payload, 0)
        after_raw = _ioctl(kernel32, handle, IOCTL_GA100_ABI, None, 96)
        after = list(struct.unpack("<24I", after_raw))
        expected_tail = [0x02779000, 0x0000020B, 0x88888888, 0x00000008]
        if after[4] != 1 or after[5] != 1 or after[19:23] != expected_tail:
            raise RuntimeError(f"언락 후 ABI 검증 실패: {[hex(x) for x in after]}")
        return before, after
    finally:
        kernel32.CloseHandle(handle)


def nvidia_memory() -> int | None:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=pci.device_id,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) >= 2 and fields[0].upper() == "0X20C210DE":
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def write_state(**values) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    values["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, STATE_PATH)


def check_only() -> dict:
    verify_package()
    setupapi, _, _, _ = _configure_apis()
    with open_target_device(setupapi) as device:
        pdo = get_pdo_name(device)
        current = current_driver(device.instance_id)
        candidates = enumerate_drivers(device)
        nvidia = find_driver(
            candidates,
            inf_name=current.get("InfName"),
            description=TARGET_NVIDIA_DESCRIPTION,
            hardware_id=TARGET_HARDWARE_ID,
        )
        boot_candidates = [item for item in candidates if item["provider"].casefold() == "170_boot"]
        return {
            "instance_id": device.instance_id,
            "pdo_name": pdo,
            "current_driver": current,
            "memory_mib": nvidia_memory(),
            "nvidia_restore": {key: nvidia[key] for key in ("inf", "section", "description", "provider", "hardware_id", "version")},
            "staged_170_boot_candidates": [
                {key: item[key] for key in ("inf", "section", "description", "provider", "hardware_id", "version")}
                for item in boot_candidates[:5]
            ],
        }


def stage_once() -> int:
    """Stage the unsigned boot package once and intentionally keep it installed."""
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("관리자 권한이 필요합니다")
    verify_package()
    setupapi, _, _, _ = _configure_apis()
    published = stage_boot_driver(setupapi)
    # SetupCopyOEMInf can return ERROR_FILE_EXISTS without the published name.
    # Discover the exact package through the target's class driver list.
    with open_target_device(setupapi) as device:
        candidates = enumerate_drivers(device)
        boot = find_driver(candidates, provider="170_BOOT", inf_name=published)
        published = Path(boot["inf"]).name
    LOG.info("170_boot 1회 stage 완료 published=%s (패키지 유지)", published)
    write_state(status="staged", temporary_inf=published)
    return 0


def recover_nvidia(instance_id: str, inf_name: str) -> int:
    """Force the known A100 node back onto a NULL/Unknown-class CMP devnode."""
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("관리자 권한이 필요합니다")
    setupapi, newdev, cfgmgr32, _ = _configure_apis()
    LOG.warning("NVIDIA 긴급 복구 시작 instance=%s inf=%s", instance_id, inf_name)
    with open_device_instance_for_display(setupapi, instance_id) as device:
        candidates = enumerate_drivers(device)
        nvidia = find_driver(
            candidates,
            inf_name=inf_name,
            description=TARGET_NVIDIA_DESCRIPTION,
            hardware_id=TARGET_HARDWARE_ID,
        )
        LOG.info("긴급 복구 후보 inf=%s section=%s hardware=%s", nvidia["inf"], nvidia["section"], nvidia["hardware_id"])
        install_selected(newdev, device, nvidia)
        cr = cfgmgr32.CM_Enable_DevNode(device.info.DevInst, 0)
        LOG.info("긴급 복구 CM_Enable_DevNode cr=%s", cr)
    for _ in range(45):
        memory = nvidia_memory()
        if memory is not None:
            LOG.info("NVIDIA 긴급 복구 완료 memory=%s MiB", memory)
            write_state(status="recovered", memory_mib=memory, instance_id=instance_id, inf_name=inf_name)
            return 0
        time.sleep(1)
    raise RuntimeError("NVIDIA 드라이버를 바인딩했지만 nvidia-smi에서 CMP를 찾지 못했습니다")


def execute() -> int:
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("관리자 권한이 필요합니다")
    verify_package()
    setupapi, newdev, cfgmgr32, kernel32 = _configure_apis()
    memory = nvidia_memory()
    if memory and memory >= 60000:
        LOG.info("이미 언락 상태입니다: %s MiB", memory)
        write_state(status="already_unlocked", memory_mib=memory)
        return 0

    temporary_inf: str | None = None
    original: dict | None = None
    instance_id: str | None = None
    restore_succeeded = False
    restore_device: DeviceSet | None = None
    restore_driver: dict | None = None
    write_state(status="starting", memory_mib=memory)
    try:
        with open_target_device(setupapi) as device:
            instance_id = device.instance_id
            pdo_name = get_pdo_name(device)
            original = current_driver(instance_id)
            if original.get("DeviceName") != TARGET_NVIDIA_DESCRIPTION or not str(original.get("InfName", "")).lower().startswith("oem"):
                raise RuntimeError(f"예상한 A100 복구 드라이버가 아닙니다: {original}")
            LOG.info("대상=%s PDO=%s 원본=%s", instance_id, pdo_name, original)

        temporary_inf = stage_boot_driver(setupapi)
        LOG.info("170_boot 패키지 stage 완료 published=%s", temporary_inf)
        restart_device(instance_id)

        with open_target_device(setupapi) as device:
            candidates = enumerate_drivers(device)
            boot = find_driver(candidates, provider="170_BOOT", inf_name=temporary_inf)
            if temporary_inf is None:
                temporary_inf = Path(boot["inf"]).name
            LOG.info("170_boot 선택 inf=%s section=%s", boot["inf"], boot["section"])
            install_selected(newdev, device, boot)

        interface = find_interface_path(setupapi, instance_id)
        LOG.info("170_boot 인터페이스=%s", interface)
        before, after = run_unlock_ioctls(kernel32, interface, pdo_name)
        LOG.info("언락 IOCTL 성공 before=%s after=%s", [hex(x) for x in before], [hex(x) for x in after])

        # Prepare and KEEP the selected A100 driver node before removing the
        # temporary package.  The devnode becomes Unknown-class after removal,
        # so rebuilding the list afterwards loses this forced, non-matching
        # 20F1 driver.  ga100ctl follows the same prepare-before-uninstall order.
        restore_device = open_device_instance_for_display(setupapi, instance_id)
        restore_candidates = enumerate_drivers(restore_device)
        restore_driver = find_driver(
            restore_candidates,
            inf_name=original.get("InfName"),
            description=TARGET_NVIDIA_DESCRIPTION,
            hardware_id=TARGET_HARDWARE_ID,
        )
        LOG.info("NVIDIA 복구 사전 선택 inf=%s section=%s", restore_driver["inf"], restore_driver["section"])

        # Stop the temporary device, then install the retained selected driver
        # node.  The 170_boot package stays staged but unused.  Removing and
        # restaging this unsigned package every boot causes an unavoidable
        # Windows Security confirmation dialog.
        with open_target_device(setupapi) as device:
            cr = cfgmgr32.CM_Disable_DevNode(device.info.DevInst, CM_DISABLE_BITS)
            LOG.info("CM_Disable_DevNode cr=%s", cr)
        time.sleep(0.5)
    except Exception as error:
        LOG.exception("언락 단계 실패: %s", error)
        write_state(status="unlock_failed", error=str(error), instance_id=instance_id, temporary_inf=temporary_inf)
        raise
    finally:
        if temporary_inf and original and instance_id:
            try:
                if restore_device is None or restore_driver is None:
                    # This fallback is valid while 170_boot is still bound.  If
                    # the package was already removed, the explicit --recover
                    # path opens the NULL devnode in a Display-class set.
                    restore_device = open_device_instance_for_display(setupapi, instance_id)
                    restore_candidates = enumerate_drivers(restore_device)
                    restore_driver = find_driver(
                        restore_candidates,
                        inf_name=original.get("InfName"),
                        description=TARGET_NVIDIA_DESCRIPTION,
                        hardware_id=TARGET_HARDWARE_ID,
                    )
                LOG.info("170_boot 패키지는 다음 부팅 재사용을 위해 유지: %s", temporary_inf)
                LOG.info("NVIDIA 복구 선택 inf=%s section=%s", restore_driver["inf"], restore_driver["section"])
                install_selected(newdev, restore_device, restore_driver)
                cfgmgr32.CM_Enable_DevNode(restore_device.info.DevInst, 0)
                restore_succeeded = True
            except Exception as restore_error:
                LOG.exception("NVIDIA 드라이버 복구 실패: %s", restore_error)
                write_state(status="restore_failed", error=str(restore_error), instance_id=instance_id, original=original, temporary_inf=temporary_inf)
            finally:
                if restore_device is not None:
                    restore_device.close()

    if not restore_succeeded:
        raise RuntimeError("NVIDIA 드라이버 복구가 완료되지 않았습니다")
    for _ in range(30):
        memory = nvidia_memory()
        if memory is not None:
            break
        time.sleep(1)
    if memory is None or memory < 60000:
        write_state(status="verify_failed", memory_mib=memory, instance_id=instance_id, original=original, temporary_inf=temporary_inf)
        raise RuntimeError(f"NVIDIA 복구 후 64GB 검증 실패: {memory}")
    LOG.info("직접 언락 완료: %s MiB", memory)
    write_state(status="success", memory_mib=memory, instance_id=instance_id, original=original, temporary_inf=temporary_inf)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct CMP 170HX Windows unlock controller")
    parser.add_argument("--execute", action="store_true", help="perform the destructive PnP/driver transition")
    parser.add_argument("--recover", action="store_true", help="force the saved NVIDIA driver back onto a NULL devnode")
    parser.add_argument("--stage-once", action="store_true", help="stage and retain 170_boot without binding it")
    parser.add_argument("--instance-id", default="", help="exact CMP device instance for --recover")
    parser.add_argument("--nvidia-inf", default="", help="published NVIDIA INF name for --recover")
    args = parser.parse_args()
    mutex = None
    try:
        if args.stage_once or args.recover or args.execute:
            mutex = acquire_operation_mutex()
        if args.stage_once:
            return stage_once()
        if args.recover:
            if not args.instance_id or not args.nvidia_inf:
                raise RuntimeError("--recover에는 --instance-id와 --nvidia-inf가 필요합니다")
            return recover_nvidia(args.instance_id, args.nvidia_inf)
        if args.execute:
            return execute()
        result = check_only()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        LOG.exception("실행 실패: %s", error)
        print(f"[CMP170 direct unlock error] {error}", file=sys.stderr)
        return 1
    finally:
        if mutex is not None:
            mutex[0].CloseHandle(mutex[1])


if __name__ == "__main__":
    raise SystemExit(main())
