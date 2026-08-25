# CMP 170HX / SageAttention 작업 인계서

마지막 갱신: 2026-08-23 14:06 KST  
작업 위치: `/srv/main_server_new`, `/home/flux/ComfyUI`, `/home/flux/cmpunlocker`

## 1. 현재 결론

- 재부팅 후 CMP 170HX는 정상 복귀했고 unlock도 정상 동작한다.
  - 장치: `NVIDIA CMP 170HX`
  - UUID: `GPU-6cd658de-2bfc-dbd7-78d3-beec843855d9`
  - Compute capability: `8.0`
  - 노출 VRAM/BAR1: `65536 MiB`
  - 커널 로그: `CMP BAR1: final size = 65536 MB`
- 현재 부팅에는 Xid/AER 오류가 없다.
- CMP에는 진단용 보수 설정 `220W / Core 1410MHz / Fan auto`가 적용되어 있다.
- 공식 SageAttention `main` 소스를 CMP(sm80)+3090(sm86) 양쪽 아키텍처로 다시 빌드해 설치했다. 두 GPU의 작은 직접 연산 테스트는 정상이다.
- 그러나 현재 로컬 `krea2_turbo_fp8_scaled.safetensors`를 CMP에서 SageAttention으로 샘플링하면 NaN이 발생해 완전한 검정 이미지가 나온다.
- 동일 워크플로/모델을 PyTorch attention으로 실행하면 정상 이미지가 나오므로 CMP unlock, HBM, VAE, 전체 워크플로 고장으로 보기는 어렵다. 현재 핵심 미해결점은 **Krea2 FP8 + SageAttention + 현재 소프트웨어 스택** 조합이다.
- 검정 출력 테스트 중 CMP는 220W 제한 안에서 정상 유지됐고 재이탈하지 않았다. 위험 때문에 전체 다중 분기 테스트는 중지했다.

## 2. GPU와 안전 설정

### CMP 170HX

- UUID 설정 파일: `/etc/main-server/gpu-tune.d/GPU-6cd658de-2bfc-dbd7-78d3-beec843855d9.conf`
- 현재 내용:

```ini
GPU_UUID=GPU-6cd658de-2bfc-dbd7-78d3-beec843855d9
GPU_INDEX_LAST=0
TUNING_ENABLED=1
POWER_LIMIT=220
CLOCK_MAX=1410
CLOCK_AUTO=0
FAN_PERCENT=0
FAN_AUTO=1
```

- `gpu-tune.service`를 재시작해 실제 부팅 적용 경로까지 검증했다.
- 서비스 로그: `GPU 0 (...): 220W / 1410MHz / fan auto`
- UI의 CMP 170HX **권장 기본값**은 `250W / Core 1410MHz / Fan auto`이다. 260W를 기본값으로 사용하지 않는다.
- 현재 220W는 장애 분석 중 사용하는 더 보수적인 제한이다. 문제 해결 전 250W 이상으로 올리지 않는 편이 안전하다.

### RTX 3090

- UUID: `GPU-309d93a2-b61f-60c2-d5d3-9a79f26e7468`
- 설정: `280W / Core 1800MHz / Fan 65%`
- llama.cpp가 이 GPU를 사용하도록 실행 중이다.

### 설정 구현 변경

- 설정은 GPU index가 아니라 UUID별 `/etc/main-server/gpu-tune.d/*.conf`만 사용한다.
- 오래된 `/etc/main-server/gpu-tune.conf`의 260W 값은 의도적으로 무시한다. GPU 순서가 바뀔 때 다른 카드 설정이 적용되는 사고를 방지하기 위함이다.
- UI에서 GPU별 `적용 끄기`가 가능하다. 끄면 현재 실행값은 갑자기 변경하지 않고 다음 부팅부터 해당 UUID를 건너뛴다.
- 관련 파일:
  - `/srv/main_server_new/system/main-server-gpu-control`
  - 설치본 `/usr/local/sbin/main-server-gpu-control`
  - `/usr/local/sbin/gpu-tune.sh`
  - `/srv/main_server_new/app.py`
  - `/srv/main_server_new/index.html`

## 3. CMP unlock 상태

- 사용한 포크 소스: `https://github.com/lesj0610/cmpunlocker`
- 로컬 소스: `/home/flux/cmpunlocker`
- NVIDIA Open Kernel Module: `610.43.02`
- 재부팅 후 커널 로그에서 다음을 확인했다.

```text
CMP BAR1: resize enabled, will attempt up to 64 GB
CMP BAR1: final size = 65536 MB
name string overridden to 'NVIDIA CMP 170HX' (devId=0x20c2)
```

- `nvidia-smi`에서도 CMP가 `65536 MiB`, compute capability `8.0`으로 표시된다.

## 4. SageAttention 설치 상태

- 소스: 공식 SageAttention `main`
- 로컬 소스: `/home/flux/ComfyUI/SageAttention-src`
- 커밋: `d1a57a546c3d395b1ffcbeecc66d81db76f3b4b5`
- 빌드 아키텍처: `TORCH_CUDA_ARCH_LIST='8.0;8.6'`
- 설치 환경:
  - ComfyUI venv: `/home/flux/ComfyUI/venv`
  - Python 3.12
  - Torch `2.13.0+cu130`
  - Triton `3.7.1`
  - CUDA toolkit `/usr/local/cuda-13.0`
