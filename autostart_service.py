"""Windows 자동 시작 등록 헬퍼 — Linux main_server.service 상당.

사용:
  python autostart_service.py status        # 자동 시작 상태 출력
  python autostart_service.py register      # 로그온 시 숨김 supervisor 자동 실행 등록
  python autostart_service.py unregister    # 자동 시작 해제

HKCU Run에서 pythonw 기반 단일-instance supervisor를 직접 시작합니다. cmd.exe와
PowerShell을 거치지 않으므로 콘솔 창이 생기지 않습니다.
"""

import json
import os
import subprocess
import sys

TASK_NAME = "MainServer"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _run_cmd():
    """HKCU Run command that never allocates a console."""
    candidate = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
    pythonw = candidate if os.path.isfile(candidate) else os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    supervisor = os.path.join(BASE_DIR, "manager_supervisor.py")
    return f'"{pythonw}" "{supervisor}"'


def _run(cmd):
    """콘솔 창 없이 명령을 실행하고 (rc, stdout, stderr)를 반환."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    kwargs = dict(
        capture_output=True, text=True, errors="replace",
        timeout=15, stdin=subprocess.DEVNULL,
    )
    try:
        r = subprocess.run(cmd, creationflags=flags, **kwargs)
    except OSError:
        r = subprocess.run(cmd, **kwargs)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _reg_query():
    """HKCU Run 키의 MainServer 값. 등록되지 않았으면 None."""
    rc, out, _ = _run(["reg", "query", f"HKCU\\{RUN_KEY}", "/v", TASK_NAME])
    if rc != 0:
        return None
    for line in out.splitlines():
        stripped = line.strip()
        # reg query 출력은 공백 구분: "    MainServer    REG_SZ    <value>"
        if stripped.startswith(TASK_NAME) and "REG_SZ" in stripped:
            return stripped.split("REG_SZ", 1)[1].strip().strip('"')
    return None


def status():
    """Only the current hidden registration counts as enabled."""
    registered = _reg_query()
    reg_ok = bool(registered and "manager_supervisor.py" in registered.lower())
    task_ok = False
    try:
        rc, _, _ = _run(["schtasks", "/query", "/tn", TASK_NAME])
        task_ok = rc == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return {"exists": reg_ok, "registry": reg_ok, "task_scheduler": False, "legacy_registration": bool(registered and not reg_ok) or task_ok}


def register():
    """부팅 후 로그온 시 자동 실행 등록. 관리자 권한 불필요."""
    # 1) HKCU Run 키 — 사용자 hive이므로 관리자 권한 없이 등록 가능
    rc, out, err = _run([
        "reg", "add", f"HKCU\\{RUN_KEY}",
        "/v", TASK_NAME, "/t", "REG_SZ", "/d", _run_cmd(), "/f",
    ])
    if rc != 0:
        raise RuntimeError(f"Run 키 등록 실패: {err or out}")

    # Older versions registered a second batch supervisor here. Remove it so
    # two watchdogs cannot race after a manager restart.
    _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])


def unregister():
    """자동 시작 해제 (Run 칸 + Task Scheduler 모두)."""
    _run(["reg", "delete", f"HKCU\\{RUN_KEY}", "/v", TASK_NAME, "/f"])
    _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    if action == "register":
        register()
        print(f"[autostart] 등록 완료: {TASK_NAME} (숨김 supervisor)")
    elif action == "unregister":
        unregister()
        print(f"[autostart] 해제 완료: {TASK_NAME}")
    elif action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
