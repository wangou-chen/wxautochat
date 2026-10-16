# -*- coding: utf-8 -*-
"""密钥加载与 live 库索引。

自包含实现，不依赖父级 wxarchive 包 —— 使 wxautochat 可整目录携带。

密钥缓存格式（复用 wxarchive 的 out/all_keys.json）：
    { "<相对路径，如 message\\message_0.db>": {"enc_key": "<64hex>", "salt": "<32hex>"} }
"""
import fnmatch
import glob
import json
import os
from typing import Dict, Optional

from . import crypto

# 只关心聊天消息分片库。必须排除 message_fts.db / message_resource.db：
# 二者不含 Msg_ 表，纳入轮询只会白白重建整个库。
WATCH_PATTERN = "message_[0-9]*.db"


def collect_databases(db_dir: str, pattern: str = WATCH_PATTERN) -> Dict[str, dict]:
    """{相对路径: {path, salt, page1, page2}}，仅匹配 pattern 的库。"""
    index: Dict[str, dict] = {}
    for root, _dirs, files in os.walk(db_dir):
        for fn in files:
            if not fn.endswith(".db") or fn.endswith(("-wal", "-shm")):
                continue
            if not fnmatch.fnmatch(fn, pattern):
                continue
            path = os.path.join(root, fn)
            if os.path.getsize(path) < crypto.PAGE_SIZE:
                continue
            with open(path, "rb") as f:
                page1 = f.read(crypto.PAGE_SIZE)
                page2 = f.read(crypto.PAGE_SIZE)
            index[os.path.relpath(path, db_dir)] = {
                "path": path, "salt": page1[:crypto.SALT_SIZE],
                "page1": page1, "page2": page2,
            }
    return index


def load_keys(keys_file: str) -> Dict[str, str]:
    """读密钥缓存 {rel_path: enc_key_hex}。文件不存在返回空 dict。"""
    if not keys_file or not os.path.exists(keys_file):
        return {}
    try:
        with open(keys_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:                                              # noqa: BLE001
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, dict) and v.get("enc_key"):
            out[k] = v["enc_key"]
        elif isinstance(v, str) and len(v) == 64:
            out[k] = v
    return out


def resolve_keys(db_dir: str, keys_file: str, verbose: bool = True
                 ) -> Dict[str, bytes]:
    """把密钥缓存与 live 库对应起来，返回 {rel_path: enc_key_bytes}。

    用 salt 交叉校验：缓存里 salt 与当前库不符时作废该条（密钥属于别的数据目录
    或该库已重建），避免用错密钥解出垃圾数据。
    """
    index = collect_databases(db_dir)
    cached = load_keys(keys_file)
    if not cached:
        if verbose:
            print("[提示] 未找到密钥缓存: %s" % keys_file, flush=True)
        return {}
    out: Dict[str, bytes] = {}
    stale = 0
    missing = 0
    for rel, meta in index.items():
        hexkey = cached.get(rel)
        if not hexkey:
            missing += 1
            continue
        try:
            out[rel] = bytes.fromhex(hexkey)
        except ValueError:
            stale += 1
            continue
        if not crypto.check_key(out[rel], meta["page1"], meta["page2"]):
            stale += 1
            del out[rel]
    if verbose:
        print("密钥匹配 %d/%d 个库（salt 校验失败 %d，缓存缺失 %d）"
              % (len(out), len(index), stale, missing), flush=True)
    return out


def db_dir_from_config() -> Optional[str]:
    """从微信自身配置探测 db_storage（探测源与工具所在目录无关）。

    读取 %APPDATA%\\Tencent\\xwechat\\config\\*.ini，其内容为数据根（如 `E:\\`），
    再拼 xwechat_files\\*\\db_storage。
    """
    cfg_dir = os.path.join(os.environ.get("APPDATA", ""),
                           "Tencent", "xwechat", "config")
    roots = []
    if os.path.isdir(cfg_dir):
        for ini in glob.glob(os.path.join(cfg_dir, "*.ini")):
            try:
                with open(ini, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
            except Exception:                                      # noqa: BLE001
                continue
            if content:
                roots.append(content)
    home = os.path.expanduser("~")
    for r in roots:
        for m in glob.glob(os.path.join(r, "xwechat_files", "*", "db_storage")):
            return m
    for m in glob.glob(os.path.join(home, "Documents", "xwechat_files", "*",
                                    "db_storage")):
        return m
    return None
