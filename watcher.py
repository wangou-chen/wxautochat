# -*- coding: utf-8 -*-
"""新消息检测：轮询 live DB 的 WAL，增量解密后扫出新行。

游标策略（实测依据）：
  * `Msg_<md5>` 表的主键是 `local_id`（INTEGER，单调递增），用它做游标，
    不用 `create_time` —— 同一秒内可并列多条，会漏。
  * **游标键必须是「分片库 + 表名」**，不能只用表名。同一个 `Msg_<hash>` 表
    在 message_0/1/2/3.db 里**都存在**，且各分片的 local_id 取值范围互不相同
    （实测翔铭：0.db 到 579、1.db 到 531、2.db 到 240、3.db 到 339）。
    若只按表名索引，先扫的分片会把游标 prime 到自己的最大值，后扫的分片
    一看游标远大于自己的 local_id 范围就会把整片历史当新消息喷出来
    （实测一次喷出 62,836 条历史消息）。
  * 首次见到某 (分片, 表) 时，游标设为该分片内 `max(local_id)`，只作为基线，
    不产出任何消息。
"""
import html
import json
import os
import re
import sqlite3
import time
from typing import Dict, List

from . import sessions

MSG_TYPES = {
    1: "文本", 3: "图片", 34: "语音", 37: "好友请求", 42: "名片", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/文件", 50: "语音通话", 10000: "系统",
    10002: "系统", 227633266737: "群接龙", 244813135921: "引用",
    266287972401: "拍一拍", 373662154801: "@所有人",
}

# 「文本类」= 正文是说话人自己打的字，**必须当普通消息对待**（该回就回）。
# `引用` 的 <appmsg><type>57</type> + <refermsg>，`<title>` 就是他自己打的话；
# 早期把它错标成「视频号」，导致群聊里被"分享类不 @ 我即跳过"整条挡掉。
TEXT_LIKE = ("文本", "引用")


def is_text_like(name: str) -> bool:
    return name in TEXT_LIKE


# ---------------------------------------------------------------- 文本渲染
# 放在 watcher 层（而不是 history）：**增量轮询拿到的消息也必须渲染**。
# 实测（23:32）轮询到的引用消息 `text` 是整段 XML，于是成轮日志、few-shot 关键词、
# drafts.jsonl 里全是 XML；模型看到的是 history 渲染后的文本 —— 两处口径必须一致。
GROUP_PREFIX_RE = re.compile(r"^wxid_[A-Za-z0-9_\-]+:\s*\n?")
# 分享类/引用类消息的正文是 XML，可读信息在 <title>
TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                      re.DOTALL)
# 引用消息（appmsg type=57）里，被引用的那句原文在 <refermsg> 下
REFER_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.DOTALL)
REFER_CONTENT_RE = re.compile(
    r"<content>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</content>", re.DOTALL)
REFER_NAME_RE = re.compile(
    r"<displayname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</displayname>", re.DOTALL)


def render_text(raw, tn: str = "") -> str:
    """把消息正文渲染成人类可读文本。XML 类消息优先取 <title>。

    引用消息额外把**被引用的那句原文**带上：`<title>` 只是他自己打的字，
    光有它模型不知道他在回哪一句（群聊里被引用的那句还可能含 @ 我）。
    """
    t = (raw or "").strip()
    if not t.startswith("<"):
        return t
    m = TITLE_RE.search(t)
    title = html.unescape(m.group(1)).strip() if m else ""
    if tn == "引用":
        ref = REFER_RE.search(t)
        who, quoted = "", ""
        if ref:
            cm = REFER_CONTENT_RE.search(ref.group(1))
            nm = REFER_NAME_RE.search(ref.group(1))
            quoted = html.unescape(cm.group(1)).strip() if cm else ""
            who = html.unescape(nm.group(1)).strip() if nm else ""
        # 被引用的可能是**另一条 XML**（引用里套引用/分享）：实测库里就有
        # `引自🎐「<?xml version="1.0"?> <msg> ...」`。这种要再取一层 <title>，
        # 否则整段 XML 塞进提示词，既没信息量又挤爆 token。
        if quoted.startswith("<"):
            qm = TITLE_RE.search(quoted)
            quoted = html.unescape(qm.group(1)).strip() if qm else ""
        quoted = re.sub(r"\s+", " ", quoted)[:100]
        body = title or "（空）"
        if quoted:
            return "[引用] %s ｜引自%s「%s」" % (body, who or "对方", quoted)
        return "[引用] %s" % body
    if title:
        return "[%s] %s" % (tn, title)
    return "[%s]" % tn


