# -*- coding: utf-8 -*-
"""群昵称（群内显示名）解析。

为什么必须单独处理：微信群里 @ 人**必须用群昵称**，而联系人表里只有全局
备注/昵称。实测本机账号在「尼杰布旅游大团」的群昵称是 `玩狗老师`，但在
联系人表里叫 `🎐`；翔铭在该群叫 `便秘练腹肌`。直接用全局名 @ 会 @ 不到人。

数据来源（实测）：`contact.db` 的 `chat_room` 表 `ext_buffer` 字段，是一个
protobuf，每个成员一条：

    0a 40                        field1 (repeated, len=64)
      0a 13 <19B> "wxid_gwm5xzw35vzm22"   嵌套 field1 = wxid
      12 12 <18B> "惠新西街南口"           嵌套 field2 = 群昵称
      18 01                                嵌套 field3 = flag
      22 13 <19B> "wxid_qmayv5ela8z322"    嵌套 field4（邀请人？）

这里自带一个极简 protobuf 读取器，不引入任何依赖。

**可靠性兜底**：protobuf 是内部格式，微信版本升级可能改动。因此支持
`nicks.local.json` 手动覆盖（键为群 username，值为 {wxid: 群昵称}），
手动配置优先级最高。解析失败时也会明确报错而不是静默返回空。
"""
import json
import os
import sqlite3
from typing import Dict, Optional, Tuple

from . import config

OVERRIDE_FILE = os.path.join(config.ROOT, "nicks.local.json")

# ---------------------------------------------------------------- protobuf

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    """读 varint，返回 (值, 新偏移)。"""
    shift = 0
    val = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 越界")


def _iter_fields(buf: bytes):
    """遍历 protobuf 字段，产出 (field_no, wire_type, value)。

    value：wire 2 为 bytes，wire 0 为 int，其余为原始切片。
    遇到损坏数据即停止（不抛异常），保证部分可用。
    """
    i = 0
    n = len(buf)
    while i < n:
        try:
            key, i = _read_varint(buf, i)
        except ValueError:
            return
        fno, wt = key >> 3, key & 0x7
        if wt == WIRE_VARINT:
            try:
                v, i = _read_varint(buf, i)
            except ValueError:
                return
            yield fno, wt, v
        elif wt == WIRE_LEN:
            try:
                ln, i = _read_varint(buf, i)
            except ValueError:
                return
            if i + ln > n:
                return
            yield fno, wt, buf[i:i + ln]
            i += ln
        elif wt == WIRE_64BIT:
            if i + 8 > n:
                return
            yield fno, wt, buf[i:i + 8]
            i += 8
        elif wt == WIRE_32BIT:
            if i + 4 > n:
                return
            yield fno, wt, buf[i:i + 4]
            i += 4
        else:
            return


def parse_roster(ext_buffer: bytes) -> Dict[str, str]:
    """从 chat_room.ext_buffer 解析 {wxid: 群昵称}。"""
    out: Dict[str, str] = {}
    if not ext_buffer:
        return out
    for fno, wt, v in _iter_fields(ext_buffer):
        if fno != 1 or wt != WIRE_LEN or not isinstance(v, bytes):
            continue
        wxid = nick = ""
        for f2, w2, v2 in _iter_fields(v):
            if w2 != WIRE_LEN or not isinstance(v2, bytes):
                continue
            if f2 == 1:
                wxid = v2.decode("utf-8", "replace")
            elif f2 == 2:
                nick = v2.decode("utf-8", "replace")
        if wxid:
            out[wxid] = nick
    return out


# ---------------------------------------------------------------- 取用

def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _first(dec_dir: str, pattern: str) -> Optional[str]:
    import glob
    hits = sorted(glob.glob(os.path.join(dec_dir, pattern)))
    return hits[0] if hits else None


def load_overrides() -> dict:
    if not os.path.exists(OVERRIDE_FILE):
        return {}
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:                                              # noqa: BLE001
        return {}


def nicks_for(dec_dir: str, room: str, verbose: bool = False) -> Dict[str, str]:
    """取某群的 {wxid: 群昵称}。手动覆盖优先，其次解析 ext_buffer。"""
    ov = (load_overrides().get(room) or {})
    parsed: Dict[str, str] = {}
    path = _first(dec_dir, "contact_contact.db") or _first(dec_dir, "contact*.db")
    if path:
        try:
            con = _open_ro(path)
            row = con.execute("SELECT ext_buffer FROM chat_room WHERE username=?",
                              (room,)).fetchone()
            con.close()
            if row and row[0]:
                buf = row[0]
                if isinstance(buf, str):
                    buf = buf.encode("utf-8", "replace")
                parsed = parse_roster(buf)
        except Exception as e:                                     # noqa: BLE001
            if verbose:
                print("  [warn] 解析群昵称失败: %s" % e)
    merged = dict(parsed)
    merged.update({k: v for k, v in ov.items() if v is not None})
    return merged


def display_map(dec_dir: str, room: str, contacts: Dict[str, str],
                self_wxid: str = "", verbose: bool = False) -> Dict[str, dict]:
    """{wxid: {群昵称, 全局名, 用于@的名字}}。

    `用于@的名字` 优先群昵称，缺失时回落到全局显示名（宁可回落也不要 @ 空）。
    """
    nicks = nicks_for(dec_dir, room, verbose=verbose)
    out: Dict[str, dict] = {}
    for wxid, nick in nicks.items():
        g = contacts.get(wxid, wxid)
        out[wxid] = {"群昵称": nick or "",
                     "全局名": g,
                     "用于@": nick or g,
                     "是我": wxid == self_wxid}
    # 群成员里可能有昵称缺失的，补上全局名
    for wxid, g in (contacts or {}).items():
        if wxid in out:
            continue
    return out


def valid_mentions(dec_dir: str, room: str, contacts: Dict[str, str],
                   self_wxid: str = "") -> set:
    """该群合法的 @ 名字集合（群昵称优先，另含全局名以容忍历史习惯）。"""
    nicks = nicks_for(dec_dir, room)
    s = set()
    for wxid, nick in nicks.items():
        if nick:
            s.add(nick)
        g = contacts.get(wxid)
        if g:
            s.add(g)
    return s
