# wxautochat · 微信拟人自动回复工具

从本机微信 4.x 的**加密数据库**里实时读取聊天记录，用大模型学出"你在这个会话里怎么说话"，
再以你的身份自动回复，并通过**数据库回读**确认消息真的发出去了。

```
微信 4.x 加密库
      │  P1 解密 + WAL 增量 + 文本规范化                零 token
      ▼
   消息流 ──轮次切分──规则预筛──LLM门控──生成──输出硬过滤──UI自动化发送──数据库回读
      │                                              │
      └── P2 风格画像（离线，一次性）→ 人格卡片         └── 每次约 ¥0.001
```

三个阶段互不耦合，可单独运行：**P1 只读库**、**P2 只读库+调 API**、**只有 P3 会操作界面**。

> 本 README 中的会话名、wxid、群昵称均为占位示例，真实数据不入库。

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 实测效果](#2-实测效果)
- [3. 快速开始](#3-快速开始)
- [4. 工作原理](#4-工作原理)
- [5. 配置体系](#5-配置体系)
- [6. P2：人格卡片](#6-p2人格卡片)
- [7. P3：自动回复](#7-p3自动回复)
- [8. 子命令参考](#8-子命令参考)
- [9. 目录结构](#9-目录结构)
- [10. 实测坑（改代码前必读）](#10-实测坑改代码前必读)
- [11. 已知限制](#11-已知限制)
- [12. 安全与隐私](#12-安全与隐私)
- [13. 运维手册](#13-运维手册)
- [14. 开发](#14-开发)

---

## 1. 它解决什么问题

微信 4.x 把聊天记录加密存在本地（SQLCipher 4 / WCDB），且**主界面没有无障碍树**
（Qt 客户端 UIA `Descendants = 1`，`uiautomation` / `pywinauto` 全部无效）。
所以"读"和"发"都必须自己从零实现：

| 阶段 | 难点 | 本项目的做法 |
|---|---|---|
| P1 读 | 库是加密的；同一会话的数据**分片**在 4 个库里 | 从进程内存取密钥 → 页级解密 → 维护可增量套用 WAL 的明文副本 → 按 (分片, 表) 维护游标 |
| P2 拟 | 风格是隐性的，人说不清自己怎么说话 | Python 出**事实**（零 token），LLM 出**解释**，每条结论强制标注依据并事后核对 |
| P3 发 | 没有控件树，只能靠截图 OCR 定位 | 严格锚点判据 + 每步前置校验 + **发送后数据库回读**作为唯一验收 |

**核心设计原则**：事实 / 解释 / 约束三层分离 —— 统计给事实，模型给解释，代码给约束。
模型说"我平时爱发链接"也不会真去发链接，因为约束层封死了。

---

## 2. 实测效果

**一段 19 分钟的真实运行**（私聊，配置见 §5）：

| 指标 | 值 |
|---|---|
| 成轮 / 已发送 | 18 / 16 |
| 回读校验通过 | **16** |
| 回读未通过 / 发送失败 / 未捕获异常 | **0 / 0 / 0** |
| 门控调用 | ¥0.0006 每次 |
| 生成调用 | ¥0.0011 ~ 0.0030 每次（缓存命中 2432 token 时更便宜） |
| 人格卡片（P2） | ¥0.0196 ~ 0.0530 每张 |

**延迟构成**（以 3 条连发为例）：

```
最后一条消息到达
  +0~2.0s    轮询发现（间隔 2 秒）
  +2~8.0s    等静默窗口（对方继续发则并入同一轮）
  +8~9.0s    LLM 门控
  +9~11.0s   生成 3 条
  +11~32.5s  发送 3 条（每条约 3.5 秒 OCR+粘贴+回车，条间随机 3~8 秒）
  +33s       回读校验
```

想更快：调小 `send_gap`、`max_burst`、`quiet_window`。

---

## 3. 快速开始

### 3.1 环境要求

- Windows 10/11，微信 **4.0.3.x / 4.1.0.x**（4.1.13+ 已移除内存明文密钥，取不到）
- Python ≥ 3.10（实测 3.14）
- 依赖：`pycryptodome`、`zstandard`、`openai`
- OCR 用系统自带 `Windows.Media.Ocr`（`zh-Hans-CN` 语言包），无第三方依赖
- 显示缩放 150% 亦可（代码两侧都声明了 DPI 感知）

```powershell
python -m pip install pycryptodome zstandard openai
```

### 3.2 取密钥（一次性）

密钥由上游 `wxarchive keys` 从微信进程内存读出，缓存到 `out\all_keys.json`：

```
x'<64hex enc_key><32hex salt>'
```

- **32 字节 enc_key 直接作为 AES-256 key，不做 KDF**（早期误用 KDF 导致上万次参数失败）
- 密钥按库的 salt 匹配；salt 变了说明库被重建
- 密钥只对 4.0.x / 4.1.0.x 有效

### 3.3 配置 API（单文件）

```powershell
# <项目根>\api.txt   或   <项目根>\wxautochat\api.txt，就近优先
notepad .\api.example.txt      # 照格式另存为 api.txt
```

```
api_key=sk-你的密钥
model=deepseek-flash
base_url=https://api.deepseek.com
```

容错：支持 BOM、`#`/`;` 注释、`key=value` 与 `key: value`、行尾注释；
**只粘贴一串 `sk-...` 也能识别**。`api.txt` 已 gitignore。
`api.example.txt` 是模板，**绝不要往里填真密钥**（代码会检测并告警）。

### 3.4 四步跑起来

```powershell
cd <wxautochat 的父目录>          # 必须在这层：python -m wxautochat 要求能看到 wxautochat 包

# ① 环境自检（免费）：数据目录 / 密钥 / 库匹配 / 本机 wxid / 截图 / OCR
python -m wxautochat probe
python -m wxautochat api --test   # 发一次最小真实请求验证连通性

# ② 出人格卡片（P2，¥0.02~0.05/张，约 1 分钟）
python -m wxautochat profile --chat "某个会话" --who self --effort high `
  --note "对方是……（背景、雷区、关系，作为权威输入）"

# ③ 写会话配置（节奏/条数/丢弃策略）
python -m wxautochat convo --chat "某个会话" --write
notepad .\conversations\某个会话.json

# ④ 开跑
python -m wxautochat reply --chat "某个会话" --once          # 只出一次草稿，不发
python -m wxautochat reply --chat "某个会话"                  # 按配置真发
python -m wxautochat reply --chat "某个会话" --verbose        # 带 API 调用细节
```

**运行期间微信必须停在目标会话**：发送前会 OCR 核验会话名，不符则只落草稿不发送。

**对任何人开箱即用**：仓库自带 `defaults/`（默认人格卡 + 默认会话策略），
所以任意会话都能直接启动，**不必先跑 P2 生成卡片**：

```powershell
python -m wxautochat reply --chat "任意联系人" --no-live --once   # 先看草稿再说
python -m wxautochat reply --chat "任意联系人"                    # 直接跑（默认 live=true）
```

### 3.5 在别人的环境里运行（可移植性）

**目录约定**：数据根 = **仓库目录的父目录**（`config.ROOT = dirname(PKG_DIR)`）。所以标准用法是：

```
D:\somewhere\                 ← 数据根：api.txt / work/ / profiles/ / conversations/ 都在这
└── wxautochat\               ← 本仓库（clone 到这里）
```

```powershell
git clone https://github.com/<用户名>/wxautochat.git D:\somewhere\wxautochat
cd D:\somewhere
python -m pip install -r wxautochat\requirements.txt
python -m wxautochat probe            # 首次会全量解密，约 4 秒 / 592 MB 分片
```

**三条硬前置（缺一不可）**

1. **Windows + 微信 4.0.x / 4.1.0.x 且处于登录态**（4.1.13+ 已移除内存明文密钥，取不到）
2. **密钥缓存 `out\all_keys.json`** —— 本工具**只消费不生产**，需上游 `wxarchive keys`
   在同一台机器上跑一次生成（从微信进程内存读密钥）。格式（值是机密，勿外传）：

   ```json
   {
     "message\\message_0.db": { "enc_key": "<64 位十六进制>", "salt": "<32 位十六进制>" },
     "contact\\contact.db":   { "enc_key": "...", "salt": "..." }
   }
   ```

   条目名是**相对微信数据目录**的路径；数据目录可用 `--db-dir` /
   `config.local.json:db_dir` / `$env:WXARCHIVE_DB_DIR` 指定（不指定则自动探测）。
   工具放在任何目录都指向同一台机器的同一份微信数据 —— 这是设计行为，不是硬编码。
3. **DeepSeek API key** 写进 `api.txt`（见 §3.3）

**不要用非 editable 的 `pip install .`**：那会把包装进 site-packages，`PKG_DIR` 随之改变，
运行时数据会被写进解释器目录。需要全局命令时用 `pip install -e .`
（`PKG_DIR` 仍指向仓库，`ROOT` 仍是其父目录）。

**首次运行**：`work/ profiles/ conversations/ replies/ exports/` 在需要时自动创建；
**仓库目录本身不会被写入数据**（数据都在父目录），因此仓库天然干净。

---

## 4. 工作原理

### 4.1 数据层：怎么读到加密库

| 环节 | 做法 |
|---|---|
| 页布局 | page1 = `[0:16]` 明文 salt｜`[16:4016]` 密文｜`[4016:4032]` IV｜`[4032:4096]` HMAC tag；pageN = `[0:4016]` 密文 + 同一 IV/tag |
| 分片 | 同一张 `Msg_<md5(会话名)>` 表在 `message_0..3.db` 里都存在，按时间分段，**必须合并+排序+去重** |
| 游标键 | `"<分片>/<表名>"`（只用表名会跨分片串台，实测一次喷出 62,836 条历史） |
| 增量来源 | 只盯 live 目录的 `*.db-wal`；微信检查点后会把已解密副本的 WAL 清零，**本地副本的 WAL 没有信息** |
| 一致性读 | 先 `stat` 定长、再单次 `read`，拿到"某一时刻的完整前缀"，WAL 帧校验保证自洽 |
| 套用增量 | 按 `pgno` 回填解密页，无需重解整库 |
| salt 变化 | 全量重建（旧帧 `pgno` 语义已失效） |
| 游标 | `local_id`（INTEGER 主键，单调），**每轮无条件落盘** |
| 发送者链 | `real_sender_id` → `Name2Id.rowid` → `user_name` → `contact` 显示名 |
| 群昵称 | `contact.db.chat_room.ext_buffer`（protobuf，自带最小解析器），@ 人必须用群昵称 |
| 性能 | 单页解密 ~384 MB/s（AES-NI），592 MB 分片全量重建 ~1.5 秒，空闲轮询 ~0.05 秒 |

**文本规范化**（`watcher.normalize_text`，轮询与整库读取**共用同一实现**）：

1. 剥掉群聊消息内嵌的发送者前缀 `wxid_xxx: 正文`；
2. XML 类消息（分享/文件/小程序）取 `<title>`，而不是丢掉整段；
3. `local_type=244813135921` 是**引用**（`<appmsg><type>57</type><refermsg>`），
   渲染成 `[引用] 他打的字 ｜引自某人「被引用的原句」`；引用里再套 XML 时再取一层 `<title>`。

### 4.2 P2：确定性统计 + LLM 语义画像

```
第 1 层（Python，确定性，零 token）
  stats.py      句长/标点/语气词/缓冲语/网络语/连发节奏/活跃时段/类型分布/回复延迟
  sampling.py   按「时间 × 回复长度」分层的配对样本（固定种子，可复现）
第 2 层（LLM，语义解释）
  输入 = 事实 JSON + 用户给定事实 + 24 组「对方说→我回」配对 + 5 段连续对话窗口
  输出 = personality / expression_habits / emotional_tone / topics /
         emotional_value / taboos / reply_style_rules / few_shot_reasons /
         unsupported_inferences / confidence
```

**防编造**：提示词要求每条结论标注 `(依据: …)`，事后 `verify()` 把依据拆成信号
（数字 + 词组）回查「事实 + 样本」，命中率 ≥0.5 记「有据」，只提统计项名记「弱依据」，
对不上记「未命中」；模型自认的推测单列 `unsupported_inferences`。
**核对是复核辅助，不是拦截器**（早期按字面比对误报 34/36，改为信号打分后降到 1 条）。

### 4.3 P3：一次回复的完整时序

| 步 | 做什么 | 成本 |
|---|---|---|
| 1 | 轮询增量（默认 2 秒），合并分片、去重、规范化文本、落盘游标 | 0 |
| 2 | 只保留本会话那张表的消息 | 0 |
| 3 | 喂进轮次聚合器：对方消息进待回队列；本人**文本**消息清空队列 | 0 |
| 4 | 成轮判定：静默满 `quiet_window`，或本轮已满 `max_wait` | 0 |
| 5 | 读最近 `context` 条，并定位"待回那一轮"在序列中的起点（生成时标 ★） | 0 |
| 6 | **规则预筛**：系统/撤回/群发/纯链接跳过；群聊里"分享类、非文本且未 @ 我"跳过 | 0 |
| 7 | **@ 强制通道**：被 @ 直接回复，跳过第 8 步 | 0 |
| 8 | **LLM 门控**：群聊用"宁可不回也别插话"；私聊用"一对一默认该回" | ¥0.0006 |
| 9 | **组装请求**：SYSTEM（铁律+画像+硬约束+群成员表+用户给定事实）+ USER（对话+★+few-shot+禁重复清单） | 0 |
| 10 | **生成**（1~`max_burst` 条）；与最近说过的话重复则重试，最多 2 次 | ¥0.001~0.003 |
| 11 | **代码级输出过滤**：去占位符/URL/编号引号、截断条数、校验 @ 名字 | 0 |
| 12 | **发送**（每条重复）：重定位窗口 → DPI → PrintWindow 截图 → OCR → 推锚点 → 激活+前台检查 → 核会话名 → 清残留 → 点输入框 → 粘贴 → 回车 | 0 |
| 13 | **回读校验**：直接读库，按序子序列匹配确认入库 | 0 |
| 14 | 落盘 `drafts.jsonl` / `sent.jsonl` | 0 |

**为什么对话单位是"轮"而不是"条"**：人是一串一串说话的。按条回会显得机械又吵。
按轮回才能做到**多对一**（对方连发 5 条 → 一次回应）与**一对多**（对方 1 条 → 你连发 2~3 条）。

**为什么正确性只靠数据库回读**：OCR 会读错、窗口会移动、焦点会丢。
截图只用于**定位**，不用于判定；唯一验收标准是"库里有没有这条文本"。

### 4.4 三层防线

1. **发送前**：会话名 OCR 核验（不符则重拍 2 次再判）+ 前台窗口检查（不在前台先重新激活一次）
2. **发送时**：锚点判据严格（发送按钮必须「右面板 + 底部 18% + 短文本」），
   **定位不到输入框就中止，绝不猜坐标**；会话名不符、前台不是微信都不发送
3. **发送后**：数据库回读；另有 `STOP` 文件急停与单实例锁

一句话：**宁可漏发，绝不发错会话。**

---

## 5. 配置体系

**优先级：命令行显式参数 > 会话配置文件 > 内置默认。**
（`main()` 记录本轮出现过哪些 `--flag`，配置文件只填没被显式给出的项。）

| 层 | 文件 | 管什么 |
|---|---|---|
| 全局密钥 | `api.txt` | api_key / model / base_url |
| 全局行为 | `wxautochat\behavior.py` + `behavior.local.json` | 手写硬约束：内容要求、情绪价值、输出形式、正确性、反重复、跳过规则 |
| 全局路径 | `config.local.json` | 数据目录、work 目录、密钥缓存、已解密目录 |
| **按会话** | `conversations\<会话>.json` | 卡片、是否真发、校验档位、节奏、连发上限与随机间隔、**哪些本人消息算"已回答"**、该会话的用户给定事实 |

### 5.1 会话配置（`conversations\<会话>.json`）

```json
{
  "chat": "某个会话",
  "aliases": ["wxid_xxxxxxxx"],
  "card": "某个会话_self",
  "live": true,
  "verify": "send",
  "interval": 2.0,
  "quiet_window": 8.0,
  "max_wait": 60.0,
  "context": 24,
  "history": 80,
  "fewshot": 4,
  "no_repeat": 12,
  "max_burst": 3,
  "send_gap": [3.0, 8.0],
  "temperature": 0.9,
  "think": false,
  "max_tokens": 512,
  "discard_pending_on": ["文本"],
  "notes": ["对方是……（权威输入，直接注入每次回复的 SYSTEM）"],
  "comment": "自由备注，代码不读"
}
```

| 字段 | 说明 |
|---|---|
| `card` | 人格卡片名或路径；不填则按会话名自动挑 |
| `live` | true 等效于加了 `--live` |
| `verify` | `full`（粘贴后+回车前都校验，最慢）/ `send`（只核会话名，默认）/ `none` |
| `interval` | 数据库轮询间隔秒 |
| `quiet_window` | 静默窗口秒：对方停下这么久即成一"轮" |
| `max_wait` | 从本轮第一条算起最长等多久就强制成轮 |
| `max_burst` | 本轮最多连发几条（默认取硬约束的 5） |
| `send_gap` | 连发间隔：单个数字，或 `[最小, 最大]` **每条随机取**（越随机越像人） |
| `discard_pending_on` | **本人发哪些类型算"我已经回过了"**，默认 `["文本"]`。可写 `["文本","引用"]`，或 `["*"]` 表示任何发言都丢弃 |
| `notes` | 该会话的用户给定事实，直接注入 SYSTEM，**不必重建卡片** |

> `discard_pending_on` 的取舍：默认"只有本人发的纯文本才算已回答"，因为实测出现过
> 用户手发一条分享/引用就把对方一条正经问题整条丢掉的 case。代价是**你用引用回他时，
> 机器人可能再回一遍**；不想重复就加 `"引用"`。

### 5.2 按会话的行为覆盖（`behavior` 段）

**为什么要这一层**：同一个人在不同会话里的分寸完全不同 —— 跟一个人是互骂互损，
跟另一个人要软要哄。全局硬约束里那条「说话要软」在互骂的会话里是**反作用**。
把它写死成全局规则，就只能靠改代码解决；分层后改一个 json 即可。

```json
{
  "chat": "某个会话",
  "behavior": {
    "emotion": ["……整体替换掉全局的「说话要软」……"],
    "content_extra": ["……在全局内容要求之后追加……"],
    "extra_facts": ["对方是能一起互骂的朋友；脏话 = 亲近，不是冒犯"]
  }
}
```

**语义**（`behavior.rules()` 三层合并，后者覆盖前者）：

| 写法 | 含义 |
|---|---|
| `"emotion": [...]` | **整体替换**该段 |
| `"emotion_extra": [...]` | **追加**到该段之后（不必抄一遍全局） |
| `"extra_facts": [...]` | 永远**追加**（全局 → behavior.local.json → 会话） |
| `"max_burst"` / `"frequency_penalty"` / `"presence_penalty"` / `"max_age_minutes"` | 覆盖数值 |

可覆盖的段：`content` / `emotion` / `private` / `correctness` / `output_constraints` /
`anti_repeat` / `skip_conditions` / `extra_facts`。

> **未列出的段沿用全局默认**，所以只写要改的那几段即可，安全底线（禁 URL、
> 禁占位符、不编造、不做承诺）不会被误伤 —— 这一点有回归用例守着。

**实例（互骂互损的会话）**：只写 `emotion`（替换）+ `content_extra` + `private_extra`
+ `extra_facts`，把「说话要软」换成「被骂必须骂回去 / 不适用软化」，
同时在 `content_extra` 里要求「接梗与反击、禁止答非所问、短狠带语气」。
自检 18/18 通过，其中一条专门断言**全局的软化要求已从提示词里消失**、
而**输出形式与正确性两段一条不少**。

### 5.4 默认配置与默认人格（开箱即用）

仓库自带三份默认文件，**任何会话都能直接启动**：

| 文件 | 作用 |
|---|---|
| `defaults/_default.json` | 默认会话策略：`live=true`、静默 8 秒、最长等 60 秒、连发上限 3、间隔随机 3~8 秒、`discard_pending_on ["文本"]` |
| `defaults/_default_self.json` | 私聊默认人格卡（人工编写，数值来自本机已生成卡片的汇总统计） |
| `defaults/_default_group_self.json` | 群聊默认人格卡（群里更短更碎，且要按群昵称 @ 人） |

**查找顺序**（前两级是"你的"，第三级是"出厂的"）：

```
卡片： profiles/<会话>_self.json  →  profiles/_default_self.json  →  <包>/defaults/_default_self.json
配置： conversations/<会话>.json  →  conversations/_default.json   →  <包>/defaults/_default.json
```

- 私聊/群聊各有一份默认卡片：两者语域差别是实测出来的，用一张卡必然有一边不像。
- 想覆盖默认：在 `<数据根>\profiles\_default_self.json` 或 `<数据根>\conversations\_default.json`
  写一份同名文件即可（项目根优先于包内）。
- 想只改一个会话：`convo --chat "会话名" --write` **从默认派生**一份，只改要改的几项。
- 想让某会话**不被发送**（只出草稿）：加 `--no-live`，它会压过默认配置里的 `live=true`。
- `pick_card` 有一层兜底：连默认卡都没有时才会借用别的会话的卡片，**并打警告**
  —— 早期版本把兜底当常规路径，会静默借用语气完全不符的卡片。

### 5.5 三层配置示例

```powershell
python -m wxautochat convo --list                    # 列出全部会话配置
python -m wxautochat convo --chat "某个会话"           # 看该会话当前生效内容
python -m wxautochat convo --chat "新会话" --write     # 生成模板
python -m wxautochat convo --chat "某会话" --write --force   # 覆盖重写
```

### 5.3 路径解析（无硬编码）

| 项 | 优先级 |
|---|---|
| 数据目录 | `--db-dir` > `config.local.json:db_dir` > `$env:WXARCHIVE_DB_DIR` > 自动探测微信自身配置 |
| 工作目录 | `--work` > `config.local.json:work` > `$env:WXAUTOCHAT_WORK` > `<项目根>\work` |
| 密钥缓存 | `--keys-file` > `config.local.json` > `$env:WXAUTOCHAT_KEYS` > 就近搜索 `out\all_keys.json` |
| 已解密目录 | `--decrypted` > `config.local.json` > `$env:WXAUTOCHAT_DECRYPTED` > 就近搜索 `decrypted\` |
| API key | `$env:DEEPSEEK_API_KEY` > `api.txt` > `config.local.json` |

自动探测源是微信自身配置 `%APPDATA%\Tencent\xwechat\config\*.ini`，
所以工具放在任何目录都指向同一台机器的同一份数据 —— **这是设计行为**。

---

## 6. P2：人格卡片

```powershell
python -m wxautochat profile --chat "某个会话" --who self --effort high
python -m wxautochat profile --chat "某个会话" --dry-run     # 不调 API，只看统计与提示词、先估成本
python -m wxautochat nicks   --chat "某个群"                 # 看群昵称对照表（@ 人必须用群昵称）
```

| 参数 | 说明 |
|---|---|
| `--chat` | 必填。显示名 / 备注 / 昵称 / 别名 / username / `Msg_` 表名 |
| `--who` | `self`（本人，P3 用这个）/ `other`（对方）/ `both` |
| `--effort` | 思考强度 `low` / `high`（默认）/ `max` |
| `--no-think` | 关思考（更快更省，画像深度下降） |
| `--limit N` | 只分析最近 N 条（0=全部） |
| `--note` | **用户给定事实**（可重复），权威输入，会写进 `python_facts` 并参与依据核对 |
| `--dry-run` | 只生成统计与提示词，不调 API |
| `--out` | 卡片输出目录（默认 `<项目根>\profiles`） |

产物：`profiles\<会话>_self.json`（结构化）+ `.md`（可读）。
请求原文留档在 `profiles\_sent_payloads\`（含原始私聊内容，注意隐私）。

**成本控制**：常量 SYSTEM 提示词放在 messages 最前且逐字节稳定 → 命中 DeepSeek 上下文缓存
（缓存命中 ¥0.02/M vs 未命中 ¥1/M，差 50 倍，实测命中率可达 96%）。

---

## 7. P3：自动回复

```powershell
python -m wxautochat reply --chat "某个会话"                 # 按会话配置真发
python -m wxautochat reply --chat "某个会话" --once          # 只跑一次
python -m wxautochat reply --chat "某个会话" --dry-send      # 走完发送流程但不按回车
python -m wxautochat reply --chat "某个会话" --verify full   # 最严校验（慢）
```

| 参数 | 说明 |
|---|---|
| `--config` | 显式指定会话配置（默认按会话名自动找） |
| `--card` | 显式指定人格卡片（跨会话借用时用） |
| `--live` | 真发送；不加则只出草稿 |
| `--no-live` | 只出草稿，**用来压过会话配置里的 `live=true`** |
| `--interval` / `--quiet-window` / `--max-wait` | 节奏（见 §5.1） |
| `--max-burst` / `--send-gap` | 连发条数与间隔；`--send-gap 3,8` 表示每条随机 |
| `--verify` | `full` / `send`（默认）/ `none` |
| `--fast` | 回车前不再抓图核会话名（省 ~1.8 秒/条，仅保留前台检查） |
| `--no-click` | 禁用"点击输入框"兜底 |
| `--no-repeat` / `--fewshot` | 反重复禁用条数 / 喂入同话题真实配对数 |
| `--context` / `--history` | 喂给模型的对话条数 / 启动时读入的历史条数 |
| `--think` / `--temperature` / `--max-tokens` | 生成参数（思考模式下 temperature 失效） |
| `--once` / `--show-raw` / `--verbose` / `--quiet` | 运行控制 |

### 7.1 群聊 vs 私聊（自动区分）

| 维度 | 群聊 | 私聊 |
|---|---|---|
| @ 逻辑 | 被 @ 强制回复；输出必须用**群昵称**；@ 名字会被校验 | **完全忽略 @**，SYSTEM 里所有 @ 约束整段撤掉 |
| 规则预筛 | "分享类/非文本且未 @ 我"→跳过 | **不做这条**（私聊没有 @，照搬会屏蔽掉 18% 的表情/图片/分享） |
| 门控标准 | 宁可不回也别插话 | 一对一默认该回，表情/图片/引用也算互动；只有系统消息或超 10 分钟才不回 |
| SYSTEM 附加 | 群成员表（群昵称对照） | 私聊专用块（更软、更近、不让话悬着、字数不必拘谨） |

### 7.2 输出硬约束（`behavior.py`，手写层，优先级高于卡片）

- **每条回复都必须提供情绪价值**（先接住情绪再回应事情，禁敷衍式单条"嗯/哦/好"）
- 说话要软：带语气词、句末不加句号、不写书面语/客服腔
- 不编造：不确定的不编数字/版本/人名；**语音/图片/视频看不到内容，禁止声称"听了/看了"**
- 不做承诺、不替他人表态、涉及金钱账号一律不回、不主动开启新话题
- 输出只能是纯文本，禁止占位符与 URL，条数封顶
- 反重复：提示词里摊开"你最近说过的话"+ `frequency_penalty`/`presence_penalty` + 生成后机械查重重试

---

## 8. 子命令参考

| 命令 | 作用 |
|---|---|
| `probe` | 环境自检：数据目录及其来源、密钥匹配、依赖、本机 wxid |
| `watch` | 只读轮询新消息。`--once` 同步一次即退出；`--show-mine` 也显示自己发的 |
| `reset` | 清空游标（下次启动重新建基线） |
| `api` | API 配置自检；`--test` 发一次最小真实请求 |
| `profile` | **P2**：生成人格卡片 |
| `reply` | **P3**：自动回复（默认只出草稿，`--live` 才发送） |
| `convo` | 查看/生成「按会话的配置」 |
| `nicks` | 查看群昵称映射（@ 人必须用群昵称） |
| `analyze` | 导出聊天记录 + 纯 Python 统计报告（零 token） |

通用参数：`--db-dir`、`--work`、`--decrypted`、`--keys-file`。

---

## 9. 目录结构

### 9.1 代码（23 个模块，约 5,800 行）

```
wxautochat/
├── __init__.py      包说明
├── __main__.py      python -m wxautochat 入口
├── cli.py           命令行入口：全部子命令、reply 主循环、回读校验
├── convo.py         按会话的配置：查找 / 优先级合并 / 模板
├── config.py        路径与配置解析（无硬编码，就近自动发现）
├── crypto.py        SQLCipher 4 页解密 + WAL 一致性读
├── keys.py          密钥缓存加载 + live 库索引 + 数据目录探测
├── sessions.py      会话/联系人/发送者映射 + 本机 wxid + 别名
├── snapshot.py      增量快照：可增量套用 WAL 的解密副本
├── watcher.py       增量轮询 + 消息类型映射 + **文本渲染（唯一实现）**
├── history.py       从快照读会话历史 + 会话定位（渲染转交 watcher）
├── burst.py         轮次聚合 + 丢弃策略
├── behavior.py      手写行为约束层（内容/情绪价值/私聊群聊分派/正确性/反重复）
├── sampling.py      分层采样 + few-shot 检索 + 采样池统计
├── stats.py         确定性统计（P2 第一层，零 token）
├── profile.py       P2 主流程 + 依据核对 + 卡片落盘
├── reply.py         P3：SYSTEM 拼装 / 门控 / 生成 / 输出过滤 / @ 校验
├── api.py           api.txt 解析 + DeepSeek 调用 + 费用估算
├── sender.py        UI 自动化发送：锚点 / 输入框 / 粘贴 / 回车 / 连发 / 核验重试
├── ui.py            Win32 层：DPI / 窗口 / PrintWindow / 剪贴板 / SendInput / 系统 OCR
├── lock.py          单实例锁（心跳，防并发写游标）
├── analyze.py       记录导出 + 零 token 统计报告
├── grouproster.py   群昵称 protobuf 解析 + @ 合法性校验
├── defaults/        **随仓库分发的出厂默认**（开箱即用的关键）
│   ├── _default.json               默认会话策略
│   ├── _default_self.json          私聊默认人格卡
│   └── _default_group_self.json    群聊默认人格卡
├── PROBE.md         可行性探测结论（为什么不能用 UIA 等）
├── api.example.txt  API 配置模板（保持为空）
├── config.local.example.json
└── .gitignore
```

### 9.2 运行时数据（全部 gitignore）

```
<项目根>/
├── api.txt                    密钥（单文件配置）
├── STOP                       新建此空文件即急停发送
├── conversations/<会话>.json  按会话的策略
├── profiles/<会话>_self.json  人格卡片（+ .md 可读版 + _sent_payloads/ 请求原文）
├── work/
│   ├── message_0..3.db        加密库的明文解密副本（实测约 730 MB）
│   ├── _cursors.json          每 (分片, 表) 的 local_id 游标
│   ├── _state.json            每库 WAL 消费位置与 salt
│   ├── _lock.json             单实例锁心跳
│   ├── _ocr_helper.ps1        调系统 OCR 的 PowerShell 脚本（首次运行写出）
│   └── shots/                 每次发送留档的截图与 OCR 结果
├── replies/drafts.jsonl       每轮草稿
├── replies/sent.jsonl         每次实际发送（含 complete/failed/费用）
└── exports/                   analyze 的导出与报告
```

`work/` 可随时删除，下次运行自动全量重建（约 4 秒）。
`profiles/` 删掉只是要重新花钱生成。

---

## 10. 实测坑（改代码前必读）

**数据层**

1. **游标键必须含分片**。只用表名会让先扫的分片把游标 prime 到自己的最大值，
   后扫的分片一看游标远大于自己范围，就把整片历史当新消息喷出来（实测 62,836 条）。
2. **基线必须无条件落盘**。游标是在扫描时 prime 的，若只在"有产出"时保存，
   每次启动都会重新 prime。
3. **salt 变化必须全量重建**。WAL 重建后旧帧 `pgno` 语义已变。
4. **状态文件用 `utf-8-sig` 读**。PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM，
   用 `utf-8` 读会抛异常并被静默吞掉 —— 表现为游标全丢、重新建基线。
5. **身份判定必须用 `is_mine`，不能比名字**。`Message.sender` 是联系人显示名，
   本机账号解析出来是**自己的昵称**，不是字符串 `我`。早期按名字比对，把分析对象搞成了本人。
6. **`local_type` 把子类型编码在高位**。`type_name()` 需要 `lt & 0xFF` 回退；
   且 **`244813135921` 是「引用」不是「视频号」**（`<appmsg><type>57</type><refermsg>`）。
   错标的后果：群聊里"分享类不 @ 我即跳过"会把**所有引用消息整条无视**。
7. **渲染口径必须唯一**。轮询与整库读取若一个渲染、一个不渲染，成轮日志、few-shot 关键词、
   `drafts.jsonl` 会被原始 XML 污染，而模型看到的却是渲染后的文本。

**发送层**

8. **不要用 `CopyFromScreen` 截图**：窗口被遮挡时抓到的是别的窗口（实测单色 RGB(40,50,40)）。
   必须 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)`。
9. **微信主界面没有无障碍树**：Qt 客户端 UIA `Descendants = 1`，`uiautomation`/`pywinauto` 全无效。
10. **会话标题要取"最靠上"的一行，不是"最宽"的**。消息区第一条气泡会落在标题高度带内，
    一旦它比标题宽，"取最宽"就会把**一条消息**当标题 → 会话名核验失败 → 整轮中止。
    实测 369 张真实截图：旧规则误取 7 张，新规则 1 张。
11. **发送按钮判据必须严格**：早期只要"含『发送』且在下半屏"，结果消息里出现"发送"二字时
    锚点跳到消息区，推出的输入框坐标全错（表现为"点了输入框却粘贴为空"）。
12. **找不到输入框时返回 None 并中止，不要用比例兜底**。瞎点会落进消息区。
13. **每次粘贴前都要点击输入框确保焦点**（实测粘贴失败常因焦点不在输入框）。
14. **输入框顶部要避开工具栏图标行**：OCR 会把那排图标读成 `O`，被误判成"残留文本"。
15. **前台窗口检查是零成本的，必须做**：不在前台时按回车会打到别的程序上。
    但**先重新激活一次再判**，否则用户切窗口就会丢掉整轮剩余消息。
16. **Python 与 PowerShell 两侧都要声明 DPI 感知**（缩放 150% 时坐标差 1.5 倍）。
17. **回读校验用子序列匹配**：用户在同一个窗口手打消息（`is_mine` 同样为真）会让
    "取末尾 N 条逐条比"全部错位 —— 明明已入库却报"未通过"。
18. **`job_kill` / `Stop-Process` 是异步的**：重启前必须确认进程列表已空、再删 `work\_lock.json`，
    否则两个进程同时写游标会 `[WinError 32]` 崩掉。

**模型与提示词**

19. **`max_tokens` 含思考 token**：开思考且 `effort=high` 时 8000 会被思维链吃掉大半，
    导致 JSON 中途截断（`finish_reason=length`）。默认给 32000，且显式报错。
20. **思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` 全部无效**。
21. **模型会把提示词里的示例词句原样照抄**（实测某句被连发 4 轮）。必须显式列出
    "你最近说过的话"并禁止重复，再加生成后机械查重。
22. **模型会退化成只输出 `@某某`**，没有任何正文。SYSTEM 必须显式禁止"纯 @ 不构成回复"。
23. **群里 @ 人必须用群昵称**，用全局备注 @ 不到人；要显式喂入对照表，否则会输出错的 @。
24. **不要把规则只写在提示词里**。占位符、URL、条数这类硬边界必须**代码再兜一层**。
25. **`compact_facts` 的键在 `对象事实` 之下**，不是顶层；按顶层取会让压缩整段失效。
26. **类名/属性类错误（`NameError`/`AttributeError`）只在跑到那行才炸**。
    本项目已实测两例（未导入的 `sampling`、不存在的 `sampling.sample_stats`），
    建议改动后做一次"模块.属性"静态核查。

**其它**

27. **不要用 PowerShell 的 `-replace` + `Set-Content` 改源码**：PS 5.1 会按 ANSI 读、以 UTF-8 写，
    中文字符串变乱码且带 BOM。
28. **不要把密钥填进 `api.example.txt`**：那是要提交到 git 的模板（代码会检测并告警）。

---

## 11. 已知限制

- **语音消息无文本**：`local_type=34` 只渲染成 `[语音]`。语音占比高的会话（实测 25%）
  画像与回复质量都会下降。要解决需接入语音转写。
- **图片/视频/文件内容不可见**：只能拿到标题，无法理解内容。硬约束禁止模型假装看过。
- **启动瞬间可能跳过极少消息**：基线取 `max(local_id)`，建基线那一刹那到达的消息会被视为已读。
- **微信必须处于登录态**：live 库需持续写入，否则没有 WAL 增量。
- **发送期间不要抢输入框**：机器人会点击输入框并粘贴，会覆盖你未发出的草稿。
- **同一时刻只能跑一个实例**（共用游标与单实例锁）。多会话并发尚未实现。
- **风格卡片是快照**：语体会漂移，建议定期重跑 P2。
- **画像质量取决于样本量**：卡片里的 `sampling.pool.可用配对` 低于 50 组时置信度低。
- **UI 自动化天然脆弱**：微信大版本更新可能改布局；锚点判据需随之调整。

---

## 12. 安全与隐私

- **P2/P3 会把原始聊天文本发到 DeepSeek**。上下文缓存可能保留一段时间。
  请求原文留档在 `profiles\_sent_payloads\`，可据此审计，**该目录含隐私，勿外传**。
- `api.txt`、`profiles/`、`conversations/`、`work/`、`replies/`、`exports/`、
  `out/all_keys.json`、`keys/` **全部已 gitignore**。提交前请再核对一次 `git status`。
- **密钥泄露 = 聊天记录泄露**：`out/all_keys.json` 与 `api.txt` 按机密对待。
- 自动发送在真实人际关系中有风险。默认建议先 `--once` 或 `--dry-send` 观察语气，
  再决定是否 `--live`。**你要为机器人说出的话负责。**

---

## 13. 运维手册

```powershell
cd <项目根>

# 启动（前台，日志直接打在终端）
python -m wxautochat reply --chat "某个会话" --verbose

# 启动（后台 + 落日志，便于远程观察）
Start-Process -FilePath "python" `
  -ArgumentList @("-u","-m","wxautochat","reply","--chat","某个会话","--verbose") `
  -RedirectStandardOutput ".\work\reply.out.log" `
  -RedirectStandardError  ".\work\reply.err.log" -WindowStyle Hidden

# 观察
Get-Content .\work\reply.out.log -Tail 40 -Encoding UTF8

# 急停（两选一）
Set-Content .\STOP ""                                  # 温和：下一次发送前中止
Get-Process python | Stop-Process -Force               # 强制

# 停止后重启前：确认干净
Get-Process python -ErrorAction SilentlyContinue
Remove-Item .\work\_lock.json -ErrorAction SilentlyContinue
```

**日志速查**

| 日志片段 | 含义 |
|---|---|
| `[burst] +N 条对方消息 -> 一轮 …` | 收到对方消息，等待成轮 |
| `[burst] 本人发言(文本) -> 丢弃 N 条待回消息` | 你自己回了，待回队列被清空 |
| `[burst] 本会话新消息 N 条全部是本人发言（其中 M 条不算'已回答'…）` | 你发的是分享/引用，队列保留 |
| `[规则层：不回复] 原因` | 零 token 层挡掉了 |
| `[该不该回] reply=False …` | 门控判断不需要回 |
| `已发送 N 条` → `[回读校验] 通过` | 成功闭环 |
| `[发送失败] 会话不符，中止` | 你切走了会话（已重拍核验仍不符） |
| `[回读校验] 未通过` | **需要关注**：可能真没发出去 |

---

## 14. 开发

**两份副本必须同步**（这是部署约定，不是可选）：

| 副本 | 路径 | 用途 |
|---|---|---|
| 开发副本 | 本仓库内的 `wxautochat/` | 改代码、提交 |
| 部署副本 | `<数据根>\wxautochat\` | 实际运行，数据目录也在那一层 |

改完开发副本后同步：

```powershell
Copy-Item .\wxautochat\*.py <数据根>\wxautochat\ -Force
```

**改动后的自检清单**

1. `python -c "import ast; ast.parse(open(...).read())"` 逐文件过语法；
2. 跑一次"模块.属性"静态核查，抓 `NameError`/`AttributeError` 这类只在运行到才炸的错误；
3. 用真实数据写**针对性的回归用例**（本项目有现成素材：`work\shots\` 里 500+ 张截图、
   解密副本里 3 万+ 条消息），例如本次修标题锚点就是拿 369 张历史截图新旧规则对比得出
   "7 张误取 → 1 张"；
4. 先用 `--once`（只出草稿）看语气，再用 `--dry-send` 走完发送流程但不按回车，最后才 `--live`；
5. 每次上线后确认 `reply.err.log` 为空、`[回读校验]` 全通过。

**代码风格约定**

- 中文注释，注释要写清"**为什么这么做**"以及"**实测现象**"，而不是复述代码；
- 凡是踩过坑的地方，注释里标明现象与日期，避免后人凭直觉改回去；
- 阈值给可配置入口，但**安全底线写死在代码默认值**里（改配置也绕不过去）。
