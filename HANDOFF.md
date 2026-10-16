# HANDOFF · 项目交接与运行状态

> 归档时间：2026-10-17
> 代码状态：`main` 分支，工作区干净，与远程同步
> 部署根目录（数据根）：见 `HANDOFF.local.md`（**不在本仓库内**，含真实会话名）

本文件是**交接文档**：接手的人只看这一份就能把系统跑起来、知道哪里有问题、下一步该做什么。
真实会话名、wxid、卡片的对应关系写在项目根目录外的 `HANDOFF.local.md`（故意不入库）。

---

## 1. 关闭时的状态（照此可原样恢复）

| 项 | 状态 |
|---|---|
| `reply` 实例 | **无**（没有任何 python 进程） |
| `STOP` 急停文件 | **不存在**（可以正常发送） |
| `work/_lock.json` 单实例锁 | **不存在**（干净，随时可启动） |
| 微信 | 运行中（自动回复要求它处于登录态，且停在目标会话） |
| 未提交改动 | 无（仓库干净、已推送） |

---

## 2. 最快的恢复方式

```powershell
cd <数据根>          # 即 wxautochat 包的父目录

# ① 自检（免费）：数据目录 / 密钥 / 库匹配 / 本机 wxid
python -m wxautochat probe

# ② 只出草稿、不发送（推荐的第一次试跑）
python -m wxautochat reply --chat "<会话名>" --no-live --once

# ③ 真正挂起来（默认配置 live=true）
python -m wxautochat reply --chat "<会话名>" --verbose
```

后台挂机 + 落日志：

```powershell
Start-Process -FilePath "python" `
  -ArgumentList @("-u","-m","wxautochat","reply","--chat","<会话名>","--verbose") `
  -RedirectStandardOutput ".\work\reply.out.log" `
  -RedirectStandardError  ".\work\reply.err.log" -WindowStyle Hidden
Get-Content .\work\reply.out.log -Tail 30 -Encoding UTF8 -Wait
```

停机两种方式（**STOP 是真·总开关**，会连生成一起停）：

```powershell
Set-Content .\STOP ""                        # 温和：循环顶部检测到就退出并释放锁
Remove-Item .\STOP -Force                   # 继续
Get-Process python | Stop-Process -Force    # 强制；之后必须删 work\_lock.json 再重启
```

**重启前自查**（不清锁会 `[WinError 32]` 崩）：

```powershell
Get-Process python -ErrorAction SilentlyContinue      # 必须为空
Remove-Item .\work\_lock.json -ErrorAction SilentlyContinue
```

---

## 3. 系统由什么组成（30 秒回顾）

```
微信 4.x 加密库
  │  P1 解密 + WAL 增量 + 文本规范化            （零 token）
  ▼
消息流 ─轮次切分─规则预筛─LLM门控─生成─输出硬过滤─UI自动化发送─数据库回读
  │                                              │
  └─ P2 风格画像（离线，¥0.02~0.05/张）→ 人格卡片   └─ 每次约 ¥0.001
