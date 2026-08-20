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
- VRAM 가드 토폴로지: 사용자 API/UI `8080` → 내부 llama-server `8082`.
  ComfyUI 큐가 유휴 상태가 된 뒤 모델/메모리를 해제하며, ComfyUI 커스텀 노드는
  교착 방지를 위해 `8082`를 직접 사용
- Media Studio의 URL 다운로드, 품질/오디오 옵션, 진행 상태, 경로 복사 기능
- Dataset, Vast.ai 및 Chrome Instagram 쿠키 브리지 페이지
- Model Hub 재구현: Hugging Face/Civitai/직접 URL 검사, 파일별 선택,
  모델 폴더 제한, 이어받기, 작업 진행률, 설치된 모델 검색
- Windows 전용 기본 경로를 현재 Ubuntu 설치 경로로 교체

## 보존 및 검증 기준

- 복구 착수 전 Git 기준 커밋: `4fdfc67`
- 비밀값과 실행 데이터(JSON 설정, 작업 데이터, 쿠키, 로그, PID, SQLite)는 Git 제외
- 실행 파일의 실제 `--help` 및 현재 ComfyUI CLI 정의와 옵션명을 대조
- Python 구문/컴파일, API 응답, 브라우저 UI, 서비스 재기동, 포트 프록시를 검증 대상으로 삼음
- Ubuntu 시스템 의존성 `ffmpeg`/`ffprobe`와 Media Studio Python 패키지 설치 확인
- `--sleep-idle-seconds` 활성화 시 llama.cpp의 알려진 `--fit` 재로딩 충돌을
  피하도록 `--fit off`를 강제하고 두 번의 sleep/wake 추론 주기를 검증
- ComfyUI JH llama 노드의 직접 백엔드를 `8082`로 변경하고 기존 `8080`
  워크플로 입력도 실행 시 자동 변환해 VRAM 가드 자기대기 교착을 방지
  (`patches/comfyui_jh_llama_8082.patch`로 외부 저장소 변경도 보존)

## 원본 없이 완전 복구할 수 없는 범위

Bot과 Watcher는 이 저장소 밖의 별도 프로젝트 소스입니다. 관리 화면의 실행 연동은
유지했지만 해당 외부 프로젝트 자체는 이 백업만으로 재생성할 수 없습니다. 설정에서
실제 Ubuntu 경로를 지정하면 기존 실행 연동을 사용할 수 있습니다.
