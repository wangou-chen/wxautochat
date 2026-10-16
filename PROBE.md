# wxautochat 可行性探测结论

探测时间：2026-10-16
环境：Windows 11 (26100) / 微信 4.1.0.30 (pid 27128, `D:\Program Files (x86)\Tencent\Weixin`) / Python 3.14 / PowerShell 5.1

## 结论速览

| 技术路径 | 结果 | 证据 |
|---|---|---|
| 微信 4.x 主界面 UIA 控件树 | **不可用** | 见 §1 |
| 自写 ctypes 绑定驱动 UIA | **不可行** | 见 §2 |
| 截图 PrintWindow + 内置 OCR | **可用** | 见 §3 |
| 数据库读取消息 | **可用**（已有 wxarchive） | 见 §4 |

## §1 微信 4.x 主界面在 UIA 下不可见（决定性）

```
顶层元素: pid=27128 class=Qt51514QWindowIcon type=Window name=[微信] rect=1011,147,728x752
  Children    数 = 1     -> Pane class=Qt51514QWindowIcon name=[Weixin]
  Descendants 数 = 1     -> 整棵树只有它自己
```

对照 —— Electron 子窗口（同一进程树的 `Chrome_WidgetWin_0`, pid=23108）：
```
  Descendants 数 = 33
    Pane   class=RootView
    Edit   class=OmniboxViewViews
    Button class=ImageButton  name=[最小化] / [最大化] / [关闭]
    ...
```

**判定**：检测方法本身有效（Electron 窗口有完整 UIA 树）；微信 Qt 主客户端
**没有启用 Qt 无障碍桥**，因此消息列表、输入框、发送按钮在 UIA 中**全部不存在**。

**推论**：一切基于 UIA 的方案（`uiautomation` / `pywinauto` / .NET UIAutomation）对
微信 4.x 聊天界面**无效**。网上声称支持的包需重新验证。

## §2 直接调用 UIAutomationCore.dll 不可行

| 尝试 | 结果 |
|---|---|
| `CoCreateInstance(CLSID_CUIAutomation)` | `0x80040154` REGDB_E_CLASSNOTREG（类未注册） |
| 注册表 `HKLM\SOFTWARE\Classes\CLSID\{FF48DBA4-...}` | 不存在 |
| 导出 `DllGetClassObject` + `IClassFactory::CreateInstance` | `0x80040111` CLASS_E_CLASSNOTAVAILABLE |
| `UIAutomationCore.dll` 文件 | 存在，4.3 MB，版本 7.2.26100.1 |

**判定**：放弃零依赖自写绑定。

## §3 截图 + 内置 OCR 可用

- **抓图**：`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` 成功
  - `CopyFromScreen` 抓到的是**屏幕区域**，窗口被遮挡时拿到的是别的窗口
    （实测抓到 `RGB(40,50,40)` 单色，391 色 vs 32 色对照明显）
  - `PrintWindow` 直接抓窗口内容，实测 391 种颜色、`RGB(237,237,237)` 为主 —— 真实界面
- **OCR**：`Windows.Media.Ocr`（系统内置，离线免费）
  - 已装语言：`en-GB`, `en-US`, `ja`, **`zh-Hans-CN`**
  - PS 5.1 调用方式：`Add-Type -AssemblyName System.Runtime.WindowsRuntime`
    后用 `System.WindowsRuntimeSystemExtensions::AsTask` 等待（轮询 `Status` **无效**）
  - 实测识别 26 行，含坐标

**OCR 质量缺陷（实测）**：字符间插空格；`pubg勇敢牛牛` -> `pubgæ 敢`；
`[图片]` -> `[ 片 ]`（丢字）。**结论：OCR 可用于定位与粗略校验，不适合作为消息正文的唯一来源。**

## §4 推荐架构

```
读（实时）  轮询 live DB：复制 .db + -wal -> 增量子解密 -> 取新消息      [可靠]
读（界面）  PrintWindow 截图 + OCR -> 判断当前会话 / 粗略校验            [辅助]
拟          风格卡片 + few-shot + LLM 出草稿                            [待建]
发          SetForegroundWindow + 剪贴板粘贴 + SendInput Enter          [待建]
校验        再读 DB，确认自己的 wxid 发出了目标文本                      [可靠]
```

## 未决问题

1. 输入框聚焦：UIA 不可用 -> 需确定点击坐标策略，或验证键盘快捷键
2. 实时读取延迟：增量子解密能否做到秒级
3. 全自动发送的风控与误发防护
