# -*- coding: utf-8 -*-
"""会话 / 联系人 / 发送者映射。

关键规则（本机实测 239/239 命中）：
    Msg_<hash> 的 <hash> = md5(SessionTable.username).hexdigest()
    发送者链：real_sender_id → 消息库 Name2Id.rowid → user_name（列名是 user_name）
              → contact.db.contact → coalesce(remark, nick_name, username)
"""
import glob
import hashlib
import os
import re
import sqlite3
from typing import Dict, Optional


def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _first(dec_dir: str, pattern: str) -> Optional[str]:
    hits = sorted(glob.glob(os.path.join(dec_dir, pattern)))
    return hits[0] if hits else None


def session_md5_map(dec_dir: str) -> Dict[str, str]:
    """{md5(username): username}"""
    path = _first(dec_dir, "session_session.db") or _first(dec_dir, "session*.db")
    out: Dict[str, str] = {}
    if not path:
        return out
    try:
        con = _open_ro(path)
        for (u,) in con.execute("SELECT username FROM SessionTable"):
            if u:
                out[hashlib.md5(u.encode("utf-8")).hexdigest()] = u
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return out


def contacts(dec_dir: str) -> Dict[str, str]:
    """{username: 显示名}，显示名优先级 remark > nick_name > username"""
    path = _first(dec_dir, "contact_contact.db") or _first(dec_dir, "contact*.db")
    out: Dict[str, str] = {}
    if not path:
        return out
    try:
        con = _open_ro(path)
        for u, remark, nick in con.execute(
                "SELECT username, remark, nick_name FROM contact"):
            if u:
                out[u] = remark or nick or u
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return out


def contact_aliases(dec_dir: str) -> Dict[str, list]:
    """{username: [显示名, 备注, 昵称, 别名, ...]}。

    为什么需要：`contacts()` 只给出优先级最高的那个名字（通常是备注）。
    但用户更可能输入**昵称**（实测「欧米茄」是昵称，备注是 `𓁅`），
    只按显示名匹配会找不到会话。把全部可选名字都收进来做匹配。
    """
    path = _first(dec_dir, "contact_contact.db") or _first(dec_dir, "contact*.db")
    out: Dict[str, list] = {}
    if not path:
        return out
    try:
        con = _open_ro(path)
        cols = [c[1] for c in con.execute("PRAGMA table_info(contact)")]
        want = [c for c in ("username", "remark", "nick_name", "alias")
                if c in cols]
        for r in con.execute("SELECT %s FROM contact" % ",".join(want)):
            rec = dict(zip(want, r))
            u = rec.get("username")
            if not u:
                continue
            names = [u]
            for k in ("remark", "nick_name", "alias"):
                v = (rec.get(k) or "").strip()
                if v and v not in names:
                    names.append(v)
            out[u] = names
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return out


def name2id(msg_db: str) -> Dict[int, str]:
    """消息库内 Name2Id: rowid -> user_name"""
    out: Dict[int, str] = {}
    try:
        con = _open_ro(msg_db)
        for rid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
            out[rid] = uname
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return out


def session_list(dec_dir: str) -> list:
    """[(username, last_timestamp, unread_count, summary)]，按 sort_timestamp 降序"""
    path = _first(dec_dir, "session_session.db") or _first(dec_dir, "session*.db")
    rows = []
    if not path:
        return rows
    try:
        con = _open_ro(path)
        cols = [c[1] for c in con.execute("PRAGMA table_info(SessionTable)")]
        sel = [c for c in ("username", "last_timestamp", "unread_count", "summary",
                           "sort_timestamp") if c in cols]
        q = "SELECT %s FROM SessionTable ORDER BY %s DESC" % (
            ",".join(sel),
            "sort_timestamp" if "sort_timestamp" in cols else "last_timestamp")
        for r in con.execute(q):
            rec = dict(zip(sel, r))
            rows.append((rec.get("username"), rec.get("last_timestamp"),
                         rec.get("unread_count"), rec.get("summary")))
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return rows


def is_group(username: str) -> bool:
    """群聊 username 形如 <数字>@chatroom；公众号 gh_xxx；其余为个人。"""
    return bool(username) and username.endswith("@chatroom")


WXID_RE = re.compile(r"^(wxid_[A-Za-z0-9]+?)(?:_\d+)?$")


def self_username(live_db_dir: str, dec_dir: str = None) -> str:
    """本机登录账号 wxid。

    首选来源（实测）：live `db_storage` 的**父目录名**形如
        E:\\xwechat_files\\wxid_usdvuhs727dr22_2291\\db_storage
    其中 `_2291` 为客户端附加的数字后缀，去掉后即真实 wxid。

    备选（目录名被改写时）：general.db 的 transferTable.pay_payer，
    该字段记录"我付款"的账号，实测为本机 wxid。
    """
    m = WXID_RE.match(os.path.basename(os.path.dirname(os.path.abspath(live_db_dir))))
    if m:
        return m.group(1)
    if not dec_dir:
        return ""
    path = _first(dec_dir, "general_general.db")
    if not path:
        return ""
    try:
        con = _open_ro(path)
        for (v,) in con.execute(
                "SELECT pay_payer FROM transferTable "
                "WHERE pay_payer LIKE 'wxid_%' LIMIT 20"):
            if v:
                con.close()
                return str(v)
        con.close()
    except Exception:                                             # noqa: BLE001
        pass
    return ""
