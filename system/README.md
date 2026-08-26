# Ubuntu system integration

Tracked recovery copies for files installed outside the repository:

- `main_server.service` → `~/.config/systemd/user/main_server.service`
- `main_server_console.desktop` → `~/바탕화면/main_service.desktop`
- `main-server-gpu-control` → `/usr/local/sbin/main-server-gpu-control`
- `main-server-fan-control.py` → `/usr/local/sbin/main-server-fan-control`
- `main-server-desktop-toggle` → `/usr/local/sbin/main-server-desktop-toggle`
- `gpu-tune.sh` → `/usr/local/sbin/gpu-tune.sh`
- `gpu-tune.service` → `/etc/systemd/system/gpu-tune.service`
- `main-server-gpu-control.sudoers` → `/etc/sudoers.d/main-server-gpu-control`
- `main-server-fan-control.sudoers` → `/etc/sudoers.d/main-server-fan-control`
- `main-server-desktop-toggle.sudoers` → `/etc/sudoers.d/main-server-desktop-toggle`
- `main-server-os-boot` → `/usr/local/sbin/main-server-os-boot`
- `main-server-os-boot.sudoers` → `/etc/sudoers.d/main-server-os-boot`
- `main-server-linux-setup` → `/usr/local/sbin/main-server-linux-setup`
- `main-server-linux-setup.sudoers` → `/etc/sudoers.d/main-server-linux-setup`
- `main-server-nas.sudoers` → `/etc/sudoers.d/main-server-nas`
- `gdm-custom.conf` → `/etc/gdm3/custom.conf`
- `sunshine.conf` → `~/.config/sunshine/sunshine.conf`
- `20-nvidia-coolbits.conf` → `/etc/X11/xorg.conf.d/20-nvidia-coolbits.conf`

`setup_env.sh` installs a root-owned allowlisted Linux setup helper and applies
the complete fresh-Ubuntu baseline: required packages, `multi-user.target`,
tty1, five-minute console blanking, SSH, systemd linger, fixed model mounts,
NAS prerequisites, and GPU/fan boot settings. The integrated settings page can
later reapply any subset with checkboxes. The manager's user service therefore
starts during boot without a CLI or graphical login. Starting GUI mode is
temporary; the next reboot still returns to CLI mode.

The integrated Git/GitHub section works on both Linux and Windows. Linux uses
the official GitHub CLI apt repository through the allowlisted setup helper;
Windows uses winget packages `Git.Git` and `GitHub.cli`. Device authentication
shows only the one-time code and GitHub URL in the dashboard. The user approves
that code directly on GitHub, after which `gh auth setup-git` runs automatically.

The main-page power menu consolidates AI service termination, Ubuntu GUI/CLI
switching, safe Git fast-forward updates, manager restart/stop, one-shot OS
switching, and host restart/shutdown. The root OS helper schedules reboot or
poweroff only after the HTTP response has reached the browser. Destructive
actions require a UI confirmation and a matching confirmation header at the
API boundary.

The same setup mounts the llama model volume at `/mnt/main-server-models` and
the shared ComfyUI model volume read-only at `/mnt/main-server-comfy`. UUID-based
fstab entries keep both paths stable without relying on a GNOME login or udisks.

The GPU helper writes one validated persistent file per GPU UUID under
`/etc/main-server/gpu-tune.d/`. The legacy index-based
`/etc/main-server/gpu-tune.conf` is deliberately ignored because a card insert
or reorder can make it target the wrong GPU. `TUNING_ENABLED=0` keeps a per-GPU record while
making both boot-time services skip that card. The CMP 170HX profile is selected
by PCI device ID (`20C2`/`2082`) even when the driver reports the generic name;
its safe default is 250 W with a 1410 MHz core cap. Its fixed HBM memory clock is
reported for visibility but is never changed by this helper.
Linux GPU tuning intentionally controls only power and core-clock limits. GPU
fan control remains on the existing Windows helper path and is neither exposed
nor invoked on Linux.

Compute mode starts `getty@tty1.service` and switches the physical console to
VT1 after stopping GDM. GUI mode stops that getty, starts GDM, and activates the
new seat0 graphical VT. GDM automatic login creates the `flux` GNOME session,
then the helper restarts GNOME Desktop Sharing and Sunshine against that new
session. Compute mode explicitly stops both capture services to release stale
X11/NvFBC resources. This avoids a remote-access deadlock at the physical user
chooser.

GUI recovery waits for `gnome-session-initialized.target`, the X11 GNOME Shell
service, and a working XRandR query, then allows four seconds for shell
extensions/input surfaces before optional remote services restart. Missing or
failed Sunshine/GNOME Remote Desktop units no longer turn an otherwise
successful GUI transition into an API failure.

Native GNOME system Remote Login owns TCP 3389. Sunshine/Moonlight remains the
low-latency remote path, and GNOME Desktop Sharing remains on 3390 for deliberate
takeover of the currently logged-in physical desktop.

## Hardware fan support

- NVIDIA settings are keyed by GPU UUID, not the boot-time GPU index. Adding or
  reordering GPUs therefore does not move a saved profile to a different card.
- Windows keeps the existing LibreHardwareMonitor/PawnIO motherboard and NVIDIA
  fan backend. Linux uses a separate `nct6775` hwmon backend and never invokes
  GPU fan control.
- On Linux, only PWM attributes on detected `nct*` Super I/O devices are exposed.
  The setup script loads `nct6775` at boot and installs a narrowly-scoped root
  helper; the web service cannot write arbitrary sysfs files.
- Each Linux manual PWM write is a renewable 15-second lease. The helper records
  the firmware-controlled state first and restores it automatically if the
  manager exits, hangs, or stops renewing the lease.
- Run `./setup_env.sh` after a fresh Ubuntu installation. It installs the Python
  and system dependencies, Linux helpers, sudo rules, the system/user
  services, and the desktop launcher. Motherboard header-to-channel mapping must
  still be identified once with the UI's manual fan finder because hwmon channel
  numbering is a motherboard/firmware property.
