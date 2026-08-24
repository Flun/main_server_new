"""Windows 자동 시작 등록 헬퍼 (Task Scheduler) — Linux main_server.service 상당.

사용:
  python autostart_service.py status        # 태스크 상태 출력
  python autostart_service.py register      # 로그온 시 start_manager.bat 실행 태스크 등록
  python autostart_service.py unregister    # 태스크 삭제

태스크가 start_manager.bat(감독 루프)을 실행하므로 manager가 멈추면 5초 후
자동 재시작됩니다 (systemd Restart=always 상당).
"""

import json
import os
import subprocess
import sys

TASK_NAME = "MainServer"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _task_command():
    bat = os.path.join(BASE_DIR, "start_manager.bat")
    # schtasks /tr 는 인자가 있으면 따옴표가 필요합니다.
    return f'cmd /c ""{bat}""'


def status():
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"exists": False, "error": str(error)}
    exists = result.returncode == 0
    running = "RUNNING" in (result.stdout or "").upper() if exists else False
    return {"exists": exists, "running": running}


def register():
    command = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", _task_command(),
        "/sc", "onlogon",
        "/rl", "limited",
        "/f",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "schtasks 실패").strip())


def unregister():
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "schtasks 실패").strip())


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    if action == "register":
        register()
        print(f"[autostart] 등록 완료: {TASK_NAME} (로그온 시 start_manager.bat)")
    elif action == "unregister":
        unregister()
        print(f"[autostart] 삭제 완료: {TASK_NAME}")
    elif action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
