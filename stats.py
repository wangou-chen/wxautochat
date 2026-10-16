# -*- coding: utf-8 -*-
"""确定性统计层（纯 Python，零 token）。

设计原则：**这一层只产出事实**，不做任何解释。解释交给 LLM，两者用途不同：
  * 本层给出「吧 397 次、≤8 字占 82%」这类可复现的数字
  * LLM 层给出「用短句维持轻松感」这类语义解释
  * 最终风格卡片同时保留两者，LLM 的结论若没有本层数字支撑，
    会被要求自行标注为"推测"（见 profile.py 的提示词）

所有指标都在真实数据上验证过，不要凭直觉改阈值。
"""
import collections
import datetime
import re
from typing import Dict, List

# 语气词 / 句末助词（实测高频，覆盖绝大多数口语）
FILLERS = ["吧", "啊", "吗", "呢", "呀", "嘛", "哈", "嗯", "哦", "噢", "诶", "哎",
           "唉", "嘿", "哇", "咯", "啦", "嘞", "滴", "呐", "唔", "噻", "嗷"]
# 缓冲/模糊表达——用于判断"表态前先缓和"的习惯
HEDGES = ["我觉得", "可能", "应该", "其实", "感觉", "好像", "大概", "也许",
          "估计", "似乎", "说不定", "或许", "不一定"]
# 附和
AGREE = ["对", "对呀", "太对了", "确实", "可以", "okk", "ok", "行", "好的",
         "嗯嗯", "嗯呢", "好", "是", "对的对的", "没问题", "是的", "对啊"]
# 口语粗话/网络语
SLANG = ["我靠", "靠", "草", "淦", "绷", "绝了", "寄", "麻了", "小丑", "笑死",
         "666", "牛逼", "服了", "真服了", "无语", "乐", "emo"]

PUNCT = "，。！？、；：～~…—,.!?;:'\"（）()《》【】"
# 句末标点
TAIL_PUNCT = ("。", "！", "？", "~", "～", "…", ".", "!", "?")
# 真 emoji（非微信表情标签）
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF"
    "\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2b00-\u2bff]")
# 微信表情标签，如 [旺柴]、[微笑]、[Facepalm]
TAG_RE = re.compile(r"^\[[^\[\]]{1,12}\]$")
URL_RE = re.compile(r"https?://|www\.", re.I)
CJK_SEG_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]+$")


def _pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 2) if b else 0.0


