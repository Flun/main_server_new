# Ubuntu system integration

Tracked recovery copies for files installed outside the repository:

- `main_server.service` → `~/.config/systemd/user/main_server.service`
- `main_server_console.desktop` → `~/바탕화면/main_server_console.desktop`
- `main-server-gpu-control` → `/usr/local/sbin/main-server-gpu-control`
- `main-server-desktop-toggle` → `/usr/local/sbin/main-server-desktop-toggle`
- `main-server-monitor-power` → `/usr/local/sbin/main-server-monitor-power`
- `gpu-tune.sh` → `/usr/local/sbin/gpu-tune.sh`
- `gpu-tune.service` → `/etc/systemd/system/gpu-tune.service`
- `gpu-fan-apply.service` → `~/.config/systemd/user/gpu-fan-apply.service`
- `main-server-gpu-control.sudoers` → `/etc/sudoers.d/main-server-gpu-control`
- `main-server-desktop-toggle.sudoers` → `/etc/sudoers.d/main-server-desktop-toggle`
- `main-server-monitor-power.sudoers` → `/etc/sudoers.d/main-server-monitor-power`
- `sunshine.conf` → `~/.config/sunshine/sunshine.conf`
- `sunshine-apps.json` → `~/.config/sunshine/apps.json`
- `20-nvidia-coolbits.conf` → `/etc/X11/xorg.conf.d/20-nvidia-coolbits.conf`

The GPU helper writes one validated persistent file per GPU UUID under
`/etc/main-server/gpu-tune.d/`. A legacy `/etc/main-server/gpu-tune.conf` is
migrated on the next apply.
Fan control also requires the existing driver-matched
`/usr/local/bin/nvidia-settings-610` and its GTK libraries.

The monitor power helper exposes independent force-off and force-on actions.
It uses DDC/CI VCP D6 to power down only the physical panels while keeping the
GPU topology alive. Wake sends DDC before looking for X11, so it also works in
compute/CLI mode; when X11 exists it additionally cycles each physical output
and restores the saved layout for LG DisplayPort wake reliability.

Compute mode starts `getty@tty1.service` and switches the physical console to
VT1 after stopping GDM. GUI mode stops that getty, starts GDM, and activates the
new seat0 graphical VT. This avoids leaving the NVIDIA framebuffer black while
the machine is otherwise healthy.

Native GNOME system Remote Login owns TCP 3389 again. Sunshine/Moonlight is the
preferred low-latency remote path and uses NvFBC capture plus NVENC on the RTX
3090. GNOME Desktop Sharing remains on 3390 for deliberate takeover of the
currently logged-in physical desktop.

## Hardware fan support

- NVIDIA settings are keyed by GPU UUID, not the boot-time GPU index. Adding or
  reordering GPUs therefore does not move a saved profile to a different card.
- One NVIDIA fan target write is sent to every fan exposed for that GPU. Passive
  cards simply report fan control as unavailable in the UI.
- A graphical-session oneshot reapplies NV-CONTROL fan settings after the X
  session becomes available; the system service continues to apply power and
  clock limits at boot.
- Motherboard 4-pin headers must first appear as writable `pwmN` files under a
  Linux hwmon device. Do not guess a header or force an unsupported Super I/O
  driver: an incorrect PWM enable mode or polarity can stop a fan. Once the
  board exposes a supported channel, add an explicit hwmon-chip + PWM-channel
  mapping and a fail-safe automatic fallback before exposing it in the UI.
