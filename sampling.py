# -*- coding: utf-8 -*-
"""分层采样（纯 Python，零 token）。

为什么不用随机采样：随机抽样会让样本偏向高频场景（日常寒暄），丢掉
低频但重要的语域差异（正式表达、长文、深夜、冲突）。这里按**时间 × 长度**
分层，保证覆盖度，且用固定随机种子保证可复现（风格卡片可 diff）。

产出两样东西：
  1. `sample_pairs`   —— 「对方说 → 我回」的配对，供 P3 做 few-shot
  2. `sample_dialog`  —— 连续对话窗口，供 LLM 感受真实语域与话题
"""
import random
import re
from typing import Dict, List

MAX_CTX_MSGS = 3          # 每个 pair 的上下文最多几条
MAX_REPLY_MSGS = 4        # 每个 pair 我的回复最多几条
MAX_MSG_CHARS = 160       # 单条消息截断长度


def _clip(s: str, n: int = MAX_MSG_CHARS) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _prepare(messages: List) -> List:
    """把消息压成紧凑元组，过滤噪音。"""
    out = []
    for m in messages:
        t = (m.text or "").strip()
        if m.type_name != "文本":
            t = "[%s]" % m.type_name
        elif not t:
            continue
        if t.startswith("<"):
            t = "[%s]" % m.type_name
        if len(t) > 400:                       # 超长（多为转发/通知）单独处理
            t = t[:400] + "…"
        out.append((m.create_time or 0, m.sender, _clip(t), m.type_name))
    return out


def _bursts(msgs: List) -> List[dict]:
    """把消息切成 burst（同一人连续发言为一段）。"""
    seq = []
    for ts, sender, text, tn in msgs:
        if seq and seq[-1]["sender"] == sender:
            seq[-1]["items"].append((ts, text, tn))
        else:
            seq.append({"sender": sender, "items": [(ts, text, tn)]})
    return seq


