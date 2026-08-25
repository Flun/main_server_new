"""Instrument ga100ctl.exe and capture its user/kernel control protocol.

This is deliberately a capture-only tool.  It does not reproduce or alter the
unlock sequence.  The operator still uses ga100ctl normally while Frida records
device opens and IOCTL buffers for later analysis.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
TOOL_DIR = Path(r"D:\170_boot_v3")
GA100CTL = TOOL_DIR / "ga100ctl.exe"
FRIDA_DIR = BASE_DIR / ".tools" / "frida_capture"
CAPTURE_ROOT = BASE_DIR / "logs" / "cmp170_capture"

# Refuse to instrument a silently replaced kernel toolchain.  Update these only
# after reviewing a new build.
EXPECTED_SHA256 = {
    "ga100ctl.exe": "B1CEE2752B65F9723AD351FD53B28DBDA60D340159427342784EA9BFE665E247",
    "170_boot.sys": "9A57777A24683C8AD4A8A3D0DD02487B0A8802F492C3F7B540384CFA6C54519F",
    "170_boot.cat": "9D5D432A8965839FB36E44613D163FDAC0C01C4A84C613C8B4996CD73498DDEC",
    "170_boot.inf": "E127C9466112DE20EE4669C7CF404A241E26FB188E29FD2479496542CE471045",
}

HOOK_SOURCE = r"""
'use strict';

const MAX_CAPTURE = 1024 * 1024;
const hooked = new Set();
const handlePaths = new Map();
let serial = 0;

function emit(event, fields, bytes) {
    const payload = Object.assign({
        event: event,
        monotonic_ms: Math.floor(Process.getCurrentThreadId() ? Date.now() : Date.now()),
        thread_id: Process.getCurrentThreadId()
    }, fields || {});
    if (bytes !== undefined && bytes !== null) {
        send(payload, bytes);
    } else {
        send(payload);
    }
}

function readBytes(pointer, requested) {
    if (pointer.isNull() || requested <= 0) return null;
    const size = Math.min(requested, MAX_CAPTURE);
    try { return pointer.readByteArray(size); }
    catch (error) {
        emit('read_error', {address: pointer.toString(), requested: requested,
            error: String(error)});
        return null;
    }
}

function exportsNamed(name, modules) {
    const result = [];
    modules.forEach(function (moduleName) {
        try {
            const address = Process.getModuleByName(moduleName).getExportByName(name);
            const key = address.toString();
            if (!hooked.has(key)) {
                hooked.add(key);
                result.push({module: moduleName, address: address});
            }
        } catch (_) {}
    });
    return result;
}

exportsNamed('CreateFileW', ['KernelBase.dll', 'kernel32.dll']).forEach(function (entry) {
    Interceptor.attach(entry.address, {
        onEnter(args) {
            this.path = null;
            try { this.path = args[0].readUtf16String(); } catch (_) {}
            this.source = entry.module;
        },
        onLeave(retval) {
            if (this.path !== null) handlePaths.set(retval.toString(), this.path);
            emit('CreateFileW', {source: this.source, path: this.path,
                handle: retval.toString()});
        }
    });
    emit('hook_attached', {api: 'CreateFileW', module: entry.module,
        address: entry.address.toString()});
});

exportsNamed('CloseHandle', ['KernelBase.dll', 'kernel32.dll']).forEach(function (entry) {
    Interceptor.attach(entry.address, {
        onEnter(args) { this.handle = args[0].toString(); },
        onLeave(_) { handlePaths.delete(this.handle); }
    });
});

exportsNamed('DeviceIoControl', ['KernelBase.dll', 'kernel32.dll']).forEach(function (entry) {
    Interceptor.attach(entry.address, {
        onEnter(args) {
            this.id = ++serial;
            this.source = entry.module;
            this.handle = args[0].toString();
            this.code = args[1].toUInt32();
            this.input = args[2];
            this.inputSize = args[3].toUInt32();
            this.output = args[4];
            this.outputSize = args[5].toUInt32();
            this.returned = args[6];
            emit('DeviceIoControl_enter', {
                call_id: this.id, source: this.source, handle: this.handle,
                path: handlePaths.get(this.handle) || null,
                ioctl: this.code, ioctl_hex: '0x' + this.code.toString(16).padStart(8, '0'),
                input_size: this.inputSize, output_size: this.outputSize,
                captured_size: Math.min(this.inputSize, MAX_CAPTURE)
            }, readBytes(this.input, this.inputSize));
        },
        onLeave(retval) {
            let returned = 0;
            try { if (!this.returned.isNull()) returned = this.returned.readU32(); } catch (_) {}
            emit('DeviceIoControl_leave', {
                call_id: this.id, source: this.source, handle: this.handle,
                path: handlePaths.get(this.handle) || null,
                ioctl: this.code, ioctl_hex: '0x' + this.code.toString(16).padStart(8, '0'),
                success: !retval.isNull(), return_value: retval.toString(),
                bytes_returned: returned,
                captured_size: Math.min(returned || this.outputSize, MAX_CAPTURE)
            }, readBytes(this.output, returned || this.outputSize));
        }
    });
    emit('hook_attached', {api: 'DeviceIoControl', module: entry.module,
        address: entry.address.toString()});
});

