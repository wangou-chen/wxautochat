# -*- coding: utf-8 -*-
"""配置与路径解析。

原则：
  * **不含任何机器特定硬编码**。所有路径由项目根目录推导，或经命令行/环境变量覆盖。
  * **密钥不入库**。DeepSeek API key 只从环境变量或 gitignore 的本地配置读取。
"""
import glob
import json
import os

# 项目根 = 本文件所在包目录的上一级
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG_DIR)

# ---------------------------------------------------------------- DeepSeek

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_FLASH = "deepseek-flash"          # 便宜档：DeepSeek-V4.1-Flash
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"           # 贵档

# 环境变量名（key 只走环境变量或本地 config.local.json）
ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_MODEL = "DEEPSEEK_MODEL"

LOCAL_CONFIG = os.path.join(ROOT, "config.local.json")

# ---------------------------------------------------------------- 路径

# 复用的 wxarchive 已解密目录（P1 读取会话名/联系人映射的来源之一）
DEFAULT_DECRYPTED = os.path.join(ROOT, "decrypted")
# wxautochat 自己的增量工作目录（由 live DB 的 WAL 增量维护）
DEFAULT_WORK = os.path.join(ROOT, "work")


def load_local() -> dict:
    """读本地配置（gitignore）。不存在则返回空 dict。"""
    if not os.path.exists(LOCAL_CONFIG):
        return {}
    try:
        with open(LOCAL_CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:                                              # noqa: BLE001
        return {}


def db_dir(explicit: str = None) -> str:
    """解析微信 live 数据目录。

    优先级：命令行 > 本地配置 > 环境变量 > 复用 wxarchive 的自动探测。
    显式给出但无效时**直接返回 None**，不静默回退。
    """
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    cfg = load_local()
    if cfg.get("db_dir"):
        p = cfg["db_dir"]
        return p if os.path.isdir(p) else None
    env = os.environ.get("WXARCHIVE_DB_DIR")
    if env:
        return env if os.path.isdir(env) else None
    # 兜底：自行探测微信自身 ini（与工具所在目录无关，见 keys.db_dir_from_config）
    from . import keys
    return keys.db_dir_from_config()


def state_dir(explicit: str = None) -> str:
    if explicit:
        return explicit
    cfg = load_local()
    d = cfg.get("work") or os.environ.get("WXAUTOCHAT_WORK") or DEFAULT_WORK
    os.makedirs(d, exist_ok=True)
    return d


def api_key() -> str:
    """DeepSeek API key：环境变量优先，其次本地配置。"""
    return os.environ.get(ENV_API_KEY) or load_local().get("api_key") or ""


def base_url() -> str:
    return os.environ.get(ENV_BASE_URL) or load_local().get("base_url") \
        or DEEPSEEK_BASE_URL


def model() -> str:
    return os.environ.get(ENV_MODEL) or load_local().get("model") \
        or DEEPSEEK_MODEL_FLASH


def _search_nearby(rel_candidates, glob_patterns):
    """在项目根及其邻近目录中查找文件/目录，**由近及远**。

    为什么需要：本工具可能被放在 `WeChatExport\\wxautochat`（仓库内，零配置），
    也可能被放在 `wxtest\\wxautochat`（与仓库同级）。后者访问不到
    `..\\out\\all_keys.json`，需要去兄弟目录里找。
    与其强制手写配置，不如做一次有界的就近搜索。

    搜索顺序（命中即返回，不递归更深层）：
        1) <base>/<rel>                工具自己的仓库
        2) <base>/*/<rel>              base 下的兄弟仓库（如 wxtest/WeChatExport/out）
        3) <base>/../<rel>             base 的父级
        4) <base>/../*/<rel>           base 父级的兄弟目录
    """
    base = ROOT
    parent = os.path.dirname(base)
    for rel in rel_candidates:
        for p in (os.path.join(base, rel),
                  os.path.join(parent, rel)):
            if os.path.exists(p):
                return p
    for pat in glob_patterns:
        for root in (base, parent):          # 先 base 下，再向上
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[0]
    return None


def keys_file(explicit: str = None) -> str:
    """SQLCipher 密钥缓存（wxarchive 的产物）。

    优先级：--keys-file > config.local.json > $env:WXAUTOCHAT_KEYS >
    <项目根>\\out\\all_keys.json > 邻近目录搜索 > 默认路径（可能不存在）。
    """
    if explicit:
        return explicit
    cfg = load_local()
    if cfg.get("keys_file"):
        return cfg["keys_file"]
    env = os.environ.get("WXAUTOCHAT_KEYS")
    if env:
        return env
    found = _search_nearby(
        [os.path.join("out", "all_keys.json")],
        [os.path.join("*", "out", "all_keys.json")])
    if found:
        return found
    return os.path.join(ROOT, "out", "all_keys.json")


def decrypted_dir(explicit: str = None) -> str:
    """wxarchive 的已解密目录，仅用于读会话名/联系人映射。

    优先级同上；找不到时返回 None，调用方会降级为「无显示名映射」而不报错。
    """
    if explicit:
        return explicit
    cfg = load_local()
    if cfg.get("decrypted"):
        return cfg["decrypted"]
    env = os.environ.get("WXAUTOCHAT_DECRYPTED")
    if env:
        return env
    found = _search_nearby(["decrypted"],
                           [os.path.join("*", "decrypted")])
    return found
