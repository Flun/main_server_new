# main_server Windows 포팅 기록

Linux(`/srv/main_server_new`) 운영 상태를 유지하면서 Windows용으로 재포팅했습니다.
`IS_WINDOWS` 분기로 Linux 경로(기존 코드)는 그대로 보존되어 양방향 호환입니다.

## 빠른 시작 (Windows)

```bat
setup_env.bat        :: .venv 생성 + requirements 설치
start_manager.bat    :: manager 실행 (중단 시 5초 후 자동 재시작)
open_manager.bat     :: 꺼져 있으면 기동하고 브라우저로 이동
install_autostart.bat:: 로그온 시 자동 시작 등록 (Task Scheduler: MainServer)
setup_media_ai.bat   :: Media Studio AI 음성 분리 패키지 설치 (선택)
```

기본 경로는 이 머신 실측에 맞췄습니다 (`config.py` → `DEFAULTS_WINDOWS`):
ComfyUI_windows_portable, `%USERPROFILE%\.unsloth\llama*`(기존 unsloth 빌드 포함),
모델 루트 `D:\model`. 변경은 대시보드/통합 환경 페이지에서 저장하면
`windows_settings.json`에 기록됩니다.

## Linux → Windows 대응 표

| 기능 | Linux | Windows |
|---|---|---|
| manager 자동 시작 | systemd user service + linger | Task Scheduler `MainServer`(로그온 시) + `start_manager.bat` 감독 루프(중단 시 5초 후 재시작 = `Restart=always` 상당) |
| manager 재시작 API | systemd Restart에 위임(os._exit) | `manager_restarter.py` detached helper가 포트 8999가 비면 재기동 (orphan/중복 방지) |
| GPU 튜닝(전력/클럭/팬) | sudo root helper + `/etc/main-server/gpu-tune.d` | `nvidia-smi -pl/-lgc/--fan` 직접 호출, `gpu_tune_settings.json`(UUID 키)에 저장, **manager 시작 시 자동 재적용**. CMP 170HX 권장값(250W/1410MHz) 로직 동일. 일부 드라이버에서 권한 오류가 나면 원문 노출 |
| llama.cpp 설치/업데이트 | git clone + CUDA 빌드 → `/opt/llama-<tag>` | GitHub 릴리스 **Windows CUDA 프리빌트 zip** 다운로드 → `<root>\llama-<tag>` (단일 최상위 폴더 승격, `build\bin\llama-server.exe` 검증). UI에서 자산 선택 가능(미선택 시 CUDA 버전 높은 것 자동 선택) |
| VRAM 프로세스 스캔 | nvidia-smi compute-apps | 동일 (권한 없는 프로세스는 `[Insufficient Permissions]`로 자동 스킵) |
| 외부 프로세스 감지 | `ps -eo pid,args` regex | psutil cmdline regex (동일 패턴) |
| 데스크톱 GUI/CLI 전환, 물리 모니터 전원(DDC/CI) | GDM 헬퍼 / main-server-monitor-power | **Linux 전용으로 유지** — Windows API는 `available: false` 반환, UI 버튼 비활성화 |
| vLLM 통합 환경 | pip venv(`bin/`) | 동일 코드(`Scripts\`)로 시도하되 **Windows에서 동작 보장 안 함**(사용자 결정). 실패 시 인스톨 로그에 원문 노출 |
| Model Hub 파일 관리자 | `/`, `/srv`, `/mnt` 등 루트 | 드라이브 루트(C:~K:) + 홈/데스크톱/다운로드/문서. 보호 대상: 드라이브 루트, 홈, main_server 루트 |
| ffmpeg 탐색 | PATH + `/opt/ffmpeg/bin` 등 | PATH + `BASE_DIR\ffmpeg\bin` (미설치 시 Media Studio 변환만 비활성) |

## 유지되는 동작 (변경 없음)

- 포트: manager 8999, llama 공개 8080 / 내부 8082(VRAM 가드), ComfyUI 8188, vLLM 8000
- VRAM 가드 토폴로지(ComfyUI 큐 대기 → 해제 → 8082 전달)
- GPU HBM 온도 이벤트, 하드웨어 이력, 메모/로그/프리셋 API
- Dataset / Vast Remote / Chrome Instagram 쿠키 브리지 (Vast 원격 인스턴스 명령은 Linux 유지 — 대상이 리눅스 서버라서)

## 주의

- Task Scheduler 등록 전에는 `start_manager.bat` 또는 `open_manager.bat`으로 수동 실행합니다.
- GPU 튜닝 영속성은 "manager 시작 시 적용" 방식이라, manager가 자동 시작되어 있지 않으면
  부팅 후 값이 리셋됩니다 (Linux gpu-tune.service와 동일한 전제).
- ComfyUI portable 레이아웃에서 `comfyui_python`은 `python_embeded\python.exe`를
  자동 감지합니다(수동 지정 시 그것을 우선).
