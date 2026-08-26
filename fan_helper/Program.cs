using System.Diagnostics;
using System.IO.Pipes;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.RegularExpressions;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

namespace MainServer.FanHelper;

internal sealed class UpdateVisitor : IVisitor
{
    public void VisitComputer(IComputer computer) => computer.Traverse(this);
    public void VisitHardware(IHardware hardware)
    {
        hardware.Update();
        foreach (IHardware child in hardware.SubHardware)
            child.Accept(this);
    }
    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}

internal sealed class FanBackend : IDisposable
{
    private readonly object _gate = new();
    private readonly Computer _computer;
    private readonly UpdateVisitor _visitor = new();
    private readonly HashSet<string> _owned = new(StringComparer.OrdinalIgnoreCase);
    private readonly Timer _leaseTimer;
    private DateTime _lastWriteUtc = DateTime.MinValue;
    private const int LeaseSeconds = 15;

    public FanBackend()
    {
        _computer = new Computer { IsMotherboardEnabled = true, IsCpuEnabled = true, IsGpuEnabled = true };
        _computer.Open();
        _computer.Accept(_visitor);
        _leaseTimer = new Timer(_ => LeaseCheck(), null, 1000, 1000);
    }

    private IEnumerable<ISensor> Sensors() =>
        _computer.Hardware.SelectMany(h => h.Sensors.Concat(h.SubHardware.SelectMany(s => s.Sensors)));

    private ISensor? FindControl(string id) => Sensors().FirstOrDefault(s =>
        s.SensorType == SensorType.Control && s.Control is not null &&
        string.Equals(s.Identifier.ToString(), id, StringComparison.OrdinalIgnoreCase));

    private static bool FanControlRunning() => Process.GetProcessesByName("FanControl").Length > 0;

    public object Status()
    {
        lock (_gate)
        {
            _computer.Accept(_visitor);
            var all = Sensors().ToList();
            var channels = all.Where(s => s.SensorType == SensorType.Control && s.Control is not null)
                .Select(control =>
                {
                    ISensor? rpm = all.FirstOrDefault(s => s.Hardware == control.Hardware &&
                        s.SensorType == SensorType.Fan && s.Index == control.Index);
                    return new
                    {
                        id = control.Identifier.ToString(),
                        name = control.Name,
                        hardware = control.Hardware.Name,
                        index = control.Index,
                        percent = control.Value,
                        software_percent = control.Control!.SoftwareValue,
                        mode = control.Control.ControlMode.ToString().ToLowerInvariant(),
                        min_percent = control.Control.MinSoftwareValue,
                        max_percent = control.Control.MaxSoftwareValue,
                        rpm = rpm?.Value,
                        rpm_name = rpm?.Name,
                        owned = _owned.Contains(control.Identifier.ToString())
                    };
                }).ToArray();
            var gpuTemperatures = _computer.Hardware
                .Where(hardware => hardware.HardwareType.ToString().StartsWith("Gpu", StringComparison.Ordinal))
                .Select(hardware =>
                {
                    var temperatures = hardware.Sensors.Where(sensor => sensor.SensorType == SensorType.Temperature).ToArray();
                    ISensor? memory = temperatures.FirstOrDefault(sensor =>
                        sensor.Name.Contains("Memory Junction", StringComparison.OrdinalIgnoreCase))
                        ?? temperatures.FirstOrDefault(sensor => sensor.Name.Contains("Memory", StringComparison.OrdinalIgnoreCase));
                    ISensor? hotspot = temperatures.FirstOrDefault(sensor =>
                        sensor.Name.Contains("Hot Spot", StringComparison.OrdinalIgnoreCase)
                        || sensor.Name.Contains("Hotspot", StringComparison.OrdinalIgnoreCase));
                    ISensor? core = temperatures.FirstOrDefault(sensor =>
                        sensor.Name.Contains("Core", StringComparison.OrdinalIgnoreCase));
                    return new
                    {
                        identifier = hardware.Identifier.ToString(),
                        name = hardware.Name,
                        memory_temperature = memory?.Value,
                        hotspot_temperature = hotspot?.Value,
                        core_temperature = core?.Value,
                    };
                }).ToArray();
            var cpuTemperatureSensors = _computer.Hardware
                .Where(hardware => hardware.HardwareType == HardwareType.Cpu)
                .SelectMany(hardware => hardware.Sensors.Concat(hardware.SubHardware.SelectMany(child => child.Sensors)))
                .Where(sensor => sensor.SensorType == SensorType.Temperature && sensor.Value.HasValue)
                .ToArray();
            ISensor? cpuTemperature = cpuTemperatureSensors.FirstOrDefault(sensor =>
                    sensor.Name.Contains("Package", StringComparison.OrdinalIgnoreCase))
                ?? cpuTemperatureSensors.FirstOrDefault(sensor =>
                    sensor.Name.Contains("Tctl", StringComparison.OrdinalIgnoreCase)
                    || sensor.Name.Contains("Tdie", StringComparison.OrdinalIgnoreCase))
                ?? cpuTemperatureSensors.OrderByDescending(sensor => sensor.Value).FirstOrDefault();
            return new
            {
                ok = true,
                pawnio_installed = LibreHardwareMonitor.PawnIo.PawnIo.IsInstalled,
                pawnio_version = LibreHardwareMonitor.PawnIo.PawnIo.Version?.ToString(),
                conflict = FanControlRunning(),
                lease_seconds = LeaseSeconds,
                channels,
                gpu_temperatures = gpuTemperatures,
                cpu_temperature = cpuTemperature?.Value,
                cpu_temperature_name = cpuTemperature?.Name
            };
        }
    }

