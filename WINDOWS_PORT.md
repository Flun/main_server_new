# main_server Windows 포팅 기록

Linux(`/srv/main_server_new`) 운영 상태를 유지하면서 Windows용으로 재포팅했습니다.
`IS_WINDOWS` 분기로 Linux 경로(기존 코드)는 그대로 보존되어 양방향 호환입니다.

## 빠른 시작 (Windows)

```bat
setup_env.bat        :: .venv 생성 + requirements 설치
start_manager.bat    :: manager 실행 (중단 시 5초 후 자동 재시작)
open_manager.bat     :: 꺼져 있으면 기동하고 브라우저로 이동
install_autostart.bat:: 로그온 시 자동 시작 등록 (콘솔 없는 Python supervisor)
setup_media_ai.bat   :: Media Studio AI 음성 분리 패키지 설치 (선택)
```

기본 경로는 이 머신 실측에 맞췄습니다 (`config.py` → `DEFAULTS_WINDOWS`):
ComfyUI_windows_portable, `%USERPROFILE%\.unsloth\llama*`(기존 unsloth 빌드 포함),
모델 루트 `D:\model`. 변경은 대시보드/통합 환경 페이지에서 저장하면
`windows_settings.json`에 기록됩니다.

## Linux → Windows 대응 표

| 기능 | Linux | Windows |
|---|---|---|
| manager 자동 시작 | systemd user service + linger | HKCU Run의 `pythonw manager_supervisor.py` 단일 감독 프로세스. `cmd.exe`/PowerShell을 거치지 않아 창이 깜빡이지 않으며 중단 시 5초 후 재시작 |
| manager 재시작 API | systemd Restart에 위임(os._exit) | `manager_restarter.py` detached helper가 포트 8999가 비면 재기동 (orphan/중복 방지) |
| GPU 튜닝(전력/클럭/팬) | sudo root helper + `/etc/main-server/gpu-tune.d` | 인증된 관리자 helper에서 `nvidia-smi` 실행, `gpu_tune_settings.json`(UUID 키)에 저장, **manager 시작 시 자동 재적용**. 코어 값은 고정이 아니라 `최저 지원 클럭~사용자 상한`의 동적 범위로 적용되어 idle 다운클럭 유지. 일부 드라이버에서 팬 설정 미지원 시 경고 노출 |
| 메인보드 PWM / GPU HBM 연동 | Linux hwmon 조건부 | `fan_helper/MainServer.FanHelper`가 LibreHardwareMonitor + PawnIO를 단독 소유. 최초 manager 실행 시 공식 PawnIO 2.2.0을 해시/서명 검증 후 UAC 1회로 자동 설치하고 관리자 예약 작업을 등록. 이후 인증된 localhost 채널로 GPU UUID와 PWM 채널을 저장하고 HBM 커브를 5초마다 적용. 15초 heartbeat lease 만료, manager 종료, 수동 해제 시 보드 기본 모드로 복귀. Fan Control 동시 실행 차단 |
| llama.cpp 설치/업데이트 | GitHub 태그 선택 + `GGML_CUDA=ON` 소스 빌드 → `/opt/llama-<tag>` | GitHub 태그/CUDA 자산 선택 → `<root>\llama-<tag>-cuda<버전>`. 동일 릴리스의 cudart/cuBLAS DLL 묶음을 자동 결합하고 `llama-server --version`까지 통과해야 설치 성공 처리 |
| VRAM 프로세스 스캔 | nvidia-smi compute-apps | 동일 (권한 없는 프로세스는 `[Insufficient Permissions]`로 자동 스킵) |
| 외부 프로세스 감지 | `ps -eo pid,args` regex | psutil cmdline regex (동일 패턴) |
| 데스크톱 GUI/CLI 전환, 물리 모니터 전원(DDC/CI) | GDM 헬퍼 / main-server-monitor-power | **Linux 전용으로 유지** — Windows API는 `available: false` 반환, UI 버튼 비활성화 |
| vLLM 통합 환경 | pip venv(`bin/`) | 동일 코드(`Scripts\`)로 시도하되 **Windows에서 동작 보장 안 함**(사용자 결정). 실패 시 인스톨 로그에 원문 노출 |
| Model Hub 파일 관리자 | `/`, `/srv`, `/mnt` 등 루트 | 드라이브 루트(C:~K:) + 홈/데스크톱/다운로드/문서. 보호 대상: 드라이브 루트, 홈, main_server 루트 |
| ffmpeg 탐색 | PATH + `/opt/ffmpeg/bin` 등 | PATH + `BASE_DIR\ffmpeg\bin` (미설치 시 Media Studio 변환만 비활성) |

## 유지되는 동작 (변경 없음)

- 포트: manager 8999, llama.cpp 8080, ComfyUI 8188, vLLM 8000
- llama.cpp API는 별도 프록시 없이 설정된 포트로 직접 연결
- GPU HBM 온도 이벤트, 하드웨어 이력, 메모/로그/프리셋 API
- Dataset / Vast Remote / Chrome Instagram 쿠키 브리지 (Vast 원격 인스턴스 명령은 Linux 유지 — 대상이 리눅스 서버라서)

## 주의

- 자동 시작 등록 전에는 `start_manager.bat` 또는 `open_manager.bat`으로 수동 실행합니다.
- GPU 튜닝 영속성은 "manager 시작 시 적용" 방식이라, manager가 자동 시작되어 있지 않으면
  부팅 후 값이 리셋됩니다 (Linux gpu-tune.service와 동일한 전제).
- ComfyUI portable 레이아웃에서 `comfyui_python`은 `python_embeded\python.exe`를
  자동 감지합니다(수동 지정 시 그것을 우선).
