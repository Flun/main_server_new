#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "== main_server 환경 설정 =="
PY=$(command -v python3)
echo "python: $PY"
INSTALL_USER=$(id -un)
if [[ ! "$INSTALL_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "지원하지 않는 Linux 사용자명입니다: $INSTALL_USER" >&2
  exit 1
fi

MISSING_PACKAGES=()
PYTHON_VENV_PACKAGE=$("$PY" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")')
PYTHON_DEV_PACKAGE=$("$PY" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")')
dpkg-query -W -f='${Status}' "$PYTHON_VENV_PACKAGE" 2>/dev/null | grep -q 'ok installed' || MISSING_PACKAGES+=("$PYTHON_VENV_PACKAGE")
dpkg-query -W -f='${Status}' "$PYTHON_DEV_PACKAGE" 2>/dev/null | grep -q 'ok installed' || MISSING_PACKAGES+=("$PYTHON_DEV_PACKAGE")
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  MISSING_PACKAGES+=(ffmpeg)
fi
if ! command -v mount.cifs >/dev/null 2>&1; then
  MISSING_PACKAGES+=(cifs-utils)
fi
if ! command -v mount.ntfs-3g >/dev/null 2>&1; then
  MISSING_PACKAGES+=(ntfs-3g)
fi
if ! command -v smbclient >/dev/null 2>&1; then
  MISSING_PACKAGES+=(smbclient)
fi
if ! dpkg-query -W -f='${Status}' openssh-server 2>/dev/null | grep -q 'ok installed'; then
  MISSING_PACKAGES+=(openssh-server)
fi
if ! command -v gcc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
  MISSING_PACKAGES+=(build-essential)
fi
if ! command -v cmake >/dev/null 2>&1; then
  MISSING_PACKAGES+=(cmake)
fi
if ! command -v ninja >/dev/null 2>&1; then
  MISSING_PACKAGES+=(ninja-build)
fi
dpkg-query -W -f='${Status}' libcurl4-openssl-dev 2>/dev/null | grep -q 'ok installed' || MISSING_PACKAGES+=(libcurl4-openssl-dev)
if ((${#MISSING_PACKAGES[@]})); then
  echo "시스템 의존성 설치: ${MISSING_PACKAGES[*]}"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_PACKAGES[@]}"
fi

# 기본 /opt 경로는 root 소유이므로 manager 사용자가 버전별 llama.cpp를
# staging/검증 후 교체할 수 있는 전용 디렉터리만 미리 위임한다.
sudo install -d -o "$INSTALL_USER" -g "$(id -gn)" -m 0755 /opt/llama

MODEL_VOLUME_UUID="${MAIN_SERVER_MODEL_UUID:-4CF89226F8920E78}"
COMFY_MODEL_VOLUME_UUID="${MAIN_SERVER_COMFY_MODEL_UUID:-06ECC18DECC17787}"
INSTALL_UID=$(id -u)
INSTALL_GID=$(id -g)

echo "선택형 Linux 초기 설정 helper 설치..."
sed -e "s/^TARGET_USER=.*/TARGET_USER=$INSTALL_USER/" \
    -e "s/^TARGET_UID=.*/TARGET_UID=$INSTALL_UID/" \
    -e "s/^TARGET_GID=.*/TARGET_GID=$INSTALL_GID/" \
    -e "s/^MODEL_VOLUME_UUID=.*/MODEL_VOLUME_UUID=$MODEL_VOLUME_UUID/" \
    -e "s/^COMFY_MODEL_VOLUME_UUID=.*/COMFY_MODEL_VOLUME_UUID=$COMFY_MODEL_VOLUME_UUID/" \
    system/main-server-linux-setup | sudo tee /usr/local/sbin/main-server-linux-setup >/dev/null
sudo chown root:root /usr/local/sbin/main-server-linux-setup
sudo chmod 0755 /usr/local/sbin/main-server-linux-setup
sed "s/^flux /$INSTALL_USER /" system/main-server-linux-setup.sudoers | sudo tee /etc/sudoers.d/main-server-linux-setup >/dev/null
sudo chown root:root /etc/sudoers.d/main-server-linux-setup
sudo chmod 0440 /etc/sudoers.d/main-server-linux-setup
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-linux-setup

echo "로그인 없는 CLI 서버 환경 일괄 적용..."
sudo /usr/local/sbin/main-server-linux-setup apply cli_boot console_blank ssh linger model_mounts nas

if [ ! -d .venv ]; then
  echo "venv 생성..."
  "$PY" -m venv .venv
fi

echo "의존성 설치..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "Linux 시스템 helper 설치..."
sudo install -o root -g root -m 0755 system/main-server-gpu-control /usr/local/sbin/main-server-gpu-control
sudo install -o root -g root -m 0755 system/gpu-tune.sh /usr/local/sbin/gpu-tune.sh
sudo install -o root -g root -m 0755 system/main-server-fan-control.py /usr/local/sbin/main-server-fan-control
sed -e "s/^TARGET_USER=.*/TARGET_USER=$INSTALL_USER/" -e "s/^TARGET_UID=.*/TARGET_UID=$INSTALL_UID/" \
  system/main-server-desktop-toggle | sudo tee /usr/local/sbin/main-server-desktop-toggle >/dev/null
sudo chown root:root /usr/local/sbin/main-server-desktop-toggle
sudo chmod 0755 /usr/local/sbin/main-server-desktop-toggle
sudo install -o root -g root -m 0755 system/main-server-os-boot /usr/local/sbin/main-server-os-boot
sudo install -o root -g root -m 0644 system/gpu-tune.service /etc/systemd/system/gpu-tune.service
sed "s/^AutomaticLogin=flux$/AutomaticLogin=$INSTALL_USER/" system/gdm-custom.conf | sudo tee /etc/gdm3/custom.conf >/dev/null
sudo chown root:root /etc/gdm3/custom.conf
sudo chmod 0644 /etc/gdm3/custom.conf
sed "s/^flux /$INSTALL_USER /" system/main-server-gpu-control.sudoers | sudo tee /etc/sudoers.d/main-server-gpu-control >/dev/null
sed "s/^flux /$INSTALL_USER /" system/main-server-fan-control.sudoers | sudo tee /etc/sudoers.d/main-server-fan-control >/dev/null
sed "s/^flux /$INSTALL_USER /" system/main-server-desktop-toggle.sudoers | sudo tee /etc/sudoers.d/main-server-desktop-toggle >/dev/null
sed "s/^flux /$INSTALL_USER /" system/main-server-os-boot.sudoers | sudo tee /etc/sudoers.d/main-server-os-boot >/dev/null
sed "s/^flux /$INSTALL_USER /" system/main-server-nas.sudoers | sudo tee /etc/sudoers.d/main-server-nas >/dev/null
sudo chown root:root /etc/sudoers.d/main-server-gpu-control /etc/sudoers.d/main-server-fan-control /etc/sudoers.d/main-server-desktop-toggle /etc/sudoers.d/main-server-os-boot /etc/sudoers.d/main-server-nas
sudo chmod 0440 /etc/sudoers.d/main-server-gpu-control /etc/sudoers.d/main-server-fan-control /etc/sudoers.d/main-server-desktop-toggle /etc/sudoers.d/main-server-os-boot /etc/sudoers.d/main-server-nas
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-gpu-control
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-fan-control
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-desktop-toggle
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-os-boot
sudo /usr/sbin/visudo -cf /etc/sudoers.d/main-server-nas
printf 'nct6775\n' | sudo tee /etc/modules-load.d/main-server-fan.conf >/dev/null
sudo modprobe nct6775 || true
sudo systemctl daemon-reload
sudo systemctl enable gpu-tune.service
sudo /usr/local/sbin/main-server-linux-setup apply gpu_services

echo "main_server 사용자 서비스 설치..."
mkdir -p "$HOME/.config/systemd/user"
UNIT_PATH="$HOME/.config/systemd/user/main_server.service"
sed "s|/srv/main_server_new|$PWD|g" system/main_server.service > "$UNIT_PATH"
systemctl --user daemon-reload
systemctl --user enable --now main_server.service

# Linux에서는 GPU 팬을 제어하지 않는다. 과거 버전의 사용자 서비스를 제거한다.
systemctl --user disable --now gpu-fan-apply.service 2>/dev/null || true
if [ -f "$HOME/.config/systemd/user/gpu-fan-apply.service" ]; then
  mv "$HOME/.config/systemd/user/gpu-fan-apply.service" \
    "$HOME/.config/systemd/user/gpu-fan-apply.service.disabled-by-main-server"
fi
systemctl --user daemon-reload

DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || true)
if [ -z "$DESKTOP_DIR" ]; then
  DESKTOP_DIR="$HOME/Desktop"
fi
mkdir -p "$DESKTOP_DIR"
sed "s|/srv/main_server_new|$PWD|g" system/main_server_console.desktop > "$DESKTOP_DIR/main_service.desktop"
chmod +x "$DESKTOP_DIR/main_service.desktop"
gio set "$DESKTOP_DIR/main_service.desktop" metadata::trusted true 2>/dev/null || true

echo "완료. main_server 서비스가 실행 중이며 바탕화면에 main_service.desktop을 만들었습니다."