- 빌드 wheel:
  - `/home/flux/ComfyUI/wheels/d1a57a5-sm80-sm86/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl`
  - SHA256 `7b8009f4b14ee8d9f5ec3abfe1acd7021da48e7b811079db6592c8a001bbfa6c`
- `cuobjdump`에서 `sm_80.cubin`과 `sm_86.cubin`을 모두 확인했다.
- 공식 dispatch는 수정하지 않았다.
  - sm80(CMP): CUDA kernel, `pv_accum_dtype="fp32"`
  - sm86(3090): Triton kernel
- FP16/BF16의 작은 contiguous tensor 직접 테스트는 두 GPU 모두 finite output 및 PyTorch 대비 낮은 오차로 통과했다.

## 5. ComfyUI 현재 실행 상태

- ComfyUI commit: `c67885b14556cf3e4e061862925282d403d09862`
- 포트: `8188`
- 현재 실행 옵션:

```text
main.py --port 8188 --listen 0.0.0.0 --preview-method none --use-sage-attention --disable-async-offload
```

- 설정 파일: `/srv/main_server_new/comfyui_settings.json`
- 핵심 값:

```json
{
  "use_sage_attention": true,
  "preview_method_none": true,
  "disable_async_offload": true,
  "fast_fp16_accumulation": false,
  "gpu_device": "GPU-6cd658de-2bfc-dbd7-78d3-beec843855d9"
}
```

## 6. 재현 결과와 원인 분석

### 이전 GPU 이탈

- 이전 강제 Triton 실험의 전체 워크플로 부하 중 CMP가 사라졌다.
- 최초 커널 오류: Xid 79 `GPU has fallen off the bus` (2026-08-23 13:43:58 KST).
- 보드는 CMP를 CPU 직결이 아닌 B550 칩셋 경로의 PCIe Gen2 x4로 연결한다. 3090은 CPU 직결이다.
- 당시 강제 Triton 패치는 재부팅 전에 되돌렸다. 다시 적용하지 말 것.
- Xid 79는 PCIe/GPU 응답 상실의 결과 코드이며, 단독으로 PSU·슬롯·커널 중 하나를 확정하지는 않는다. 반복 부하, 강제 Triton, 전력/링크 조건이 함께 관여했을 가능성이 있다.

### 재부팅 후 제한 테스트

- 최근 정상 PyTorch 출력 PNG에 포함된 119-node prompt에서 SaveImage 한 분기만 의존성 기준으로 줄여 33 nodes로 테스트했다.
- SageAttention, CMP 220W/1410MHz, preview off, async offload off 조건.
- 약 71초 완료, 관측 최댓값:
  - 전력 `212.33W`
  - Core `1410MHz`
  - GPU 온도 `50°C`
  - HBM 온도 `57°C`
  - VRAM 약 `18.8GB`
- Xid와 bus loss는 없었다.
- 출력:
  - `/home/flux/ComfyUI/output/Krea2_turbo_15510_.png`
  - `/home/flux/ComfyUI/output/Krea2_turbo_15511_.png`
- 두 파일 모두 RGB min/max/mean `0`, black pixel `100%`.
- 로그에 `/home/flux/ComfyUI/nodes.py:1698 RuntimeWarning: invalid value encountered in cast`가 다시 발생했다.

### 모델 정밀도 확인

- `/home/flux/ComfyUI/models/unet/krea2_turbo_fp8_scaled.safetensors`
  - 13,141,730,784 bytes
  - tensor: F8_E4M3 256개, F32 scale 256개, BF16 174개
- `/home/flux/ComfyUI/models/unet/darkBeastINT8Convrot2_darkBeastKREA2FP8.safetensors`
  - 22,203,498,732 bytes
  - 역시 FP8 포함 모델이며 full BF16/FP16 Krea2 대체재가 아니다.
- 현재 로컬에는 BF16/FP16 Krea2 UNet이 확인되지 않았다.

### 가장 가능성 높은 원인

1. 현재 Krea2 FP8 scaled weight와 SageAttention 사이의 수치 비정상(NaN).
2. Vast에서 과거 성공했던 환경과 현재 로컬 환경의 Torch/Triton/ComfyUI 버전 또는 모델 로더 차이.
3. 작은 contiguous 직접 테스트로는 실제 Krea2의 길고 non-contiguous한 attention layout을 재현하지 못한다.

참고 이슈:

- FP8 model + Sage black output: `https://github.com/thu-ml/SageAttention/issues/221`
- 긴 non-contiguous Triton offset/crash: `https://github.com/thu-ml/SageAttention/issues/386`
- NVIDIA Xid 문서: `https://docs.nvidia.com/deploy/pdf/XID_Errors.pdf`

## 7. Vast 환경 비교 상태

