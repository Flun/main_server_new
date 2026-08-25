# MainServer Windows fan helper

This x64 helper is the only process allowed to write motherboard Super I/O fan
controls. It uses LibreHardwareMonitorLib and the signed PawnIO kernel driver.
The Python manager communicates over an authenticated JSON-lines connection
bound to `127.0.0.1:8997`. A per-installation 256-bit token is readable only by
the current user, Administrators, and SYSTEM. The elevated helper is registered
as a highest-privilege per-user scheduled task and starts at logon.
The published helper is copied to `fan_helper/dist` and is self-contained, so a
fresh server machine does not need a separate .NET runtime or SDK.

Build from PowerShell:

```powershell
.\fan_helper\build.ps1
```

Normally no manual fan setup is required. On first `main_server` launch it
downloads the official PawnIO 2.2.0 installer only when missing, verifies its
pinned SHA-256 and Authenticode signer, then asks for one Windows UAC approval.
That elevated step silently installs the driver and registers the helper task.

For development, this optional script rebuilds the bundled helper and performs
the same setup:

```powershell
.\fan_helper\setup_windows.ps1
```

The built `fan_helper/dist/MainServer.FanHelper.exe` is self-contained, so a
fresh server does not need a separate .NET runtime or SDK. First-time PawnIO
installation needs internet access and UAC approval. Do not run Fan Control,
Armoury Crate fan tuning, or another Super I/O writer concurrently.

Every software PWM write is a 15-second lease. If the manager stops refreshing
the selected channel, the helper returns it to the motherboard default mode.