exportsNamed('NtDeviceIoControlFile', ['ntdll.dll']).forEach(function (entry) {
    Interceptor.attach(entry.address, {
        onEnter(args) {
            this.id = ++serial;
            this.handle = args[0].toString();
            this.statusBlock = args[4];
            this.code = args[5].toUInt32();
            this.input = args[6];
            this.inputSize = args[7].toUInt32();
            this.output = args[8];
            this.outputSize = args[9].toUInt32();
            emit('NtDeviceIoControlFile_enter', {
                call_id: this.id, handle: this.handle,
                path: handlePaths.get(this.handle) || null,
                ioctl: this.code, ioctl_hex: '0x' + this.code.toString(16).padStart(8, '0'),
                input_size: this.inputSize, output_size: this.outputSize,
                captured_size: Math.min(this.inputSize, MAX_CAPTURE)
            }, readBytes(this.input, this.inputSize));
        },
        onLeave(retval) {
            let information = 0;
            try {
                if (!this.statusBlock.isNull()) {
                    information = Number(this.statusBlock.add(Process.pointerSize).readPointer());
                }
            } catch (_) {}
            emit('NtDeviceIoControlFile_leave', {
                call_id: this.id, handle: this.handle,
                path: handlePaths.get(this.handle) || null,
                ioctl: this.code, ioctl_hex: '0x' + this.code.toString(16).padStart(8, '0'),
                ntstatus: retval.toInt32(), information: information,
                captured_size: Math.min(information || this.outputSize, MAX_CAPTURE)
            }, readBytes(this.output, information || this.outputSize));
        }
    });
    emit('hook_attached', {api: 'NtDeviceIoControlFile', module: entry.module,
        address: entry.address.toString()});
});

emit('capture_ready', {pid: Process.id, architecture: Process.arch,
    pointer_size: Process.pointerSize});
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def preflight() -> dict[str, str]:
    if os.name != "nt":
        raise RuntimeError("이 캡처 도구는 Windows 전용입니다")
    if not FRIDA_DIR.is_dir():
        raise RuntimeError(f"Frida 런타임이 없습니다: {FRIDA_DIR}")
    hashes: dict[str, str] = {}
    for name, expected in EXPECTED_SHA256.items():
        path = TOOL_DIR / name
        if not path.is_file():
            raise RuntimeError(f"필수 파일이 없습니다: {path}")
        actual = _sha256(path)
        hashes[name] = actual
        if actual != expected:
            raise RuntimeError(f"{name} SHA-256 불일치: {actual}")
    return hashes


def _is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


class CaptureWriter:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.events = session_dir / "ioctl-events.jsonl"
        self.text_log = session_dir / "capture.log"
        self.lock = threading.Lock()
        self.binary_serial = 0

    def log(self, message: str) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} {message}"
        with self.lock:
            with self.text_log.open("a", encoding="utf-8") as target:
                target.write(line + "\n")
        print(line, flush=True)

    def on_message(self, message: dict, data: bytes | None) -> None:
        now = datetime.now().astimezone().isoformat(timespec="milliseconds")
        record: dict = {"captured_at": now, "frida_message": message.get("type")}
        if message.get("type") == "send":
            payload = message.get("payload")
            record["payload"] = payload
            if data:
                with self.lock:
                    self.binary_serial += 1
                    event = str((payload or {}).get("event", "buffer"))
                    call_id = str((payload or {}).get("call_id", "none"))
                    filename = f"{self.binary_serial:04d}-{event}-{call_id}.bin"
                    (self.session_dir / filename).write_bytes(bytes(data))
                record["binary_file"] = filename
                record["binary_size"] = len(data)
        else:
            record["description"] = message.get("description")
            record["stack"] = message.get("stack")
            record["message"] = message
        with self.lock:
            with self.events.open("a", encoding="utf-8") as target:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")


