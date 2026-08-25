# CMP 170HX Windows IOCTL 캡처

이 도구는 `ga100ctl.exe`의 언락 동작을 변경하거나 대신 수행하지 않는다. 원본
프로그램을 계측 상태로 실행해 Windows 장치 경로, IOCTL 번호 및 입출력 버퍼를
기록한다.

## 다음 재부팅에서 할 일

1. Windows 로그인 직후 다른 GPU 프로그램을 실행하지 않는다.
2. `C:\main_server_new\capture_ga100ctl.bat`를 더블클릭한다.
3. UAC 창에서 허용한다.
4. 열린 `ga100ctl` 창에서 평소와 동일하게 암호를 입력하고 언락을 진행한다.
5. `ga100ctl`이 끝날 때까지 캡처 콘솔을 닫지 않는다.

원본 `D:\170_boot_v3\ga100ctl.exe`를 직접 실행하면 IOCTL 캡처가 남지 않는다.
캡처 실행기가 후킹 단계에서 실패하면 정지 상태의 프로세스를 종료하고 오류를
표시하므로, 같은 부팅에서 원본 프로그램으로 즉시 재시도할 수 있다.

## 결과 위치

세션별 결과는 다음 경로에 생성된다.

```text
C:\main_server_new\logs\cmp170_capture\YYYYMMDD-HHMMSS\
```

주요 파일:

- `ioctl-events.jsonl`: API 호출 메타데이터와 대응하는 바이너리 파일명
- `*.bin`: 각 IOCTL의 원시 입력/출력 버퍼
- `capture.log`: 캡처 실행/분리/오류 기록
- `ga100ctl-delta.log`: 해당 실행에서 원본 프로그램이 추가한 로그
- `ga100ctl-full.log`: 원본 프로그램이 세션 시작 때 덮어쓴 전체 최신 로그
- `before-*.log`, `after-*.log`: 실행 전후 NVIDIA/PnP/드라이버 상태
- `metadata.json`: 대상 파일 해시와 캡처 런타임 정보

가장 최근 세션 경로는 `logs\cmp170_capture\LATEST.txt`에도 기록된다.

## 안전장치

- 분석한 EXE/SYS/INF/CAT의 SHA-256과 하나라도 다르면 실행하지 않는다.
- 최대 1 MiB까지만 각 API 버퍼를 복사한다.
- 암호 입력 내용 자체는 캡처 로그에 기록하지 않는다.
- 캡처 실패 시 `ga100ctl`을 정지 상태로 남겨두지 않는다.

## 2026-08-25 성공 캡처 결과

세션 `20260825-203350`에서 8GB→64GB 언락이 성공했고 다음 프로토콜을 확인했다.

- 장치 인터페이스 GUID: `{3e15c21a-a02f-4d6a-9244-edb58cbe6e2a}`
- ABI 조회: `IOCTL 0x00226004`, 출력 96바이트, ABI 버전 18
- 언락 실행: `IOCTL 0x0022A000`, 입력 552바이트, 출력 없음
- 20C2 프로파일: `CFG1=0x02779000`, `LMR=0x0000020B`
- compute 값: `SS0=0x88888888`, `SS1=0x00000008`
- 실행 시 PDO: `\Device\NTPNP_PCI0026` (부팅마다 동적으로 다시 조회)

직접 컨트롤러는 `cmp170_direct_unlock.py`에 구현했다. 기본 실행은 읽기 전용
진단이며 `--execute`에서만 PnP 드라이버 전환과 IOCTL을 수행한다.

다음 재부팅의 첫 직접 테스트에서는 원본 프로그램 대신
`C:\main_server_new\unlock_cmp170_direct.bat`를 실행한다. 독립 경로가 한 번
성공하기 전에는 자동 로그온 작업으로 등록하지 않는다.

### 첫 직접 테스트에서 발견·수정한 사항

- unsigned `170_boot`를 매번 삭제/재설치하면 Windows 보안 확인창이 뜬다.
  패키지는 `oem29.inf`로 한 번만 승인해 stage하고, NVIDIA로 복귀한 뒤에도
  Driver Store에 유지해 다음 부팅부터 재사용한다.
- `170_boot` 제거 직후 CMP devnode가 Display 클래스에서 Unknown 클래스로
  바뀌므로 `PresentOnly + Display` 재검색은 실패한다. NVIDIA A100 후보
  `oem22.inf / Section056`를 제거 전에 선택해 같은 device-info set을 유지하도록
  수정했다.
- 최초 실패로 NULL 드라이버 상태가 된 CMP는 `--recover` 경로로 `oem22.inf`를
  재바인딩했고, 이후 `65536 MiB / CM_PROB_NONE` 복구를 확인했다.
