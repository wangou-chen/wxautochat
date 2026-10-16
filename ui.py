# -*- coding: utf-8 -*-
"""Windows UI 原语：窗口定位与激活、截图+OCR、剪贴板、键鼠输入。

**为什么不用 UIA**：微信 4.x 主界面是 Qt 自绘，UIA 树只有 1 个节点
（实测 `Descendants 数 = 1`），拿不到输入框与消息列表。所以走
「窗口激活 + PrintWindow 截图 + OCR 定位 + 剪贴板粘贴 + SendInput 按键」。

本模块只提供无状态原语，不含发送决策逻辑（那在 sender.py）。

踩过的坑（勿改）：
  * **ctypes 必须声明 restype/argtypes**。64 位下 `GlobalAlloc` 返回的句柄
    默认被截断成 32 位，`GlobalLock` 会直接访问违例崩溃。
  * **截图必须用 PrintWindow(PW_RENDERFULLCONTENT=2)**。`CopyFromScreen`
    抓的是屏幕像素，窗口被遮挡时抓到的是别的窗口（实测抓到单色 RGB(40,50,40)）。
  * **OCR 结果走文件而不是 stdout**。PS 5.1 的 stdout 是 ANSI，中文会乱码；
    让 PowerShell 写 UTF-8 JSON 文件再由 Python 读，彻底绕开编码问题。
  * PowerShell 辅助脚本必须存为 **UTF-8 with BOM**，否则 PS 5.1 解析含中文的
    脚本会报语法错误。
"""
import ctypes
import ctypes.wintypes as w
import json
import os
import subprocess
import tempfile
import time

from . import config

u = ctypes.windll.user32
k = ctypes.windll.kernel32

WINDOW_TITLE = "微信"

# ---------------------------------------------------------------- ctypes 声明

k.GlobalAlloc.restype = ctypes.c_void_p
k.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalLock.argtypes = [ctypes.c_void_p]
k.GlobalUnlock.argtypes = [ctypes.c_void_p]
u.SetClipboardData.restype = ctypes.c_void_p
u.SetClipboardData.argtypes = [w.UINT, ctypes.c_void_p]
u.GetClipboardData.restype = ctypes.c_void_p
u.GetClipboardData.argtypes = [w.UINT]
u.OpenClipboard.argtypes = [ctypes.c_void_p]
u.CloseClipboard.restype = ctypes.c_bool
u.GetWindowTextLengthW.argtypes = [w.HWND]
u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u.SetForegroundWindow.argtypes = [w.HWND]
u.GetForegroundWindow.restype = w.HWND
u.WindowFromPoint.restype = w.HWND
u.WindowFromPoint.argtypes = [w.POINT]
u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# ---------------------------------------------------------------- DPI

_dpi_done = False


def ensure_dpi_aware():
    """让进程 DPI 感知，否则 GetWindowRect 与截图坐标会与物理像素不一致。"""
    global _dpi_done
    if _dpi_done:
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                              # noqa: BLE001
        try:
            u.SetProcessDPIAware()
        except Exception:                                          # noqa: BLE001
            pass
    _dpi_done = True


# ---------------------------------------------------------------- 窗口

class WinInfo(object):
    __slots__ = ("hwnd", "pid", "cls", "title", "rect")

    def __init__(self, hwnd, pid, cls, title, rect):
        self.hwnd, self.pid, self.cls, self.title, self.rect = \
            hwnd, pid, cls, title, rect

    @property
    def width(self):
        return self.rect[2] - self.rect[0]

    @property
    def height(self):
        return self.rect[3] - self.rect[1]

    @property
    def origin(self):
        return (self.rect[0], self.rect[1])

    def __repr__(self):
        return "<Win 0x%08X %s %dx%d @%s>" % (self.hwnd, self.cls,
                                              self.width, self.height,
                                              self.origin)