def text_metrics(texts: List[str]) -> dict:
    """文本长度/标点/emoji/标签等基础指标。"""
    n = len(texts)
    if not n:
        return {"条数": 0}
    lens = [len(t) for t in texts]
    sl = sorted(lens)
    with_punct = sum(1 for t in texts if any(c in t for c in PUNCT))
    tail_q = sum(1 for t in texts if t.rstrip().endswith(("?", "？")))
    tail_e = sum(1 for t in texts if t.rstrip().endswith(("!", "！")))
    tail_p = sum(1 for t in texts if t.rstrip().endswith(("。", ".")))
    tail_none = sum(1 for t in texts
                    if t and t.rstrip()[-1] not in TAIL_PUNCT)
    emoji_msgs = sum(1 for t in texts if EMOJI_RE.search(t))
    tag_msgs = sum(1 for t in texts if TAG_RE.match(t.strip()))
    url_msgs = sum(1 for t in texts if URL_RE.search(t))
    return {
        "条数": n,
        "平均长度": round(sum(lens) / n, 2),
        "中位长度": sl[n // 2],
        "P90长度": sl[int(n * 0.9)],
        "最长": max(lens),
        "长度分布": {
            "≤2字占比": _pct(sum(1 for x in lens if x <= 2), n),
            "≤8字占比": _pct(sum(1 for x in lens if x <= 8), n),
            ">35字占比": _pct(sum(1 for x in lens if x > 35), n),
        },
        "标点": {
            "含标点占比": _pct(with_punct, n),
            "句末问号": tail_q,
            "句末感叹号": tail_e,
            "句末句号": tail_p,
            "句末无标点占比": _pct(tail_none, n),
        },
        "emoji": {"含emoji条数": emoji_msgs, "占比": _pct(emoji_msgs, n)},
        "微信表情标签": {"条数": tag_msgs, "占比": _pct(tag_msgs, n)},
        "含链接条数": url_msgs,
    }


def word_metrics(texts: List[str]) -> dict:
    """语气词/缓冲语/附和/粗话/整句重复/高频片段。"""
    joined = "\n".join(texts)
    n = max(1, len(texts))
    fillers = {w: joined.count(w) for w in FILLERS}
    fillers = {k: v for k, v in sorted(fillers.items(), key=lambda kv: -kv[1])
               if v > 0}
    hedge_hits = {w: sum(1 for t in texts if w in t) for w in HEDGES}
    hedge_hits = {k: v for k, v in sorted(hedge_hits.items(),
                                          key=lambda kv: -kv[1]) if v > 0}
    slang_hits = {w: sum(1 for t in texts if w in t) for w in SLANG}
    slang_hits = {k: v for k, v in sorted(slang_hits.items(),
                                          key=lambda kv: -kv[1]) if v > 0}
    agree = sum(1 for t in texts if t in AGREE
                or any(t.startswith(a) for a in AGREE))
    whole = collections.Counter(t for t in texts if len(t) <= 12)
    frag = collections.Counter()
    for t in texts:
        for k in (2, 3):
            for i in range(len(t) - k + 1):
                seg = t[i:i + k]
                if CJK_SEG_RE.match(seg):
                    frag[seg] += 1
    return {
        "语气词": fillers,
        "缓冲语": hedge_hits,
        "缓冲语条数占比": _pct(
            sum(1 for t in texts if any(h in t for h in HEDGES)), n),
        "网络语": slang_hits,
        "附和条数": agree,
        "附和占比": _pct(agree, n),
        "整句高频 (≥3次)": [(t, c) for t, c in whole.most_common(25) if c >= 3],
        "高频2-3字片段 (≥8次)": [(t, c) for t, c in frag.most_common(40)
                                 if c >= 8][:25],
        "含问号占比": _pct(sum(1 for t in texts if "?" in t or "？" in t), n),
    }


def burst_metrics(messages: List, who: str) -> dict:
    """连发节奏：同一人连续发言算一段 burst。"""
    seq = []
    cur, cnt = None, 0
    for m in messages:
        s = m.sender
        if s == cur:
            cnt += 1
        else:
            if cur is not None:
                seq.append((cur, cnt))
            cur, cnt = s, 1
    if cur is not None:
        seq.append((cur, cnt))
    mine = [c for w, c in seq if w == who]
    return {
        "连发段数": len(mine),
        "平均连发条数": round(sum(mine) / len(mine), 2) if mine else 0,
        "最长连发": max(mine) if mine else 0,
        "连发≥3比例": _pct(sum(1 for x in mine if x >= 3), len(mine)),
    }


def time_metrics(messages: List) -> dict:
    """活跃时段与月度分布。"""
    hours = collections.Counter()
    months = collections.Counter()
    for m in messages:
        if not m.create_time:
            continue
        d = datetime.datetime.fromtimestamp(m.create_time)
        hours[d.hour] += 1
        months[d.strftime("%Y-%m")] += 1
    return {
        "活跃小时": {str(h): hours.get(h, 0) for h in range(24)},
        "凌晨占比": _pct(sum(hours.get(h, 0) for h in (0, 1, 2, 3, 4)),
                         sum(hours.values())),
        "月度分布": dict(sorted(months.items())),
    }


def type_metrics(messages: List, who: str = None) -> dict:
    """消息类型构成（语音/图片/表情占比对拟语气很关键）。"""
    msgs = [m for m in messages if who is None or m.sender == who]
    c = collections.Counter(m.type_name for m in msgs)
    n = max(1, len(msgs))
    return {"类型分布": dict(c.most_common()),
            "非文本占比": _pct(sum(v for k, v in c.items() if k != "文本"), n)}


def reply_latency(messages: List, me: str, other: str) -> dict:
    """我回复对方的延迟分布（秒）。P3 用它做拟人化发送间隔。"""
    gaps = []
    pending = None
    for m in messages:
        if m.sender == other:
            if pending is None:
                pending = m.create_time
        elif m.sender == me and pending:
            if m.create_time and m.create_time >= pending:
                gaps.append(m.create_time - pending)
            pending = None
    if not gaps:
        return {"样本数": 0}
    g = sorted(gaps)
    n = len(g)
    return {
        "样本数": n,
        "中位秒": g[n // 2],
        "P25秒": g[n // 4],
        "P75秒": g[int(n * 0.75)],
        "P90秒": g[int(n * 0.9)],
        "≤10秒占比": _pct(sum(1 for x in g if x <= 10), n),
        ">5分钟占比": _pct(sum(1 for x in g if x > 300), n),
    }


def speaker_facts(session, who: str) -> dict:
    """某个发言人的全部确定性事实。"""
    mine = [m for m in session.messages if m.sender == who]
    texts = [m.text.strip() for m in mine
             if m.type_name == "文本" and m.text and m.text.strip()]
    texts = [t for t in texts if not URL_RE.search(t)]
    return {
        "发言人": who,
        "总条数": len(mine),
        "文本条数": len(texts),
        "文本指标": text_metrics(texts),
        "用词指标": word_metrics(texts),
        "连发指标": burst_metrics(session.messages, who),
        "时段指标": time_metrics(mine),
        "类型指标": type_metrics(session.messages, who),
    }


def session_facts(session, self_wxid: str = "", self_display: str = "") -> dict:
    """会话级事实：双方发言量、回复延迟。"""
    sp = session.speakers()
    me = self_display or "我"
    other = next((k for k in sp if k != me), "")
    facts = {
        "会话": session.display,
        "username": session.username,
        "是否群聊": session.is_group,
        "总条数": len(session.messages),
        "发言人数": len(sp),
        "发言分布": dict(list(sp.items())[:15]),
        "首条时间": session.messages[0].create_time if session.messages else 0,
        "末条时间": session.messages[-1].create_time if session.messages else 0,
    }
    if not session.is_group and other:
        facts["我回复延迟"] = reply_latency(session.messages, me, other)
        facts["对方回复延迟"] = reply_latency(session.messages, other, me)
    return facts
