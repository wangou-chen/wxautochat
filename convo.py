# -*- coding: utf-8 -*-
"""按会话的配置文件（`<项目根>/conversations/<任意名>.json`）。

为什么要有这一层：同一个人在不同会话里的回法应当不同 ——
跟翔铭要放慢打字速度、少连发；跟小狗媳妇可以快一些。
这些是**运维策略**，不该写死在命令行参数里，也不该混进风格卡片
（卡片是"从统计里学出来的语气"，这里是"怎么发、发几条、什么算已回答"）。

优先级：**命令行显式给出的参数 > 配置文件 > 内置默认**。
实现方式是在 `main()` 里记录本轮出现过哪些 `--flag`（`args._given`），
配置文件只填那些没被显式指定的项。

文件字段（全部可选）：
    chat / aliases   这个配置属于谁（会话显示名 / wxid / 备注名）。匹配用。
    card             人格卡片名（同 --card）
    live             是否真发送（true 时等效于加了 --live）
    verify           full / send / none
    interval         轮询间隔秒
    quiet_window     静默窗口秒（对方停下多久算一轮）
    max_wait         最长等待秒
    context          喂给模型的最近对话条数
    history          启动时读入的历史条数（0=全部）
    fewshot          相近话题配对条数
    no_repeat        反重复禁用条数
    max_burst        连发上限（覆盖硬约束里的 5）
    send_gap         连发间隔秒：单个数字，或 [最小, 最大] 随机区间
    temperature / think / max_tokens
    fast / no_click / dry_send / once
    discard_pending_on  本人发哪些类型算"我已经回过了"，从而丢弃待回队列。
                        默认 ["文本"]；可写 ["文本","引用"]，或 ["*"] 表示任何发言都丢弃。
    notes            用户给定事实（权威输入），会注入每次回复的 SYSTEM
    comment          自由备注，代码不读
"""
import glob
import json
import os
import re

from . import config

DIR = os.path.join(config.ROOT, "conversations")

# 配置键 -> 对应的命令行开关。命令行里显式出现过就**不**被配置覆盖。
VALUE_FLAGS = {
    "card": "--card", "history": "--history", "context": "--context",
    "temperature": "--temperature", "max_tokens": "--max-tokens",
    "interval": "--interval", "quiet_window": "--quiet-window",
    "max_wait": "--max-wait", "no_repeat": "--no-repeat", "fewshot": "--fewshot",
    "verify": "--verify", "send_gap": "--send-gap", "max_burst": "--max-burst",
}
BOOL_FLAGS = {
    "live": "--live", "dry_send": "--dry-send", "no_click": "--no-click",
    "fast": "--fast", "think": "--think", "once": "--once",
}
KNOWN = set(VALUE_FLAGS) | set(BOOL_FLAGS) | {
    "chat", "aliases", "discard_pending_on", "notes", "comment",
}


def parse_gap(v):
    """连发间隔：0.8 或 "3,8" / [3,8] -> float 或 (min,max) 元组。"""
    if isinstance(v, (list, tuple)):
        nums = [float(x) for x in v]
    elif isinstance(v, str) and ("," in v or "-" in v):
        nums = [float(x) for x in v.replace("-", ",").split(",") if x.strip()]
    else:
        return float(v)
    if not nums:
        return 0.8
    if len(nums) == 1:
        return nums[0]
    lo, hi = min(nums), max(nums)
    return (lo, hi)


def files():
    return sorted(glob.glob(os.path.join(DIR, "*.json")))


def path_for(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "example"))[:60]
    return os.path.join(DIR, "%s.json" % (safe or "example"))


def dump(cfg: dict, name: str = None, path: str = None) -> str:
    """写配置文件（UTF-8，无 BOM）。name 用于推导文件名。"""
    p = path or path_for(name or cfg.get("chat") or "example")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


# 新建配置时的字段骨架（值刻意给成"可用的起点"）
DEFAULTS = {
    "chat": "", "aliases": [], "card": "", "live": True, "verify": "send",
    "interval": 2.0, "quiet_window": 8.0, "max_wait": 60.0, "context": 24,
    "history": 80, "fewshot": 4, "no_repeat": 12, "max_burst": 3,
    "send_gap": [3.0, 8.0], "temperature": 0.9, "think": False,
    "max_tokens": 512, "discard_pending_on": ["文本"], "notes": [],
    "comment": "命令行显式给出的参数会覆盖本文件；本文件覆盖内置默认",
}


def write_template(chat: str, force: bool = False) -> str:
    """给某会话生成配置模板；已存在时默认不覆盖，只补齐缺的键。"""
    p = path_for(chat)
    cfg = load_file(p) if os.path.exists(p) else {}
    if cfg and not force:
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
    else:
        cfg = dict(DEFAULTS)
        cfg["chat"] = chat
    cfg["chat"] = cfg.get("chat") or chat
    return dump(cfg, path=p)


def load_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:                                              # noqa: BLE001
        return {}


def find(display: str = "", username: str = "", name: str = None) -> tuple:
    """定位该会话的配置文件。name 给出时按文件名/路径直接取。"""
    if name:
        cands = [name] if os.path.isabs(name) or os.sep in name else [
            os.path.join(DIR, name if name.endswith(".json") else name + ".json"),
            os.path.join(DIR, "%s.json" % name)]
        for c in cands:
            if os.path.exists(c):
                return c, load_file(c)
        return None, {}
    for p in files():
        cfg = load_file(p)
        who = [str(cfg.get("chat") or "")] + [str(a) for a in
                                              (cfg.get("aliases") or [])]
        base = os.path.basename(p)[:-5]
        if (display and display in who) or (username and username in who) \
                or (display and display == base):
            return p, cfg
    return None, {}


def apply(args, target: dict) -> dict:
    """把该会话的配置填进 args（只填命令行没显式给的项），返回配置字典。"""
    name = getattr(args, "config", None)
    disp = display_of(target)
    if not name and not disp:
        return {}
    path, cfg = find(disp, (target or {}).get("username", ""), name)
    if not cfg:
        if name:
            print("[警告] 未找到会话配置 %r，按命令行/默认参数运行" % name)
        return {}
    given = getattr(args, "_given", set())
    for k, flag in VALUE_FLAGS.items():
        if k in cfg and flag not in given:
            setattr(args, k, cfg[k])
    for k, flag in BOOL_FLAGS.items():
        if k in cfg and flag not in given:
            setattr(args, k, bool(cfg[k]))
    args.send_gap = parse_gap(args.send_gap)
    print("会话配置: %s" % path)
    return cfg


def display_of(target: dict) -> str:
    return (target or {}).get("display") or ""


def write_example(path: str = None) -> str:
    """写一份带注释的模板（供手工改）。"""
    path = path or os.path.join(DIR, "example.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sample = {
        "chat": "某个会话的显示名",
        "aliases": ["wxid_xxx", "备注名"],
        "card": "某会话_self",
        "live": True,
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
        "think": False,
        "max_tokens": 512,
        "discard_pending_on": ["文本"],
        "notes": ["对方是……（你自己补：背景、雷区、关系）"],
        "comment": "命令行显式给出的参数会覆盖本文件；本文件覆盖内置默认",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    return path