def find_wechat(require_qt: bool = True):
    """找到微信主窗口。返回 WinInfo 或 None。

    微信 4.x 主界面类名是 `Qt51514QWindowIcon`（Qt5），标题恒为「微信」。
    会话/小程序等子窗口是 `Chrome_WidgetWin_0`（Electron），标题也是「微信」，
    故优先取 Qt 那个。
    """
    ensure_dpi_aware()
    rows = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

    def cb(hwnd, l):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        t = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, t, n + 1)
        if t.value != WINDOW_TITLE:
            return True
        c = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, c, 256)
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = w.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        rows.append(WinInfo(hwnd, pid.value, c.value, t.value,
                            (r.left, r.top, r.right, r.bottom)))
        return True

    u.EnumWindows(PROC(cb), 0)
    if not rows:
        return None
    qt = [x for x in rows if "Qt" in x.cls]
    if qt:
        # 取面积最大的 Qt 窗口（主界面）
        return max(qt, key=lambda x: x.width * x.height)
    return None if require_qt else rows[0]


def foreground_hwnd():
    return u.GetForegroundWindow()


def is_foreground(hwnd) -> bool:
    return foreground_hwnd() == hwnd


def activate(hwnd, timeout: float = 2.0) -> bool:
    """把窗口激活到前台并校验。返回是否真的成为前台窗口。

    Windows 会拒绝非前台进程的 SetForegroundWindow，所以要校验而不是信返回值。
    失败时用一次 ALT 轻敲解除前台锁（常见绕过手段），仍失败则返回 False。
    """
    ensure_dpi_aware()
    if is_foreground(hwnd):
        return True
    u.ShowWindow(hwnd, 9)                       # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_foreground(hwnd):
            return True
        time.sleep(0.05)
    # 兜底：发一个 ALT 键事件，让系统允许本进程设置前台窗口
    _send_vk(0x12, up=False)                    # ALT down
    _send_vk(0x12, up=True)                     # ALT up
    time.sleep(0.05)
    u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_foreground(hwnd):
            return True
        time.sleep(0.05)
    return False


def window_at(x, y):
    """返回屏幕坐标 (x,y) 处的窗口句柄（用于校验点击落点）。"""
    return u.WindowFromPoint(w.POINT(int(x), int(y)))


# ---------------------------------------------------------------- 剪贴板

def clip_set(text: str) -> bool:
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            k.GlobalUnlock(h)
        u.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        u.CloseClipboard()


def clip_get():
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


# ---------------------------------------------------------------- 输入

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTunion)]


INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

VK = {"CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "ENTER": 0x0D, "RETURN": 0x0D,
      "V": 0x56, "A": 0x41, "C": 0x43, "DEL": 0x2E, "ESC": 0x1B, "SPACE": 0x20}

u.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
u.SendInput.restype = w.UINT


def _send_vk(vk, up=False):
    inp = INPUT(type=INPUT_KEYBOARD,
                u=_INPUTunion(ki=KEYBDINPUT(wVk=vk, wScan=0,
                                            dwFlags=KEYEVENTF_KEYUP if up else 0,
                                            time=0, dwExtraInfo=None)))
    u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def key_combo(*names, delay: float = 0.03):
    """按组合键，如 key_combo("CTRL", "V")。"""
    vks = [VK[n.upper()] for n in names]
    for v in vks:
        _send_vk(v, up=False)
        time.sleep(delay)
    for v in reversed(vks):
        _send_vk(v, up=True)
        time.sleep(delay)


def press(name: str, times: int = 1, delay: float = 0.05):
    for _ in range(times):
        v = VK[name.upper()]
        _send_vk(v, up=False)
        time.sleep(delay)
        _send_vk(v, up=True)
        time.sleep(delay)


def click(x: int, y: int, settle: float = 0.25):
    """在屏幕坐标点击。"""
    ensure_dpi_aware()
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    inp = INPUT(type=INPUT_MOUSE,
                u=_INPUTunion(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                            dwFlags=MOUSEEVENTF_LEFTDOWN,
                                            time=0, dwExtraInfo=None)))
    u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.05)
    inp = INPUT(type=INPUT_MOUSE,
                u=_INPUTunion(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                                            dwFlags=MOUSEEVENTF_LEFTUP,
                                            time=0, dwExtraInfo=None)))
    u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(settle)


# ---------------------------------------------------------------- 截图 + OCR

