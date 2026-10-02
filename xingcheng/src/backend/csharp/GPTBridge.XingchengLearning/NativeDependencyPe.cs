// NativeDependencyPe.cs — minimal PE import-directory reader for the
// NativeDependencyGate (Native-Only Architecture P0).
//
// The gate audits what a built binary actually links: the static import
// table is the ground truth for "linked". A module reached only via
// LoadLibrary (the driver-only CUDA lane loads nvcuda.dll that way)
// never appears here — which is exactly the distinction
// driver_api_only has to prove. Symbol-level auditing is out of scope
// for the P0 gate; a malformed table returns null and the caller treats
// the file itself as a finding.

using System.Text;

namespace GPTBridge.XingchengLearning;

internal static class NativeDependencyPe
{
    private const int ImportDirectoryIndex = 1;

    /// <summary>Return the lowercased static-import module names of a PE
    /// file, or null when the file cannot be read/parsed as a PE.</summary>
    public static List<string>? ImportModules(string path)
    {
        byte[] f;
        try { f = File.ReadAllBytes(path); }
        catch { return null; }
        try { return Parse(f); }
        catch { return null; }
    }

    private static ushort U16(byte[] b, int o)
        => (ushort)(b[o] | (b[o + 1] << 8));
    private static uint U32(byte[] b, int o)
        => (uint)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) |
                  ((uint)b[o + 3] << 24));

    private static List<string>? Parse(byte[] f)
    {
        if (f.Length < 0x40 || f[0] != 'M' || f[1] != 'Z') return null;
        int pe = (int)U32(f, 0x3C);
        if (pe <= 0 || pe + 24 > f.Length ||
            f[pe] != 'P' || f[pe + 1] != 'E') return null;
        int sections = U16(f, pe + 6);
        int optSize = U16(f, pe + 20);
        int opt = pe + 24;
        if (opt + optSize > f.Length) return null;
        ushort magic = U16(f, opt);
        int dirBase = magic switch
        {
            0x10B => opt + 96,    // PE32
            0x20B => opt + 112,   // PE32+
            _ => -1,
        };
        if (dirBase < 0) return null;
        int dirOff = dirBase + ImportDirectoryIndex * 8;
        if (dirOff + 8 > opt + optSize) return new List<string>();
        uint impRva = U32(f, dirOff);
        if (impRva == 0) return new List<string>();

        // section table follows the optional header
        int sec = opt + optSize;
        if (sec + sections * 40 > f.Length) return null;
        int RvaToOff(uint rva)
        {
            for (int i = 0; i < sections; i++)
            {
                int s = sec + i * 40;
                uint vsize = U32(f, s + 8);
                uint vaddr = U32(f, s + 12);
                uint rawsize = U32(f, s + 16);
                uint rawptr = U32(f, s + 20);
                uint span = Math.Max(vsize, rawsize);
                if (rva >= vaddr && rva < vaddr + span)
                    return (int)(rawptr + (rva - vaddr));
            }
            return -1;
        }

        var modules = new List<string>();
        int desc = RvaToOff(impRva);
        if (desc < 0) return null;
        for (int i = 0; i < 4096 && desc + i * 20 + 20 <= f.Length; i++)
        {
            int d = desc + i * 20;
            uint nameRva = U32(f, d + 12);
            if (nameRva == 0 && U32(f, d) == 0 && U32(f, d + 16) == 0)
                break;
            int nameOff = RvaToOff(nameRva);
            if (nameOff < 0 || nameOff >= f.Length) continue;
            int end = Array.IndexOf<byte>(f, 0, nameOff);
            if (end < 0) end = f.Length;
            if (end - nameOff > 260) return null;
            modules.Add(Encoding.ASCII
                .GetString(f, nameOff, end - nameOff).ToLowerInvariant());
        }
        return modules;
    }
}
