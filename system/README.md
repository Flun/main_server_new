# Ubuntu system integration

Tracked recovery copies for files installed outside the repository:

- `main_server.service` → `~/.config/systemd/user/main_server.service`
- `main_server_console.desktop` → `~/바탕화면/main_server_console.desktop`
- `main-server-gpu-control` → `/usr/local/sbin/main-server-gpu-control`
- `gpu-tune.sh` → `/usr/local/sbin/gpu-tune.sh`
- `gpu-tune.service` → `/etc/systemd/system/gpu-tune.service`
- `main-server-gpu-control.sudoers` → `/etc/sudoers.d/main-server-gpu-control`
- `20-nvidia-coolbits.conf` → `/etc/X11/xorg.conf.d/20-nvidia-coolbits.conf`

The helper writes validated persistent values to `/etc/main-server/gpu-tune.conf`.
Fan control also requires the existing driver-matched
`/usr/local/bin/nvidia-settings-610` and its GTK libraries.
