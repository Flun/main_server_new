# CMP 170HX vLLM 운영 메모

관리 화면: `http://127.0.0.1:8999/infrastructure`

## 현재 구성

- 안정 환경: `/home/flux/vllm-env` (`vLLM 0.22.0`, PyTorch cu130)
- 실험 환경: `/home/flux/vllm-dflash-env` (DFlash2 설치 시 별도 생성)
- 공유 모델 루트: `/media/flux/새 볼륨/model`
- 대상 모델: `/media/flux/새 볼륨/model/vllm/Qwen3.8-27B-MTP-NVFP4`
- OpenAI 호환 API: `http://127.0.0.1:8000/v1`

모델 경로는 Hugging Face 캐시의 실제 스냅샷을 가리키는 안정적인 별칭이다.
다운로드와 업데이트는 `HF_HOME=/media/flux/새 볼륨/model`을 사용하므로 다른
엔진과 같은 디스크를 사용한다.

## 프로필

### 안정: NVFP4 + native MTP

모델 카드에서 검증한 vLLM 0.22.0 구성이다. 단일 GPU이므로 TP=1이며 MTP가
한 번에 3개의 speculative token을 제안한다. 첫 하드웨어 검증은 반드시 이
프로필로 시작한다.

### 실험: NVFP4 + DFlash2

`incoai/Qwen3.8-27B-DFlash2`와 vLLM PR #52816을 사용한다. 아직 vLLM 정식
릴리스에 병합되지 않았으므로 안정 환경을 덮어쓰지 않고 별도 venv에 설치한다.
관리 화면의 `격리된 실험 환경 설치 / 업데이트`가 현재 PR HEAD를 설치한다.

## 카드 장착 후 순서

1. 관리 화면에서 GPU 이름, VRAM 64GB, compute capability 12.x, native NVFP4
   준비 상태를 확인한다.
2. llama.cpp와 ComfyUI의 GPU 작업을 중지해 VRAM을 확보한다.
3. 안정 MTP 프로필, 32K context, `gpu-memory-utilization=0.90`부터 시작한다.
4. 코딩 512토큰 벤치마크와 일반 문장 벤치마크를 각각 기록한다.
5. 131K context로 올려 메모리와 prefill을 확인한다.
6. DFlash2 실험 환경을 설치하고 같은 프롬프트로 비교한다.

200 tok/s는 입력 길이, 출력 종류, MTP/DFlash acceptance, 전력·클럭·온도에
따라 달라지는 목표값이지 보장값이 아니다. API 응답의 completion token 수를
실제 경과 시간으로 나눈 관리 화면 벤치마크를 기준으로 비교한다.