```

- **P1 只读库**；**P2 只读库 + 调 API**；**只有 P3 操作界面**。
- 对话单位是**轮次**（静默窗口切分），支持多对一与一对多。
- **正确性只由"发送后回读数据库"保证**；OCR 只用于定位锚点。
- 三层防线：会话名核验（失败重拍）+ 严格锚点（定位不到就中止）+ 发送后回读。
  原则：**宁可漏发，绝不发错会话。**

细节见 `README.md`（含完整时序、配置体系、28 条实测坑）。

---

## 4. 配置在哪一层（改东西先看这里）

优先级：**命令行显式参数 > 会话配置 > 默认配置 > 内置默认**

| 层 | 位置 | 管什么 |
|---|---|---|
| 密钥 | `<数据根>\api.txt` | api_key / model / base_url |
| 路径 | `<数据根>\config.local.json`（可省） | 数据目录、work 目录等 |
| 全局行为 | `wxautochat\behavior.py` + `<数据根>\behavior.local.json` | 手写硬约束：内容要求、情绪价值、输出形式、正确性、反重复、跳过规则 |
| 按会话行为 | `conversations\<会话>.json` 的 `behavior` 段 | `emotion` 整体替换 / `xxx_extra` 追加 / `extra_facts` 追加 |
| 按会话策略 | `conversations\<会话>.json` | 卡片、是否真发、节奏、连发上限与随机间隔、丢弃策略、用户给定事实 |
| **默认** | 包内 `defaults/`（项目根同名文件优先） | 未配置的会话走这里：默认策略 + 私聊/群聊两张默认人格卡 |

```powershell
python -m wxautochat convo --list                 # 看有哪些会话配置 + 默认配置
python -m wxautochat convo --chat "<会话名>"        # 看某会话当前生效的配置
python -m wxautochat convo --chat "<新会话>" --write # 从默认派生一份专属配置
```

---

## 5. 待办（按优先级）

1. **三张群聊卡片是用旧版 P2 提示词生成的**：缺 `emotional_value`（P3 里那段显示「无」），
   还带着已废弃的 `register_shifts`。重跑即可，每张约 ¥0.02、1 分钟：
   ```powershell
   python -m wxautochat profile --chat "<群名>" --who self --effort high
   ```
2. **没有专属卡片、正在借用默认人格的会话**：想要贴切的语气就单独跑一次 P2。
   默认人格是"通用日常口语"，对亲密关系或特定圈子会偏平。
3. **`<数据根>` 的父项目 `.gitignore` 有一处未提交改动**（补上漏掉的 `/work/`，
   那是 488 MB 解密明文，一条 `git add -A` 就会提交进仓库）。是否提交由你决定。
4. 可选增强（都没做）：
   - 语音转写（语音占比较高的会话，画像与回复质量会明显提升）
   - 多会话并发（目前同一时刻只能跑一个实例，共用游标与单实例锁）
   - 群聊里对"图片/表情"的进一步处理（现在私聊会回，群聊不 @ 就跳过）

---

## 6. 三条铁律（踩过才写下来的）

1. **`Stop-Process` 是异步的**：终止后必须确认进程列表为空、再删 `work/_lock.json`，
   否则两个进程同写游标会崩。
2. **发送期间不要抢微信输入框**：机器人会点击输入框并粘贴，会覆盖你未发出的草稿。
3. **不要用 PowerShell 的 `-replace` + `Set-Content` 改这些源码**（PS 5.1 会按 ANSI 读、
   以 UTF-8 写，中文变乱码且带 BOM）。用编辑器或 `edit`/`write` 通道。

其它已修但容易回归的点（详见 README §10）：
- 会话名 OCR 要取「最靠上」的一行（取最宽会把消息气泡当标题）；
- 卡片文件名会把空格归一化成下划线，查找必须两种写法都试；
- 回读校验用**子序列匹配**（用户手打消息穿插时，末尾对齐会误报未通过）；
- 前台丢失要**先重新激活一次**再判（直接中止会丢掉整轮剩余消息）；
- `local_type=244813135921` 是**引用**不是视频号；
- `STOP` 必须在主循环顶部生效，否则会为发不出去的消息持续付费。

---

## 7. 命令速查

| 命令 | 作用 |
|---|---|
| `probe` | 环境自检（数据目录 / 密钥 / 依赖 / 本机 wxid） |
| `api [--test]` | API 配置自检；`--test` 发一次最小真实请求 |
| `watch [--once]` | 只读轮询新消息 |
| `reset` | 清空游标（下次启动重建基线） |
| `profile --chat X --who self` | **P2**：生成人格卡片 |
| `reply --chat X` | **P3**：自动回复（`--no-live` 只出草稿） |
| `convo --list / --chat X [--write]` | 会话配置的查看与派生 |
| `nicks --chat X` | 群昵称对照表（@ 人必须用群昵称） |
| `analyze --chat X` | 导出聊天记录 + 零 token 统计报告 |

---

## 8. 隐私提醒

- `<数据根>` 下的 `profiles/ conversations/ work/ replies/ exports/ api.txt`
  以及父项目的 `out/all_keys.json`、`keys/` **全部是机密**（含聊天明文与数据库密钥）。
- P2/P3 会把原始聊天文本发给 DeepSeek；请求原文留档在 `profiles/_sent_payloads/`。
- 本仓库已 gitignore 全部数据目录；**提交前仍请跑一次 `git status` 核对**。