_OCR_PS = r"""param([int]$Hwnd, [string]$OutDir)
$ErrorActionPreference = "Stop"
$Png  = Join-Path $OutDir "shot.png"
$Json = Join-Path $OutDir "shot.json"
if (Test-Path $Png)  { Remove-Item $Png -Force }
if (Test-Path $Json) { Remove-Item $Json -Force }
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PW {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
}
"@
# **必须在任何窗口测量之前**声明 DPI 感知。否则在 150% 缩放下
# GetWindowRect 返回虚拟化坐标（实测 728x683 vs 真实的 1092x1025），
# 由 OCR 坐标推出的点击位置会整体偏移 1.5 倍。
[void][PW]::SetProcessDPIAware()
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) {
  $m = $asTask.MakeGenericMethod($t); $x = $m.Invoke($null, @($op))
  $x.Wait(-1) | Out-Null; $x.Result
}

$h = [IntPtr]$Hwnd
$r = New-Object PW+RECT
[void][PW]::GetWindowRect($h, [ref]$r)
$wd = $r.R - $r.L; $ht = $r.B - $r.T
if ($wd -le 0 -or $ht -le 0) { throw "窗口尺寸无效" }

$bmp = New-Object System.Drawing.Bitmap($wd, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [PW]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc); $g.Dispose()
if (-not $ok) { throw "PrintWindow 失败" }
$bmp.Save($Png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

$f   = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Png)) ([Windows.Storage.StorageFile])
$st  = Await ($f.OpenAsync(0)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sb  = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$lang = New-Object Windows.Globalization.Language("zh-Hans-CN")
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $eng) { $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
$res = Await ($eng.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])

$lines = New-Object System.Collections.ArrayList
foreach ($l in $res.Lines) {
  $w0 = $l.Words | Select-Object -First 1
  $x = [int][double]$w0.BoundingRect.X
  $y = [int][double]$w0.BoundingRect.Y
  $ww = 0
  foreach ($ww0 in $l.Words) { $ww += [int][double]$ww0.BoundingRect.Width }
  [void]$lines.Add(@{ x = $x; y = $y; w = $ww; text = $l.Text })
}
$obj = @{ width = $wd; height = $ht; left = $r.L; top = $r.T; lines = $lines }
$txt = $obj | ConvertTo-Json -Depth 6 -Compress
[System.IO.File]::WriteAllText($Json, $txt, (New-Object System.Text.UTF8Encoding($false)))
"""

_ps_cache = None


def _ps_path():
    global _ps_cache
    if _ps_cache and os.path.exists(_ps_cache):
        return _ps_cache
    d = os.path.join(config.ROOT, "work")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "_ocr_helper.ps1")
    # PowerShell 5.1 需要 UTF-8 with BOM 才能正确解析含中文的脚本
    with open(p, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(_OCR_PS)
    _ps_cache = p
    return p


def capture_ocr(hwnd, tag: str = "shot", timeout: float = 60.0):
    """抓窗口内容并 OCR。返回 {width, height, left, top, lines:[{x,y,w,text}]}。

    lines 里的坐标是**窗口内相对坐标**；转屏幕坐标用 left+x, top+y。
    输出走文件而不是 stdout —— PS 5.1 的 stdout 是 ANSI，中文必然乱码。
    """
    d = os.path.join(config.ROOT, "work", "shots", tag)
    os.makedirs(d, exist_ok=True)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", _ps_path(), "-Hwnd", str(int(hwnd)), "-OutDir", d],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    js = os.path.join(d, "shot.json")
    if not os.path.exists(js):
        raise RuntimeError("OCR 失败：%s" % ((r.stderr or r.stdout or "")[:400]))
    with open(js, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["png"] = os.path.join(d, "shot.png")
    data["dir"] = d
    return data


def region_text(ocr: dict, x0, y0, x1, y1, join: str = "") -> str:
    """取 OCR 结果里落在某矩形内的文字。坐标用窗口内相对坐标。"""
    out = []
    for ln in ocr.get("lines") or []:
        x, y = ln.get("x", 0), ln.get("y", 0)
        if x0 <= x <= x1 and y0 <= y <= y1:
            out.append(ln.get("text") or "")
    return join.join(out)