def sample_pairs(messages: List, me: str, other: str, n: int = 24,
                 seed: int = 20261016) -> List[dict]:
    """抽取「对方 burst → 我 burst」配对，按回复长度分层覆盖。

    other=None 表示"任意非我发言人"（群聊里给自己做画像时使用，此时上下文
    可能来自不同的人，按发生顺序取即可）。

    分层：把候选按"我的回复字数"分成 短(≤6) / 中(7~30) / 长(>30) 三档，
    按比例分配名额，确保长回复（通常更正式、信息量更大）不被淹没。
    """
    bs = _bursts(_prepare(messages))
    pairs = []
    for i in range(len(bs) - 1):
        prev = bs[i]["sender"]
        if prev == me or bs[i + 1]["sender"] != me:
            continue
        if other is not None and prev != other:
            continue
        ctx = [t for _ts, t, _tn in bs[i]["items"]][-MAX_CTX_MSGS:]
        rep = [t for _ts, t, _tn in bs[i + 1]["items"]][:MAX_REPLY_MSGS]
        if not rep:
            continue
        pairs.append({
            "ts": bs[i + 1]["items"][0][0],
            "context": ctx,
            "reply": rep,
            "reply_chars": sum(len(x) for x in rep),
        })
    if not pairs:
        return []

    short = [p for p in pairs if p["reply_chars"] <= 6]
    mid = [p for p in pairs if 7 <= p["reply_chars"] <= 30]
    long_ = [p for p in pairs if p["reply_chars"] > 30]
    quota = {"short": max(1, n // 3), "mid": max(1, n // 3),
             "long": max(1, n - 2 * (n // 3))}

    rnd = random.Random(seed)
    picked = []
    for key, pool in (("short", short), ("mid", mid), ("long", long_)):
        if not pool:
            continue
        # 池内按时间分桶，每桶取一条 -> 时间覆盖
        pool = sorted(pool, key=lambda p: p["ts"])
        k = min(quota[key], len(pool))
        idxs = [int(i * (len(pool) - 1) / max(1, k - 1)) for i in range(k)] \
            if k > 1 else [0]
        for i in sorted(set(idxs)):
            picked.append(pool[i])
    # 名额没用满就从剩余里补，仍按时间均布
    if len(picked) < n:
        rest = [p for p in pairs if p not in picked]
        rest.sort(key=lambda p: p["ts"])
        need = n - len(picked)
        if rest and need > 0:
            step = max(1, len(rest) // need)
            picked.extend(rest[::step][:need])
    picked.sort(key=lambda p: p["ts"])
    rnd.shuffle(picked)
    return [{"context": p["context"], "reply": p["reply"]} for p in picked]


def sample_stats(messages: List, me: str, other: str = None) -> Dict:
    """统计采样池（纯 Python，零 token）。

    为什么要有这个：卡片里要记下"样本来自多大的池子、能配出多少对"，
    否则无法判断一份画像是基于 30 条还是 3 万条得出的，也就无法判断可信度。
    判定口径与 `sample_pairs` **逐条一致**，保证 `可用配对` 与
    「实际抽到的配对数 ≤ 可用配对」自洽。
    """
    msgs = _prepare(messages)
    bs = _bursts(msgs)
    pairs = 0
    for i in range(len(bs) - 1):
        prev = bs[i]["sender"]
        if prev == me or bs[i + 1]["sender"] != me:
            continue
        if other is not None and prev != other:
            continue
        pairs += 1
    return {
        "消息总数": len(messages),
        "可用条目": len(msgs),
        "文本条数": sum(1 for m in msgs if m[3] == "文本"),
        "我发言段": sum(1 for b in bs if b["sender"] == me),
        "非我发言段": sum(1 for b in bs if b["sender"] != me),
        "可用配对": pairs,
    }


def sample_dialog(messages: List, n: int = 5, window: int = 14,
                  seed: int = 20261016) -> List[dict]:
    """抽取 n 段连续对话窗口，覆盖会话时间轴的首、中、尾。"""
    msgs = _prepare(messages)
    if len(msgs) < window:
        window = max(2, len(msgs))
    total = len(msgs)
    if total <= window:
        starts = [0]
    else:
        step = max(1, (total - window) // max(1, n - 1)) if n > 1 else 0
        starts = [min(i * step, total - window) for i in range(n)]
    out = []
    for s in starts:
        chunk = msgs[s:s + window]
        if not chunk:
            continue
        out.append({
            "start": chunk[0][0],
            "lines": ["%s: %s" % (sender, text) for _ts, sender, text, _tn in chunk],
        })
    return out


def relevant_pairs(messages: List, me: str, other: str, trigger_text: str,
                   n: int = 4, scan: int = 400) -> List[dict]:
    """找回**与当前触发消息话题相近**的真实配对，作为 P3 的 few-shot。

    **为什么需要**：P2 已经抽出了 24 组真实配对，但 P3 只喂规则、不喂范例，
    等于让模型凭空模仿。实测结果是大量空洞回应（`开始啥`/`说`/`想得美`）——
    形式对了，但没接住话。给出"你在类似场景下真实说过什么"是最有效的补法。

    检索方式：用触发消息里的 2-gram 中文片段做关键词，与历史配对的上下文
    算重叠度，取最高者；没有命中就回落到最近的几组。**不调模型，零 token。**
    """
    import collections
    trg = set(_keywords(trigger_text))
    if not trg:
        return []
    pairs = sample_pairs(messages, me, other, n=scan)
    if not pairs:
        return []
    scored = []
    for i, p in enumerate(pairs):
        ctx = " ".join(p.get("context") or [])
        rep = " ".join(p.get("reply") or [])
        kw = set(_keywords(ctx))
        if not kw:
            continue
        overlap = len(trg & kw)
        if overlap:
            # 重叠优先；同分时取更近期的（下标靠后）
            scored.append((overlap, i, p))
    if not scored:
        return pairs[-n:]
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for _ov, _i, p in scored:
        if p not in out:
            out.append(p)
        if len(out) >= n:
            break
    return out


_STOP = {"什么", "怎么", "这个", "那个", "就是", "可以", "没有", "自己", "知道",
         "不是", "我们", "你们", "他们", "现在", "还是", "因为", "所以", "但是",
         "如果", "可能", "应该", "真的", "一个", "一下", "不了", "的话"}


_AT_RE = re.compile(r"@[^\s@，。！？、,.!?]{1,24}")


def _keywords(text: str) -> List[str]:
    """取 2-gram 中文片段作关键词，去掉常见虚词组合。

    **必须先剥掉 @ 提及**：实测群里每条被 @ 的消息都含 `@玩狗老师`，
    若不剥离，检索出的"同话题配对"全是被 @ 的消息，纯属虚假相似
    （实测 4 组检索结果全以 `@玩狗老师` 开头）。
    """
    s = _AT_RE.sub("", str(text or ""))
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", s)
    out = []
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        if g in _STOP:
            continue
        if all("\u4e00" <= ch <= "\u9fff" for ch in g) or g.isalnum():
            out.append(g)
    return out


def format_fewshot(pairs: List[dict], max_pairs: int = 4) -> str:
    """把配对渲染成提示词里的 few-shot 段。"""
    if not pairs:
        return ""
    lines = ["【你在类似场景下的真实回复 —— 只用来体会**你会说什么内容**，"
             "不要照抄字句】"]
    for i, p in enumerate(pairs[:max_pairs], 1):
        lines.append("场景%d" % i)
        for t in (p.get("context") or [])[-3:]:
            lines.append("  对方: %s" % t)
        for t in (p.get("reply") or [])[:4]:
            lines.append("  你:   %s" % t)
    return "\n".join(lines)
    """采样池规模，便于自检采样是否够用。other=None 表示任意非我发言人。"""
    bs = _bursts(_prepare(messages))
    pairs = sum(1 for i in range(len(bs) - 1)
                if bs[i]["sender"] != me and bs[i + 1]["sender"] == me
                and (other is None or bs[i]["sender"] == other))
    return {"burst段数": len(bs), "可用配对": pairs,
            "我发言条数": sum(1 for _t, s, _x, _n in _prepare(messages) if s == me)}
