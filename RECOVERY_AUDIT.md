# main_server 복구 감사 기록

복구 기준일: 2026-08-20  
원본 대화 자료: Codex 로컬 상태 DB의 `main_server` 관련 대화 22건과 각 rollout JSONL

## 복구 완료 범위

- ComfyUI 실행 옵션: 외부 접속, SageAttention, 프리뷰/캐시, 수치형 VRAM 예약,
  async offload 비활성화, fast disk, FP16 accumulation
- ComfyUI 별도 로그/콘솔, 정상 종료 확인, 강제 종료 및 panic 보호
- llama.cpp 버전/모델/mmproj/Jinja 템플릿 검색과 최신 버전 우선 정렬
- llama.cpp 추론/Reasoning/Flash Attention/캐시/Speculative decoding/MTP 설정
- llama.cpp 동적 고급 인자(batch, ubatch, load mode, multi-GPU, CPU MoE,
  SWA/프롬프트 캐시, timeout, HTTP threads, metrics, slots, warmup)
- 마지막 llama 실행 설정 저장 및 서비스별 자동 시작
- 물리 GPU UUID 기반 배치, 다중 GPU 순서 지정, 프로세스별 실제 VRAM/오프로딩 표시
- llama.cpp API/UI와 ComfyUI 커스텀 노드는 설정된 llama-server 포트(기본 `8080`)를 직접 사용
- Media Studio의 URL 다운로드, 품질/오디오 옵션, 진행 상태, 경로 복사 기능
- Dataset, Vast.ai 및 Chrome Instagram 쿠키 브리지 페이지
- Model Hub 재구현: Hugging Face/Civitai/직접 URL 검사, 파일별 선택,
  이어받기, 작업 진행률, 설치된 모델 검색. 별도 파일 관리 탭에서 Ubuntu 전체
  디렉터리를 OS 사용자 권한 범위 내 탐색하고 생성/업로드/다운로드/이름 변경/이동/
  휴지통 작업 가능 (시스템 핵심 경로 및 main_server 루트 삭제 차단)
- Windows 전용 기본 경로를 현재 Ubuntu 설치 경로로 교체
- GPU 맵에 NVML 총량과 compute process 합의 차이를 `OS / 디스플레이 / 드라이버`
  VRAM으로 분리 표시
- 기존 `gpu-tune.service`를 검증형 root helper와 `/etc/main-server/gpu-tune.conf`로
  연결하고 280W/1800MHz/65% 현재값을 유지한 채 대시보드에서 동적·영구 변경 지원
- `main_server.service`(systemd user + linger)를 유일한 자동 실행 경로로 유지하고,
  바탕화면에는 서비스가 꺼진 경우 복구 후 콘솔을 여는 바로가기 하나만 유지
- Manager 재시작 API는 별도 app.py를 spawn하지 않고 systemd의 `Restart=always`만
  사용해 8999 orphan/중복 프로세스를 방지. `/models`는 새 `/model-hub` 경로로
  리다이렉트해 브라우저의 복구 전 문서 캐시와 구분
- GPU 튜닝은 `850mV @ 1800MHz` 같은 전압-주파수 곡선 고정이 아니라
  `280W power limit + 1800MHz clock cap` 방식임을 UI에 명시. 현재 RTX 3090
  Linux 드라이버는 목표 mV 직접 지정/조회 기능을 제공하지 않음

## 보존 및 검증 기준

- 복구 착수 전 Git 기준 커밋: `4fdfc67`
- 비밀값과 실행 데이터(JSON 설정, 작업 데이터, 쿠키, 로그, PID, SQLite)는 Git 제외
- 실행 파일의 실제 `--help` 및 현재 ComfyUI CLI 정의와 옵션명을 대조
- Python 구문/컴파일, API 응답, 브라우저 UI, 서비스 재기동, 포트 프록시를 검증 대상으로 삼음
- Ubuntu 시스템 의존성 `ffmpeg`/`ffprobe`와 Media Studio Python 패키지 설치 확인
- llama.cpp 실행 설정은 모델/비전 projector, 컨텍스트, GPU/KV 캐시,
  성능·메모리, MoE, speculative/MTP 인자만 명시적으로 관리
- ComfyUI JH llama 노드는 별도 프록시나 포트 변환 없이 사용자가 지정한 로컬
  llama-server URL을 직접 호출

## 원본 없이 완전 복구할 수 없는 범위

Bot과 Watcher는 이 저장소 밖의 별도 프로젝트 소스입니다. 관리 화면의 실행 연동은
유지했지만 해당 외부 프로젝트 자체는 이 백업만으로 재생성할 수 없습니다. 설정에서
실제 Ubuntu 경로를 지정하면 기존 실행 연동을 사용할 수 있습니다.