def normalize_text(raw, tn: str = "") -> str:
    """轮询与整库读取共用的正文规范化：剥群聊发送者前缀 + XML 渲染。"""
    t = (raw or "")
    if t and not t.strip().startswith("<"):
        t = GROUP_PREFIX_RE.sub("", t.strip(), count=1)
    return render_text(t, tn)


def type_name(lt) -> str:
    """把 local_type 映射成可读名。

    微信 4.x 会把**子类型编码进高位**：实测 266287972401、34359738417、
    141733920817、219043332145、25769803825、21474836529、17179869233
    等值的低字节都是 0x31(=49, appmsg)。所以查不到时回退到 `lt & 0xFF`。
    """
    if lt is None:
        return "None"
    n = MSG_TYPES.get(lt)
    if n:
        return n
    try:
        low = int(lt) & 0xFF
    except (TypeError, ValueError):
        return str(lt)
    n = MSG_TYPES.get(low)
    return n if n else str(lt)


CURSOR_FILE = "_cursors.json"


def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _zstd(data: bytes) -> str:
    for mod in ("zstandard", "zstd"):
        try:
            m = __import__(mod)
            if mod == "zstandard":
                return m.ZstdDecompressor().decompress(data).decode("utf-8", "replace")
            return m.decompress(data).decode("utf-8", "replace")
        except Exception:                                          # noqa: BLE001
            continue
    return ""


def decompress(data, ct) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if ct == 4:
        return _zstd(data)
    if ct in (0, None):
        return data.decode("utf-8", "replace")
    return ""


class Message(object):
    __slots__ = ("table", "username", "display", "is_group", "local_id",
                 "create_time", "type", "type_name", "sender_id", "sender",
                 "text", "is_mine")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self):
        return "<%s %s %s: %s>" % (
            time.strftime("%H:%M:%S", time.localtime(self.create_time or 0)),
            self.display, self.sender, (self.text or "")[:40])


