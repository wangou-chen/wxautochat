# -*- coding: utf-8 -*-
"""从 P1 的本地快照读取某会话的完整历史。

为什么读快照而不读 wxarchive 的 `out/single/*.json`：
  * 快照覆盖**全部 679 个分片表**，而 single 只导过 5 个会话；
  * 快照由 WAL 增量维护，比静态导出更新（实测领先 4 小时 / 536 行）；
  * 不依赖上一次是否跑过 `wxarchive chat`。

会话定位：显示名子串 / username / `Msg_` 表名，与 P1 的 watch 保持一致。
"""
import os
from typing import Dict, List, Optional

from . import sessions, watcher as wmod

# 群聊里**别人发的**消息，message_content 内嵌发送者前缀，形如：
#     wxid_gwm5xzw35vzm22: 用不用开电脑
# 自己发的不带。不剥离会浪费 token、干扰模型，也会让 `--who other` 的
# 统计长度被系统性抬高（wxarchive 的 render() 同样会剥离，此处保持一致）。
# 文本渲染统一放在 watcher 层：**增量轮询与整库读取必须同一口径**，
# 否则轮询到的引用消息是整段 XML（成轮日志、few-shot 关键词、drafts.jsonl 全被污染）。
# 这里做转发保持旧调用点（behavior.py 等）不变。
GROUP_PREFIX_RE = wmod.GROUP_PREFIX_RE
TITLE_RE = wmod.TITLE_RE
REFER_RE = wmod.REFER_RE
REFER_CONTENT_RE = wmod.REFER_CONTENT_RE
REFER_NAME_RE = wmod.REFER_NAME_RE
render_text = wmod.render_text
normalize_text = wmod.normalize_text


def render_text(raw, type_name: str) -> str:
    """兼容旧调用点；实现已统一到 watcher.render_text。"""
    return wmod.render_text(raw, type_name)


class Session(object):
    __slots__ = ("username", "display", "table", "is_group", "messages")

    def __init__(self, username="", display="", table="", is_group=False,
                 messages=None):
        self.username = username
        self.display = display
        self.table = table
        self.is_group = is_group
        self.messages = messages or []

    def speakers(self) -> Dict[str, int]:
        """{发言人: 条数}，按条数降序。"""
        c: Dict[str, int] = {}
        for m in self.messages:
            c[m.sender or "?"] = c.get(m.sender or "?", 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    def mine(self) -> List:
        return [m for m in self.messages if m.is_mine]

    def theirs(self) -> List:
        return [m for m in self.messages if not m.is_mine]

    def self_name(self) -> str:
        """本机账号在消息里的 sender 显示名。

        注意：`Message.sender` 存的是**联系人显示名**，本机账号也会解析成
        自己的昵称（实测是 `🎐`），并不是字符串 "我"。"我"/"对方" 只是
        CLI 打印时才加的标签。所以判定"谁是我"必须用 `is_mine`（由
        self_wxid 比对得出），不能靠名字比较。
        """
        for m in self.messages:
            if m.is_mine and m.sender:
                return m.sender
        return ""

    def other_name(self) -> str:
        """非我方的发言人。私聊即唯一对方；群聊取发言最多者。"""
        sp = self.speakers()
        for name in sp:
            if name != self.self_name():
                return name
        return self.display


def resolve(snapshots: Dict[str, object], md5map: Dict[str, str],
            contacts: Dict[str, str], query: str,
            self_wxid: str = "", aliases: Dict[str, list] = None) -> List[dict]:
    """把 query 解析成候选会话。

    匹配范围：**全部已知名字**（备注、昵称、别名）、username、表名 hash。
    只匹配"显示名"是不够的 —— 显示名取的是备注优先，而用户更可能输入昵称
    （实测「欧米茄」是昵称，备注为 `𓁅`）。
    返回 [{table, username, display, is_group, exact}]。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = []
    for sid, uname in md5map.items():
        disp = contacts.get(uname, "") or uname
        names = [uname, disp] + list((aliases or {}).get(uname, []))
        cand = [str(n).lower() for n in names if n]
        exact = q in cand
        sub = any(q in c for c in cand) or (q in sid)
        if exact or sub:
            hits.append({"table": "Msg_" + sid, "username": uname,
                         "display": disp, "is_group": sessions.is_group(uname),
                         "exact": exact, "matched": next(
                             (n for n in cand if q == n or q in n), "")})
    # 直接给 Msg_/hash
    if not hits:
        h = q[4:] if q.startswith("msg_") else q
        if len(h) == 32 and all(c in "0123456789abcdef" for c in h):
            uname = md5map.get(h, "")
            hits.append({"table": "Msg_" + h, "username": uname,
                         "display": contacts.get(uname, uname) or uname,
                         "is_group": sessions.is_group(uname), "exact": True})
    # 精确命中优先；其次按显示名长度（短名通常更接近用户意图）
    hits.sort(key=lambda x: (not x["exact"], -len(x["display"] or "")))
    return hits


def read(snapshots: Dict[str, object], target: dict, contacts: Dict[str, str],
         self_wxid: str = "", limit: int = 0,
         types: Optional[set] = None) -> Session:
    """从所有分片读该会话的消息，合并、按时间排序、去重。

    limit: 只保留**最近** N 条（0 = 全部）。
    types: 只要这些 local_type（None = 全部）。
    """
    import sqlite3
    table = target["table"]
    msgs = []
    seen = set()
    for name in sorted(snapshots):
        db = snapshots[name].dst
        if not os.path.exists(db):
            continue
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            cols = [c[1] for c in con.execute('PRAGMA table_info("%s")' % table)]
            if "local_id" not in cols:
                continue
            n2i = sessions.name2id(db)
            sel = [c for c in ("local_id", "local_type", "create_time",
                               "real_sender_id", "message_content",
                               "WCDB_CT_message_content", "source",
                               "WCDB_CT_source") if c in cols]
            for r in con.execute('SELECT %s FROM "%s" ORDER BY create_time'
                                 % (",".join(sel), table)):
                rec = dict(zip(sel, r))
                lt = rec.get("local_type")
                if types is not None and lt not in types:
                    continue
                text = wmod.decompress(rec.get("message_content"),
                                       rec.get("WCDB_CT_message_content"))
                if not text:
                    text = wmod.decompress(rec.get("source"),
                                           rec.get("WCDB_CT_source"))
                # 统一规范化：剥群聊发送者前缀 + XML 取 <title>（引用带出被引句）
                text = wmod.normalize_text(text, wmod.type_name(lt))
                sid = n2i.get(rec.get("real_sender_id"), "")
                sender = contacts.get(sid, sid) or str(rec.get("real_sender_id"))
                ts = rec.get("create_time") or 0
                # 分片间去重：(时间, 发送者, 原文, 类型)
                dk = (ts, sender, text, lt)
                if dk in seen:
                    continue
                seen.add(dk)
                msgs.append(wmod.Message(
                    table=table, username=target["username"],
                    display=target["display"], is_group=target["is_group"],
                    local_id=rec.get("local_id"), create_time=ts,
                    type=lt, type_name=wmod.type_name(lt),
                    sender_id=sid, sender=sender, text=text or "",
                    is_mine=bool(self_wxid and sid == self_wxid)))
        except sqlite3.DatabaseError:
            continue
        finally:
            con.close()

    msgs.sort(key=lambda m: (m.create_time or 0, m.local_id or 0))
    if limit:
        msgs = msgs[-limit:]
    return Session(username=target["username"], display=target["display"],
                   table=table, is_group=target["is_group"], messages=msgs)