def _command_snapshot(session_dir: Path, phase: str) -> None:
    """Save enough host state to correlate the captured IOCTLs with PnP state."""
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    commands = {
        "nvidia-smi": ["nvidia-smi", "-q"],
        "display-devices": [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-PnpDevice -PresentOnly -Class Display | Select-Object Status,FriendlyName,InstanceId | Format-List",
        ],
        "170-boot-service": [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_SystemDriver -Filter \"Name='170_boot'\" | Select-Object Name,State,StartMode,PathName | Format-List",
        ],
    }
    for label, command in commands.items():
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=45,
                creationflags=no_window,
            )
            content = (
                f"command={command!r}\nexit_code={result.returncode}\n\n"
                f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}"
            )
        except Exception as error:
            content = f"command={command!r}\nerror={error!r}\n"
        (session_dir / f"{phase}-{label}.log").write_text(content, encoding="utf-8")


def _save_ga100ctl_log_delta(session_dir: Path, original_size: int) -> None:
    source = TOOL_DIR / "ga100ctl.log"
    if not source.is_file():
        return
    current_size = source.stat().st_size
    start = original_size if current_size >= original_size else 0
    with source.open("rb") as handle:
        handle.seek(start)
        delta = handle.read()
    (session_dir / "ga100ctl-delta.log").write_bytes(delta)
    # ga100ctl truncates and rewrites its log at session start.  When the new
    # file happens to be the same size as the previous one, an offset-only
    # delta is empty, so retain the complete current session as well.
    (session_dir / "ga100ctl-full.log").write_bytes(source.read_bytes())


def run_capture() -> int:
    hashes = preflight()
    if not _is_admin():
        raise RuntimeError("관리자 권한으로 캡처 실행기를 실행해야 합니다")

    sys.path.insert(0, str(FRIDA_DIR))
    import frida  # type: ignore

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = CAPTURE_ROOT / stamp
    session_dir.mkdir(parents=True, exist_ok=False)
    writer = CaptureWriter(session_dir)
    (CAPTURE_ROOT / "LATEST.txt").write_text(str(session_dir), encoding="utf-8")
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "started_at": datetime.now().astimezone().isoformat(),
                "executable": str(GA100CTL),
                "hashes": hashes,
                "frida_version": getattr(frida, "__version__", "unknown"),
                "capture_user": os.environ.get("USERNAME"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    writer.log(f"캡처 세션 시작: {session_dir}")
    writer.log("ga100ctl 창에서 평소처럼 암호를 입력하고 언락을 진행하세요")
    ga_log = TOOL_DIR / "ga100ctl.log"
    ga_log_size = ga_log.stat().st_size if ga_log.is_file() else 0
    _command_snapshot(session_dir, "before")
    device = frida.get_local_device()
    pid = None
    session = None
    script = None
    detached = threading.Event()

    def on_detached(reason, crash=None):
        writer.log(f"프로세스 분리: reason={reason} crash={crash}")
        detached.set()

    try:
        pid = device.spawn([str(GA100CTL)], cwd=str(TOOL_DIR))
        writer.log(f"ga100ctl spawn 완료 pid={pid}; 후킹 스크립트 로드 중")
        session = device.attach(pid)
        session.on("detached", on_detached)
        script = session.create_script(HOOK_SOURCE)
        script.on("message", writer.on_message)
        script.load()
        device.resume(pid)
        writer.log("후킹 준비 완료; ga100ctl 실행을 재개했습니다")
        while not detached.wait(1):
            pass
    except KeyboardInterrupt:
        writer.log("사용자 요청으로 캡처 대기를 중단했습니다; ga100ctl은 강제 종료하지 않습니다")
    except Exception:
        # Never leave a protected executable suspended if injection fails.  The
        # operator can immediately fall back to the original tool in this boot.
        if pid is not None:
            try:
                device.kill(pid)
            except Exception:
                pass
        raise
    finally:
        try:
            if script is not None:
                script.unload()
        except Exception:
            pass
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass
        _save_ga100ctl_log_delta(session_dir, ga_log_size)
        _command_snapshot(session_dir, "after")
    writer.log("캡처 세션 종료")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture ga100ctl device IOCTLs")
    parser.add_argument("--check", action="store_true", help="validate dependencies and hashes without launching ga100ctl")
    args = parser.parse_args()
    try:
        hashes = preflight()
        if args.check:
            sys.path.insert(0, str(FRIDA_DIR))
            import frida  # type: ignore

            print(json.dumps({"ok": True, "hashes": hashes, "frida": getattr(frida, "__version__", "unknown")}, indent=2))
            return 0
        return run_capture()
    except Exception as error:
        print(f"[CMP170 capture error] {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
