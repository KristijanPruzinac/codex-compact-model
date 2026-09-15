using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using System.Threading.Tasks;

class Launcher {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters {
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    static string Quote(string arg) {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') {
                result.Append('\\', slashes * 2 + 1); result.Append(c);
            } else { result.Append('\\', slashes); result.Append(c); }
            slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"');
        return result.ToString();
    }
    static async Task Relay(Stream input, Stream output) {
        var buffer = new byte[16384];
        int length;
        while ((length = await input.ReadAsync(buffer, 0, buffer.Length)) > 0) {
            await output.WriteAsync(buffer, 0, length);
            await output.FlushAsync();
        }
    }
    static int Main(string[] args) {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            var config = new JavaScriptSerializer().Deserialize<Dictionary<string,string>>(
                File.ReadAllText(Path.Combine(root, "launcher.json")));
            var job = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            int size = Marshal.SizeOf(limits);
            var pointer = Marshal.AllocHGlobal(size);
            Marshal.StructureToPtr(limits, pointer, false);
            bool ready = job != IntPtr.Zero && SetInformationJobObject(job, 9, pointer, (uint)size)
                && AssignProcessToJobObject(job, GetCurrentProcess());
            Marshal.FreeHGlobal(pointer);
            if (!ready) throw new Exception("Could not establish automatic cleanup for the Codex process.");
            var command = new StringBuilder("-u ").Append(Quote(Path.Combine(root, "router.py")));
            foreach (string arg in args) command.Append(" ").Append(Quote(arg));
            var start = new ProcessStartInfo(config["python"], command.ToString());
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardInput = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            var process = Process.Start(start);
            var output = Relay(process.StandardOutput.BaseStream, Console.OpenStandardOutput());
            var error = Relay(process.StandardError.BaseStream, Console.OpenStandardError());
            Task.Run(async () => {
                try { await Relay(Console.OpenStandardInput(), process.StandardInput.BaseStream); }
                catch (IOException) { }
                finally { try { process.StandardInput.Close(); } catch (InvalidOperationException) { } }
            });
            process.WaitForExit();
            Task.WaitAll(output, error);
            // Windows closes the job handle when this launcher exits, cleaning up every descendant.
            return process.ExitCode;
        } catch (Exception e) {
            Console.Error.WriteLine("Compaction Router: " + e.Message);
            return 1;
        }
    }
}
