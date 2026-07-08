# Present plugin: in-session display enumeration helper.
#
# When the OpenAVC server runs as a Windows service it lives in session 0,
# where the real monitors aren't visible (display enumeration there sees a
# placeholder console display). The plugin launches this script inside the
# signed-in user's session instead; it enumerates the active monitors via
# Win32 and writes them as JSON to -OutFile for the plugin to read back.
#
# Output shape (one entry per active monitor):
#   [{"id","name","x","y","width","height","primary"}]
# id is the monitor's device interface path — stable across reboots and
# replugs on the same connector.

param(
    [Parameter(Mandatory = $true)]
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace OpenAvcPresent
{
    public class OutputInfo
    {
        public string id;
        public string name;
        public int x;
        public int y;
        public int width;
        public int height;
        public bool primary;
    }

    public static class Outputs
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct MONITORINFOEX
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
            public string szDevice;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DISPLAY_DEVICE
        {
            public int cb;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
            public string DeviceName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string DeviceString;
            public uint StateFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string DeviceID;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string DeviceKey;
        }

        public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rect, IntPtr data);

        [DllImport("user32.dll")]
        public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc proc, IntPtr data);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFOEX info);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplayDevices(string device, uint devNum, ref DISPLAY_DEVICE dd, uint flags);

        // EDD_GET_DEVICE_INTERFACE_NAME: DeviceID becomes the stable
        // monitor device interface path (carries the EDID model code).
        private const uint EDD_GET_DEVICE_INTERFACE_NAME = 0x1;
        private const uint MONITORINFOF_PRIMARY = 0x1;

        public static List<OutputInfo> Enumerate()
        {
            var result = new List<OutputInfo>();
            MonitorEnumProc callback = delegate(IntPtr hMon, IntPtr hdc, ref RECT r, IntPtr d)
            {
                var info = new MONITORINFOEX();
                info.cbSize = Marshal.SizeOf(typeof(MONITORINFOEX));
                if (!GetMonitorInfo(hMon, ref info))
                    return true;

                string id = info.szDevice;
                string name = info.szDevice;
                var dd = new DISPLAY_DEVICE();
                dd.cb = Marshal.SizeOf(typeof(DISPLAY_DEVICE));
                if (EnumDisplayDevices(info.szDevice, 0, ref dd, EDD_GET_DEVICE_INTERFACE_NAME)
                    && !String.IsNullOrEmpty(dd.DeviceID))
                {
                    id = dd.DeviceID;
                    name = dd.DeviceString;
                    string lower = (name ?? "").ToLower();
                    if (String.IsNullOrEmpty(name) || lower == "generic pnp monitor" || lower == "generic non-pnp monitor")
                    {
                        string[] parts = dd.DeviceID.Split('#');
                        if (parts.Length >= 2 && parts[1].Length > 0)
                            name = parts[1];
                    }
                }

                var o = new OutputInfo();
                o.id = id;
                o.name = name;
                o.x = info.rcMonitor.Left;
                o.y = info.rcMonitor.Top;
                o.width = info.rcMonitor.Right - info.rcMonitor.Left;
                o.height = info.rcMonitor.Bottom - info.rcMonitor.Top;
                o.primary = (info.dwFlags & MONITORINFOF_PRIMARY) != 0;
                result.Add(o);
                return true;
            };
            EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            return result;
        }
    }
}
'@

$outputs = [OpenAvcPresent.Outputs]::Enumerate()
$objects = @($outputs | ForEach-Object {
    [pscustomobject]@{
        id      = $_.id
        name    = $_.name
        x       = $_.x
        y       = $_.y
        width   = $_.width
        height  = $_.height
        primary = $_.primary
    }
})

# Temp file + move so the plugin never reads a torn write.
$json = ConvertTo-Json -InputObject $objects -Compress
$tmp = "$OutFile.tmp"
Set-Content -Path $tmp -Value $json -Encoding UTF8
Move-Item -Path $tmp -Destination $OutFile -Force