    public object Set(string id, float percent)
    {
        lock (_gate)
        {
            if (FanControlRunning())
                throw new InvalidOperationException("Fan Control이 실행 중입니다. Super I/O 충돌 방지를 위해 먼저 종료하세요.");
            _computer.Accept(_visitor);
            ISensor control = FindControl(id) ?? throw new ArgumentException($"팬 제어 채널을 찾을 수 없습니다: {id}");
            if (percent is < 0 or > 100)
                throw new ArgumentOutOfRangeException(nameof(percent), "PWM은 0~100%여야 합니다.");
            float min = control.Control!.MinSoftwareValue;
            float max = control.Control.MaxSoftwareValue;
            float requestedPercent = percent;
            percent = Math.Clamp(percent, min, max);
            control.Control.SetSoftware(percent);
            _owned.Add(id);
            _lastWriteUtc = DateTime.UtcNow;
            return new { ok = true, id, percent, requested_percent = requestedPercent, lease_seconds = LeaseSeconds };
        }
    }

    public object Reset(string? id = null)
    {
        lock (_gate)
        {
            IEnumerable<string> targets = string.IsNullOrWhiteSpace(id) ? _owned.ToArray() : new[] { id };
            foreach (string target in targets)
            {
                ISensor? control = FindControl(target);
                control?.Control?.SetDefault();
                _owned.Remove(target);
            }
            return new { ok = true, reset = targets.ToArray() };
        }
    }

    private static string RunNvidiaSmi(params string[] arguments)
    {
        var start = new ProcessStartInfo
        {
            FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "nvidia-smi.exe"),
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (string argument in arguments)
            start.ArgumentList.Add(argument);
        using Process process = Process.Start(start) ?? throw new InvalidOperationException("nvidia-smi를 시작하지 못했습니다.");
        string stdout = process.StandardOutput.ReadToEnd();
        string stderr = process.StandardError.ReadToEnd();
        if (!process.WaitForExit(20000))
        {
            try { process.Kill(true); } catch { }
            throw new TimeoutException("nvidia-smi 실행 시간이 초과되었습니다.");
        }
        if (process.ExitCode != 0)
            throw new InvalidOperationException((stderr + Environment.NewLine + stdout).Trim());
        return stdout.Trim();
    }

    private static string RunBcdEdit(params string[] arguments)
    {
        var start = new ProcessStartInfo
        {
            FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "bcdedit.exe"),
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (string argument in arguments)
            start.ArgumentList.Add(argument);
        using Process process = Process.Start(start) ?? throw new InvalidOperationException("bcdedit를 시작하지 못했습니다.");
        string stdout = process.StandardOutput.ReadToEnd();
        string stderr = process.StandardError.ReadToEnd();
        if (!process.WaitForExit(15000))
        {
            try { process.Kill(true); } catch { }
            throw new TimeoutException("Windows UEFI 항목 조회 시간이 초과되었습니다.");
        }
        if (process.ExitCode != 0)
            throw new InvalidOperationException((stderr + Environment.NewLine + stdout).Trim());
        return stdout;
    }