- main_server의 Vast 원격 설정에서 현재 Jupyter URL이 비어 있어 원격 `/workspace/setup_h3_final_v2.sh`를 직접 읽지 못했다.
- 로컬 `/home/flux/ComfyUI/SageAttention-src`가 Vast 방식 설치 흔적으로 보이며, 공식 main commit을 source build한 상태였다.
- 그 exact source commit을 sm80+sm86으로 다시 빌드해도 Krea2 FP8 검정 출력은 해결되지 않았다.
- 다음 AI가 원격 Vast 인스턴스에 다시 연결할 수 있다면 아래를 우선 비교할 것:
  - `/workspace/setup_h3_final_v2.sh`
  - Torch, CUDA, Triton, SageAttention 버전/commit
  - ComfyUI commit
  - 실제 사용한 Krea2 모델 파일명과 SHA256
  - SageAttention 설치 명령과 build arch

## 8. GPU 모니터링 UI 구현

- main_server UI에서 GPU별로 좁은 카드 형태로 표시한다.
- 기본 카드: GPU util, VRAM, GPU 온도, HBM 온도, 전력, core/memory clock.
- 상세 펼침: GPU UUID/PCI, GPU별 프로세스와 VRAM 사용량.
- CMP의 `temperature.memory`를 HBM 온도로 표시한다. 3090처럼 드라이버가 값을 주지 않으면 `--`로 표시한다.
- HBM이 `85°C 초과`한 연속 구간을 1회 이벤트로 기록한다.
- 기록 내용: 시작/종료 시각, 지속시간, 최고 온도, 당시 GPU 작업(service/process/PID/VRAM).
- API:
  - `GET /api/gpu/thermal-events`
  - `POST /api/gpu/thermal-events/clear`
- 저장 파일: `/srv/main_server_new/gpu_thermal_events.json` (첫 이벤트가 생길 때 생성)
- 최대 500 이벤트 보관.
- 자원 비용은 작다. 기존 하드웨어 샘플 주기에 맞춰 `nvidia-smi` 1회를 1초 캐시하고, 디스크 쓰기는 이벤트 시작/새 최고점/종료 때만 한다. 모델 샘플링 부하에 비하면 무시 가능한 수준이다.
- 검증:
  - `/srv/main_server_new/.venv/bin/python -m py_compile app.py gpu.py` 통과
  - 관련 shell script `bash -n` 통과
  - `GET http://127.0.0.1:8999/api/gpus`에서 CMP HBM `48°C` 수집 확인
  - thermal events API 정상, 현재 count 0

## 9. 현재 실행 프로세스

- main_server: 포트 `8999`
- ComfyUI: 포트 `8188`, CMP 사용, SageAttention enabled
- llama.cpp: 포트 `8080`, 3090 사용

## 10. 다음 작업 권장 순서

1. **Vast에서 성공했던 정확한 환경을 확보해 버전/모델 hash를 비교한다.** 현재 가장 정보 가치가 높고 GPU 위험이 없다.
2. 원격 성공 모델이 FP8인지 확인한다. 다른 quantization 또는 BF16이면 같은 모델로 맞춰 한 분기만 A/B한다.
3. exact 환경 비교 전에는 CMP에서 v2 Triton 강제, 전체 8분기, 전력 상향 테스트를 하지 않는다.
4. 새로운 후보를 테스트할 때도 220W/1410MHz, 단일 분기, HBM/Xid 동시 모니터링을 유지한다.
5. Xid 79가 안전 설정에서도 재발하면 소프트웨어보다 PCIe 슬롯/보조전원/라이저/칩셋 링크를 우선 점검한다.

## 11. 빠른 확인 명령

```bash
# GPU/온도/전력
nvidia-smi --query-gpu=index,name,uuid,power.limit,clocks.current.graphics,clocks.current.memory,temperature.gpu,temperature.memory --format=csv,noheader

# 현재 부팅 Xid/AER
journalctl -k -b --no-pager | rg 'NVRM: Xid|AER:'

# UUID별 저장 설정과 부팅 적용
sudo /usr/local/sbin/main-server-gpu-control list
systemctl status gpu-tune.service --no-pager -l

# main_server GPU/HBM API
curl -fsS http://127.0.0.1:8999/api/gpus | jq .
curl -fsS http://127.0.0.1:8999/api/gpu/thermal-events | jq .

# 실행 프로세스
ps -eo pid,lstart,args | rg '/home/flux/ComfyUI|llama-server'
```

## 12. 주의

- `/etc/main-server/gpu-tune.conf`의 오래된 260W 값은 현재 구현에서 무시된다. 다시 index 기반 적용 로직을 살리지 말 것.
- `nvidia-smi --query-gpu=clocks.max.graphics`는 하드웨어 최대값을 보여 주므로 1695MHz로 표시될 수 있다. 실제 lock은 부하 중 `clocks.current.graphics`가 1410MHz를 넘지 않는지로 확인한다.
- 현재 SageAttention 자체의 sm80 누락 문제는 해결됐지만, Krea2 FP8 black/NaN 문제는 해결되지 않았다.
- full precision 모델 신규 다운로드는 대용량이므로 사용자 확인 없이 진행하지 않았다.
