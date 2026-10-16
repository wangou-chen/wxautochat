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
    behavior         按会话的行为覆盖（覆盖 behavior.py 的硬约束）。键名同 behavior.py
                     的规则段：content / emotion / private / correctness /
                     output_constraints / anti_repeat / skip_conditions / extra_facts /
                     max_burst / frequency_penalty / presence_penalty。
                     语义：`xxx` 整体替换该段；`xxx_extra` 追加到该段之后；
                     `extra_facts` 永远追加。**未列出的段沿用全局默认**，
                     所以只写要改的那几段即可，安全底线不会被误伤。
                     例：互骂互损的会话只要写 emotion_extra + content_extra 把
                     「说话要软」反过来，不必重写全部约束。
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
    "no_live": "--no-live",
}
KNOWN = set(VALUE_FLAGS) | set(BOOL_FLAGS) | {
    "chat", "aliases", "discard_pending_on", "notes", "comment", "behavior",
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


DEFAULT_NAME = "_default.json"


def default_files() -> list:
    """默认会话配置的候选路径，按优先级：项目根 → 包内（随仓库分发）。"""
    return [os.path.join(DIR, DEFAULT_NAME),
            os.path.join(config.PKG_DIR, "defaults", DEFAULT_NAME)]


def is_default(path: str = "") -> bool:
    return bool(path) and os.path.basename(path) == DEFAULT_NAME


def path_for(name: str) -> str:
    return os.path.join(DIR, "%s.json" % config.safe_name(name))


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
    """给某会话生成配置模板。

    骨架优先取自**生效的默认配置**（`_default.json`：项目根优先，其次包内），
    这样"从默认派生一份专属配置、只改要改的几项"是自然操作；
    没有默认文件时退回内置 DEFAULTS。
    已存在时默认不覆盖，只补齐缺的键。
    """
    seed = load_default() or dict(DEFAULTS)
    seed.pop("chat", None)
    p = path_for(chat)
    cfg = load_file(p) if os.path.exists(p) else {}
    if cfg and not force:
        for k, v in seed.items():
            cfg.setdefault(k, v)
    else:
        cfg = dict(seed)
    cfg["chat"] = cfg.get("chat") or chat
    return dump(cfg, path=p)


def load_default() -> dict:
    """读生效的默认会话配置（项目根 → 包内 defaults/）。都没有返回 {}。"""
    for p in default_files():
        if os.path.exists(p):
            cfg = load_file(p)
            if cfg:
                return cfg
    return {}


def load_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:                                              # noqa: BLE001
        return {}


def find(display: str = "", username: str = "", name: str = None,
         allow_default: bool = True) -> tuple:
    """定位该会话的配置文件。

    顺序：专属配置 → **默认配置**（`_default.json`：项目根优先，其次包内 defaults/）。
    有了默认配置，"对任何人启动对话"才成立 —— 否则未配置的会话会退回 argparse
    的内置默认值，其中 `live=False` 会导致**根本不发送**。

    name 给出时按文件名/路径直接取，不再走默认兜底。
    `_default.json` 自身不会被当成某个会话的专属配置（按 chat 匹配时跳过它）。
    """
    if name:
        cands = [name] if os.path.isabs(name) or os.sep in name else [
            os.path.join(DIR, name if name.endswith(".json") else name + ".json"),
            os.path.join(DIR, "%s.json" % name)]
        for c in cands:
            if os.path.exists(c):
                return c, load_file(c)
        return None, {}
    for p in files():
        if is_default(p):
            continue
        cfg = load_file(p)
        who = [str(cfg.get("chat") or "")] + [str(a) for a in
                                              (cfg.get("aliases") or [])]
        base = os.path.basename(p)[:-5]
        if (display and display in who) or (username and username in who) \
                or (display and display == base):
            return p, cfg
    if allow_default:
        for p in default_files():
            if os.path.exists(p):
                cfg = load_file(p)
                if cfg:
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
            print("[警告] 未找到会话配置 %r，按命令行/内置默认运行（不会自动发送）"
                  % name)
        return {}
    given = getattr(args, "_given", set())
    for k, flag in VALUE_FLAGS.items():
        if k in cfg and flag not in given:
            setattr(args, k, cfg[k])
    for k, flag in BOOL_FLAGS.items():
        if k in cfg and flag not in given:
            setattr(args, k, bool(cfg[k]))
    if getattr(args, "no_live", False):
        # --no-live 是"只出草稿"的显式出口：默认配置里 live=true 时用它压回去
        args.live = False
    args.send_gap = parse_gap(args.send_gap)
    if is_default(path):
        print("会话配置: %s（**默认**，该会话没有专属配置）" % path)
    else:
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