class Watcher(object):
    """轮询多个 live DB，产出新消息。"""

    def __init__(self, work_dir: str, snapshots: Dict[str, "object"],
                 contact_map: Dict[str, str], md5_map: Dict[str, str],
                 self_wxid: str = "", verbose: bool = True):
        self.work_dir = work_dir
        self.snapshots = snapshots          # {name: Snapshot}
        self.contacts = contact_map
        self.md5map = md5_map
        self.self_wxid = self_wxid
        self.verbose = verbose
        self.cursors: Dict[str, int] = {}
        self._n2i_cache: Dict[str, Dict[int, str]] = {}
        self._load_cursors()

    # ---------------------------------------------------------------- 游标

    def _cursor_path(self):
        return os.path.join(self.work_dir, CURSOR_FILE)

    def _load_cursors(self):
        p = self._cursor_path()
        if os.path.exists(p):
            try:
                # utf-8-sig：PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM，
                # 用 utf-8 读会抛异常。手工调试时难免用 PS 改这个文件，
                # 而静默吞掉解析失败会导致游标全丢、重新建基线。
                with open(p, "r", encoding="utf-8-sig") as f:
                    self.cursors = {k: int(v) for k, v in json.load(f).items()}
            except Exception:                                      # noqa: BLE001
                self.cursors = {}

    def _save_cursors(self):
        """原子保存游标。

        **不能抛异常**：实测两个实例并发写时 `os.replace` 会抛
        `PermissionError [WinError 32]`，把整个轮询循环打崩。丢一次保存不影响
        正确性（下一轮会重写），所以失败只告警。
        """
        from . import lock
        if not lock.write_json(self._cursor_path(), self.cursors):
            if self.verbose:
                print("  [warn] 游标保存失败（可能有另一个实例在写），本轮跳过",
                      flush=True)

    # ---------------------------------------------------------------- 同步

    def sync(self) -> List[str]:
        """对所有快照做增量同步。返回发生变化的库名列表。"""
        changed = []
        for name, snap in self.snapshots.items():
            try:
                r = snap.sync()
            except Exception as e:                                 # noqa: BLE001
                if self.verbose:
                    print("  [warn] %s 同步失败: %s" % (name, e), flush=True)
                continue
            if r.get("changed"):
                changed.append(name)
            # 本地库被重建后，Name2Id 映射需要失效重读
            if r.get("full_rebuild"):
                self._n2i_cache.pop(name, None)
        return changed

    def _n2i(self, db_path: str) -> Dict[int, str]:
        key = os.path.basename(db_path)
        if key not in self._n2i_cache:
            self._n2i_cache[key] = sessions.name2id(db_path)
        return self._n2i_cache[key]

    # ---------------------------------------------------------------- 扫描

    def poll(self) -> List[Message]:
        """同步 + 扫出新消息（按时间升序）。"""
        self.sync()
        out: List[Message] = []
        for name in sorted(self.snapshots):
            db_path = self.snapshots[name].dst
            if not os.path.exists(db_path):
                continue
            try:
                out.extend(self._scan_db(db_path))
            except sqlite3.DatabaseError as e:
                # 本地副本可能正被写入；跳过本轮，下轮重试
                if self.verbose:
                    print("  [warn] %s 读取失败(稍后重试): %s" % (name, e), flush=True)
        # **必须无条件落盘**：首次运行会在 _scan_table 里把各表游标 prime 到
        # max(local_id) 并返回空列表，若只在 `if out` 时保存，基线就永远不会被
        # 持久化 —— 于是每次启动都重新 prime（`--once` 永远静默，
        # 持续模式则反复重扫全部表）。
        self._save_cursors()
        out.sort(key=lambda m: (m.create_time or 0, m.local_id or 0))
        return out

    def _scan_db(self, db_path: str) -> List[Message]:
        con = _open_ro(db_path)
        found: List[Message] = []
        shard = os.path.basename(db_path)
        try:
            n2i = self._n2i(db_path)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in sorted(tables):
                found.extend(self._scan_table(con, db_path, t, n2i, shard))
        finally:
            con.close()
        return found

    def _scan_table(self, con, db_path: str, table: str, n2i: Dict[int, str],
                    shard: str) -> List[Message]:
        cols = [c[1] for c in con.execute('PRAGMA table_info("%s")' % table)]
        if "local_id" not in cols or "create_time" not in cols:
            return []

        # 游标键 = 分片库 + 表名。同一表名存在于多个分片，各分片 local_id 范围不同，
        # 只用表名会导致跨分片串味（见模块 docstring）。
        ckey = "%s/%s" % (shard, table)
        cursor = self.cursors.get(ckey)
        if cursor is None:
            # 首次见到该 (分片, 表)：把该分片内当前最大 local_id 记为已读。
            # **不要减任何回退量**——否则次轮会把这些历史消息当新消息喷出来。
            row = con.execute('SELECT MAX(local_id) FROM "%s"' % table).fetchone()
            self.cursors[ckey] = (row[0] if row and row[0] else 0)
            return []

        sel = [c for c in ("local_id", "local_type", "create_time",
                           "real_sender_id", "message_content",
                           "WCDB_CT_message_content", "source", "WCDB_CT_source")
               if c in cols]
        q = ('SELECT %s FROM "%s" WHERE local_id > ? ORDER BY local_id'
             % (",".join(sel), table))
        rows = list(con.execute(q, (cursor,)))
        if not rows:
            return []

        sid = table[4:]
        uname = self.md5map.get(sid, "")
        display = self.contacts.get(uname, uname) or uname or sid
        group = sessions.is_group(uname)
        msgs: List[Message] = []
        max_id = cursor
        for r in rows:
            rec = dict(zip(sel, r))
            lid = rec.get("local_id") or 0
            max_id = max(max_id, lid)
            text = decompress(rec.get("message_content"),
                              rec.get("WCDB_CT_message_content"))
            if not text:
                text = decompress(rec.get("source"), rec.get("WCDB_CT_source"))
            lt = rec.get("local_type")
            tn = type_name(lt)
            text = normalize_text(text, tn)
            sender_id = n2i.get(rec.get("real_sender_id"), "")
            sender = self.contacts.get(sender_id, sender_id) or str(
                rec.get("real_sender_id"))
            msgs.append(Message(
                table=table, username=uname, display=display, is_group=group,
                local_id=lid, create_time=rec.get("create_time") or 0,
                type=lt, type_name=tn,
                sender_id=sender_id, sender=sender,
                text=text or "",
                is_mine=bool(self.self_wxid and sender_id == self.self_wxid),
            ))
        self.cursors[ckey] = max_id
        return msgs

    # ---------------------------------------------------------------- 重置

    def reset_cursors(self, table: str = None):
        """table=None 清空全部；否则删除 key 中匹配该子串的游标（可给表名或分片名）。"""
        if table:
            for k in [k for k in self.cursors if table in k]:
                del self.cursors[k]
        else:
            self.cursors = {}
        self._save_cursors()