    private static Dictionary<string, Dictionary<string, string>?> FirmwareTargets()
    {
        string output = RunBcdEdit("/enum", "firmware", "/v");
        var targets = new Dictionary<string, Dictionary<string, string>?>(StringComparer.OrdinalIgnoreCase)
        {
            ["windows"] = null,
            ["linux"] = null,
        };
        string? identifier = null;
        foreach (string raw in output.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
        {
            Match id = Regex.Match(raw, @"\{(?:[0-9a-fA-F-]{36}|bootmgr|fwbootmgr)\}");
            if (id.Success)
                identifier = id.Value;
            if (identifier is null)
                continue;
            if (raw.Contains("Windows Boot Manager", StringComparison.OrdinalIgnoreCase))
                targets["windows"] = new Dictionary<string, string> { ["id"] = identifier, ["description"] = "Windows Boot Manager" };
            else if (raw.Contains("Ubuntu", StringComparison.OrdinalIgnoreCase))
                targets["linux"] = new Dictionary<string, string> { ["id"] = identifier, ["description"] = "Ubuntu" };
        }
        return targets;
    }

    public object OsBootStatus()
    {
        var targets = FirmwareTargets();
        bool available = targets["windows"] is not null && targets["linux"] is not null;
        return new
        {
            ok = true, available, platform = "windows", current = "windows", targets,
            error = available ? null : "Windows Boot Manager 또는 Ubuntu UEFI 항목을 찾지 못했습니다",
        };
    }

    public object OsBootSet(string target)
    {
        if (target is not ("windows" or "linux"))
            throw new ArgumentException("지원하지 않는 부팅 대상입니다.");
        var targets = FirmwareTargets();
        Dictionary<string, string>? entry = targets[target];
        if (entry is null)
            throw new InvalidOperationException($"{target} UEFI 부팅 항목을 찾지 못했습니다.");
        RunBcdEdit("/set", "{fwbootmgr}", "bootsequence", entry["id"]);
        return new { ok = true, target, entry };
    }

    public object GpuTune(string uuid, int power, int clock, int fan)
    {
        if (!Regex.IsMatch(uuid, @"^GPU-[A-Za-z0-9-]+$"))
            throw new ArgumentException("GPU UUID가 올바르지 않습니다.");
        if (power is < 1 or > 2000)
            throw new ArgumentOutOfRangeException(nameof(power), "전력 제한값이 올바르지 않습니다.");
        if (clock is < 100 or > 10000)
            throw new ArgumentOutOfRangeException(nameof(clock), "코어 클럭값이 올바르지 않습니다.");
        if (fan != 0 && fan is < 20 or > 100)
            throw new ArgumentOutOfRangeException(nameof(fan), "팬 값은 0 또는 20~100%여야 합니다.");

        string supported = RunNvidiaSmi(
            "-i", uuid, "--query-supported-clocks=graphics", "--format=csv,noheader,nounits");
        int[] supportedClocks = supported.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(value => int.TryParse(value.Trim(), out int parsed) ? parsed : 0)
            .Where(value => value > 0)
            .ToArray();
        if (supportedClocks.Length == 0)
            throw new InvalidOperationException("GPU의 지원 코어 클럭 범위를 읽지 못했습니다.");
        int minClock = supportedClocks.Min();
        int maxClock = supportedClocks.Max();
        if (clock < minClock || clock > maxClock)
            throw new ArgumentOutOfRangeException(nameof(clock), $"코어 상한은 {minClock}~{maxClock}MHz여야 합니다.");

        RunNvidiaSmi("-i", uuid, "-pl", power.ToString());
        // This is a dynamic range, not a fixed clock. The GPU may downclock to
        // its lowest supported clock while idle and cannot boost above `clock`.
        RunNvidiaSmi("-i", uuid, "-lgc", $"{minClock},{clock}");
        var warnings = new List<string>();
        if (fan > 0)
        {
            try { RunNvidiaSmi("-i", uuid, "--fan", fan.ToString()); }
            catch (Exception error) { warnings.Add($"팬 설정은 이 GPU/드라이버에서 적용되지 않았습니다: {error.Message}"); }
        }
        return new { ok = true, uuid, power, clock, min_clock = minClock, fan, warnings };
    }

    private void LeaseCheck()
    {
        try
        {
            lock (_gate)
            {
                if (_owned.Count == 0 || DateTime.UtcNow - _lastWriteUtc <= TimeSpan.FromSeconds(LeaseSeconds))
                    return;
                Reset();
            }
        }
        catch { }
    }

    public void Dispose()
    {
        _leaseTimer.Dispose();
        try { Reset(); } catch { }
        _computer.Close();
    }
}

internal static class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
    private static void Log(string message)
    {
        try { File.AppendAllText(Path.Combine(AppContext.BaseDirectory, "fan_helper.log"), $"{DateTime.Now:O} {message}{Environment.NewLine}"); }
        catch { }
    }

    private static void Reply(TextWriter writer, object value)
    {
        writer.WriteLine(JsonSerializer.Serialize(value, JsonOptions));
        writer.Flush();
    }

    private static void RunProtocol(TextReader reader, TextWriter writer, FanBackend backend, string? expectedToken = null)
    {
        string? line;
        while ((line = reader.ReadLine()) is not null)
        {
            // Never log the JSON body: authenticated TCP requests contain the secret.
            Log("request received");
            try
            {
                using JsonDocument doc = JsonDocument.Parse(line);
                JsonElement root = doc.RootElement;
                if (expectedToken is not null)
                {
                    string supplied = root.TryGetProperty("token", out JsonElement tokenElement) ? tokenElement.GetString() ?? "" : "";
                    if (!CryptographicOperations.FixedTimeEquals(
                        System.Text.Encoding.UTF8.GetBytes(supplied),
                        System.Text.Encoding.UTF8.GetBytes(expectedToken)))
                        throw new UnauthorizedAccessException("팬 헬퍼 인증 토큰이 올바르지 않습니다.");
                }
                string command = root.GetProperty("command").GetString() ?? "";
                object result = command switch
                {
                    "status" => backend.Status(),
                    "set" => backend.Set(root.GetProperty("id").GetString() ?? "", root.GetProperty("percent").GetSingle()),
                    "reset" => backend.Reset(root.TryGetProperty("id", out JsonElement id) ? id.GetString() : null),
                    "gpu_tune" => backend.GpuTune(
                        root.GetProperty("uuid").GetString() ?? "",
                        root.GetProperty("power").GetInt32(),
                        root.GetProperty("clock").GetInt32(),
                        root.GetProperty("fan").GetInt32()),
                    "os_boot_status" => backend.OsBootStatus(),
                    "os_boot_set" => backend.OsBootSet(root.GetProperty("target").GetString() ?? ""),
                    "ping" => new { ok = true, protocol = 1 },
                    _ => throw new ArgumentException($"지원하지 않는 명령입니다: {command}")
                };
                Reply(writer, result);
                Log("reply ok");
            }
            catch (Exception error)
            {
                Reply(writer, new { ok = false, error = error.Message, type = error.GetType().Name });
                Log($"reply error {error}");
            }
        }
    }

    public static int Main(string[] args)
    {
        if (args.Contains("--self-test"))
        {
            Reply(Console.Out, new { ok = true, protocol = 1, runtime = Environment.Version.ToString() });
            return 0;
        }

        using var singleton = new Mutex(true, "Local\\MainServer.FanHelper", out bool created);
        if (!created)
        {
            Reply(Console.Out, new { ok = false, error = "팬 헬퍼가 이미 실행 중입니다." });
            return 2;
        }

        try
        {
            using var backend = new FanBackend();
            if (args.Contains("--tcp"))
            {
                int tokenIndex = Array.IndexOf(args, "--token-file");
                if (tokenIndex < 0 || tokenIndex + 1 >= args.Length)
                    throw new ArgumentException("--tcp에는 --token-file 경로가 필요합니다.");
                string expectedToken = File.ReadAllText(args[tokenIndex + 1]).Trim();
                var listener = new TcpListener(IPAddress.Loopback, 8997);
                listener.Start(1);
                Log("tcp listening 127.0.0.1:8997");
                while (true)
                {
                    using TcpClient client = listener.AcceptTcpClient();
                    client.NoDelay = true;
                    Log("tcp connected");
                    using NetworkStream stream = client.GetStream();
                    using var reader = new StreamReader(stream, new System.Text.UTF8Encoding(false), false, 4096, true);
                    using var writer = new StreamWriter(stream, new System.Text.UTF8Encoding(false), 4096, true) { AutoFlush = true };
                    try { RunProtocol(reader, writer, backend, expectedToken); }
                    finally { try { backend.Reset(); } catch { } }
                }
            }
            if (args.Contains("--pipe"))
            {
                while (true)
                {
                    var security = new PipeSecurity();
                    SecurityIdentifier? userSid = WindowsIdentity.GetCurrent().User;
                    if (userSid is not null)
                        security.AddAccessRule(new PipeAccessRule(userSid, PipeAccessRights.ReadWrite, AccessControlType.Allow));
                    security.AddAccessRule(new PipeAccessRule(
                        new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                        PipeAccessRights.FullControl, AccessControlType.Allow));
                    using var pipe = NamedPipeServerStreamAcl.Create(
                        "MainServerFanHelper", PipeDirection.InOut, 1,
                        PipeTransmissionMode.Byte, PipeOptions.WriteThrough,
                        4096, 4096, security);
                    Log($"waiting pipe sid={userSid}");
                    pipe.WaitForConnection();
                    Log("pipe connected");
                    using var reader = new StreamReader(pipe, new System.Text.UTF8Encoding(false), false, 4096, true);
                    using var writer = new StreamWriter(pipe, new System.Text.UTF8Encoding(false), 4096, true) { AutoFlush = true };
                    try
                    {
                        RunProtocol(reader, writer, backend);
                    }
                    finally
                    {
                        try { backend.Reset(); } catch { }
                    }
                }
            }
            RunProtocol(Console.In, Console.Out, backend);
            return 0;
        }
        catch (Exception error)
        {
            Reply(Console.Out, new { ok = false, error = error.Message, type = error.GetType().Name });
            return 1;
        }
    }
}
