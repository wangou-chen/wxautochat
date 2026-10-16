# -*- coding: utf-8 -*-
"""导出聊天记录 + 纯 Python 统计分析（**不调用任何大模型，零 token**）。

与 P2 的关系：
  * P2 `profile` = Python 统计事实 + LLM 语义画像（花钱）
  * 本模块    = 只做 Python 统计事实（免费），并额外导出原始聊天记录

用途：想看"客观数字"而不想花钱时用；也是 P2 结论的**事实基线**——
凡模型给出的判断，都应能在这里找到对应数字。

产物：
  exports/<名>.json            结构化聊天记录（与 wxarchive out/single 同格式）
  exports/<名>.md              可读聊天记录
  exports/<名>_python分析.md   统计报告
"""
import datetime
import html as _html
import json
import os
import re
from typing import Dict, List

from . import config, stats

EXPORT_DIR = os.path.join(config.ROOT, "exports")

TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                      re.DOTALL)
NAME_MAX = 60


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name))[:NAME_MAX] or "chat"


def _ts(t, fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.datetime.fromtimestamp(t).strftime(fmt) if t else "-"


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else 0.0


# ---------------------------------------------------------------- 导出

def export_history(session, contacts: dict, out_dir: str = None,
                   verbose: bool = True) -> dict:
    """导出聊天记录为 JSON + Markdown（格式兼容 wxarchive 的 out/single）。"""
    out_dir = out_dir or EXPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    base = _safe(session.display)
    jp = os.path.join(out_dir, "%s.json" % base)
    mp = os.path.join(out_dir, "%s.md" % base)

    sess_obj = {
        "table": session.table,
        "sid": session.table[4:] if session.table.startswith("Msg_") else "",
        "username": session.username,
        "display": session.display,
        "count": len(session.messages),
        "exported": len(session.messages),
        "shards": [],
        "messages": [{
            "time": m.create_time, "type": m.type,
            "type_name": m.type_name, "sender_id": m.sender_id,
            "sender": m.sender, "text": m.text,
        } for m in session.messages],
    }
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_dir": "(wxautochat 实时快照)",
        "query": session.display,
        "sessions_total": 1,
        "messages_total": len(session.messages),
        "contacts": contacts or {},
        "sessions": [sess_obj],
    }
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    with open(mp, "w", encoding="utf-8") as f:
        f.write("# 聊天记录：%s\n\n" % session.display)
        f.write("- username：`%s`\n- 消息数：%d\n- 导出时间：%s\n\n"
                % (session.username, len(session.messages),
                   payload["generated_at"]))
        for m in session.messages:
            f.write("- `%s` **%s**：%s\n"
                    % (_ts(m.create_time), m.sender,
                       m.text or "[%s]" % m.type_name))
    if verbose:
        print("-> %s (%.1f MB)" % (jp, os.path.getsize(jp) / 1048576))
        print("-> %s" % mp)
    return {"json": jp, "md": mp, "payload": payload}


# ---------------------------------------------------------------- 报告

def _md_table(rows, header):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def _bar(n, total, width=24):
    if not total:
        return ""
    k = int(round(width * n / total))
    return "█" * k


def python_report(session, contacts: dict, who: str = "other",
                  out_dir: str = None, verbose: bool = True) -> str:
    """生成纯 Python 统计报告。who: other / self。"""
    out_dir = out_dir or EXPORT_DIR
    os.makedirs(out_dir, exist_ok=True)

    me = session.self_name() or "我"
    if who == "self":
        subject = me
    else:
        subject = session.other_name() or session.display
    sf = stats.speaker_facts(session, subject)
    cf = stats.session_facts(session, self_display=me)
    tm = sf["文本指标"]
    wm = sf["用词指标"]
    bm = sf["连发指标"]
    tf = sf["时段指标"]
    tp = sf["类型指标"]

    L = []
    A = L.append
    A("# %s · Python 统计分析（%s）" % (session.display,
                                      "本人" if who == "self" else "对方"))
    A("")
    A("> **本报告完全由 Python 统计生成，未调用任何大模型（零 token）。**  ")
    A("> 这些是可复现的**事实**；对性格/语气的**解释**需要另外跑 `profile`（会调用大模型）。")
    A("")
    A("- 生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("- 统计对象：**%s**" % subject)
    A("- 会话：%s（`%s`）%s" % (session.display, session.username,
                              "  [群聊]" if session.is_group else "  [私聊]"))
    A("- 消息总数：%d" % cf["总条数"])
    A("- 时间跨度：%s ~ %s" % (_ts(cf["首条时间"], "%Y-%m-%d"),
                             _ts(cf["末条时间"], "%Y-%m-%d")))
    A("- 统计对象发言：%d 条，其中纯文本 %d 条" % (sf["总条数"], sf["文本条数"]))
    A("")

    # ---- 双方对比
    A("## 一、发言量对比")
    A("")
    sp = cf["发言分布"]
    tot = sum(sp.values()) or 1
    rows = []
    for k, v in list(sp.items())[:8]:
        rows.append([k, v, "%.1f%%" % _pct(v, tot), _bar(v, tot)])
    A(_md_table(rows, ["发言人", "条数", "占比", ""]))
    A("")
    if not session.is_group:
        rl1 = cf.get("我回复延迟") or {}
        rl2 = cf.get("对方回复延迟") or {}
        if rl1.get("样本数") or rl2.get("样本数"):
            A("### 回复延迟（秒）")
            A("")
            A(_md_table([
                ["我", rl1.get("样本数", 0), rl1.get("中位秒"), rl1.get("P75秒"),
                 rl1.get("P90秒"), "%.1f%%" % (rl1.get("≤10秒占比") or 0)],
                ["对方", rl2.get("样本数", 0), rl2.get("中位秒"), rl2.get("P75秒"),
                 rl2.get("P90秒"), "%.1f%%" % (rl2.get("≤10秒占比") or 0)],
            ], ["谁", "样本", "中位", "P75", "P90", "≤10秒占比"]))
            A("")

    # ---- 长度
    A("## 二、文本长度")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A("| 平均 | **%s 字** |" % tm.get("平均长度"))
    A("| 中位 | %s 字 |" % tm.get("中位长度"))
    A("| P90 | %s 字 |" % tm.get("P90长度"))
    A("| 最长 | %s 字 |" % tm.get("最长"))
    A("")
    ld = tm.get("长度分布") or {}
    if ld:
        A(_md_table([[k, "%s%%" % v] for k, v in ld.items()],
                    ["区间", "占比"]))
        A("")

    # ---- 标点
    A("## 三、标点习惯")
    A("")
    pt = tm.get("标点") or {}
    A("| 指标 | 值 |")
    A("|---|---|")
    A("| 含标点的消息 | %s%% |" % pt.get("含标点占比"))
    A("| **句末无标点** | **%s%%** |" % pt.get("句末无标点占比"))
    A("| 句末问号 | %s 条 |" % pt.get("句末问号"))
    A("| 句末感叹号 | %s 条 |" % pt.get("句末感叹号"))
    A("| 句末句号 | %s 条 |" % pt.get("句末句号"))
    A("")

    # ---- 用词
    A("## 四、用词")
    A("")
    if wm.get("语气词"):
        A("### 语气词（按频次）")
        A("")
        A(_md_table([[k, v, _bar(v, max(wm["语气词"].values()))]
                     for k, v in list(wm["语气词"].items())[:14]],
                    ["词", "次数", ""]))
        A("")
    for key, title in (("缓冲语", "缓冲/模糊表达"), ("网络语", "网络语与梗词")):
        if wm.get(key):
            A("### %s" % title)
            A("")
            A(_md_table([[k, v] for k, v in list(wm[key].items())[:12]],
                        ["词", "出现条数"]))
            A("")
    if wm.get("附和条数"):
        A("- 附和类回复：%d 条（占 %s%%）" % (wm["附和条数"], wm.get("附和占比")))
    if wm.get("含问号占比") is not None:
        A("- 含问号的消息：%s%%" % wm.get("含问号占比"))
    if wm.get("缓冲语条数占比") is not None:
        A("- 含缓冲语的消息：%s%%" % wm.get("缓冲语条数占比"))
    A("")

    # ---- 高频整句 / 片段
    if wm.get("整句高频 (≥3次)"):
        A("## 五、高频整句（口头禅候选，≥3 次）")
        A("")
        A(_md_table([[repr(k), v] for k, v in wm["整句高频 (≥3次)"][:20]],
                    ["整句", "次数"]))
        A("")
    if wm.get("高频2-3字片段 (≥8次)"):
        A("## 六、高频 2-3 字片段（≥8 次）")
        A("")
        A(_md_table([[k, v] for k, v in wm["高频2-3字片段 (≥8次)"][:24]],
                    ["片段", "次数"]))
        A("")

    # ---- 连发
    A("## 七、连发节奏")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A("| 连发段数 | %s |" % bm.get("连发段数"))
    A("| 平均每段条数 | **%s** |" % bm.get("平均连发条数"))
    A("| 最长连发 | %s 条 |" % bm.get("最长连发"))
    A("| 段长 ≥3 条的比例 | %s%% |" % bm.get("连发≥3比例"))
    A("")

    # ---- 时段
    A("## 八、活跃时段")
    A("")
    hours = tf.get("活跃小时") or {}
    mx = max(hours.values()) if hours else 0
    A("| 时刻 | 条数 | |")
    A("|---|---|---|")
    for h in range(24):
        c = hours.get(str(h), 0)
        if c:
            A("| %02d:00 | %d | %s |" % (h, c, _bar(c, mx)))
    A("")
    A("- 凌晨（0-4 点）占比：%s%%" % tf.get("凌晨占比"))
    A("")

    # ---- 表情 / 类型
    A("## 九、消息类型构成")
    A("")
    A(_md_table([[k, v, "%.1f%%" % _pct(v, sf["总条数"])]
                 for k, v in list((tp.get("类型分布") or {}).items())[:12]],
                ["类型", "条数", "占比"]))
    A("")
    A("- 非文本消息占比：**%s%%**" % tp.get("非文本占比"))
    A("")
    em = tm.get("emoji") or {}
    tg = tm.get("微信表情标签") or {}
    A("- 含 emoji：%s 条（%s%%）" % (em.get("含emoji条数"), em.get("占比")))
    A("- 微信表情标签：%s 条（%s%%）" % (tg.get("条数"), tg.get("占比")))
    A("- 含链接：%s 条" % tm.get("含链接条数"))
    A("")

    # ---- 说明
    A("---")
    A("")
    A("## 附：怎么看这份报告")
    A("")
    A("- 本节所有数字都可用 `python -m wxautochat analyze` 重新生成，完全可复现。")
    A("- 想要**对性格与语气的解释**（例如「用短句维持轻松感」），跑：")
    A("  `python -m wxautochat profile --chat \"%s\" --who %s`（会调用大模型）"
      % (session.display, who))
    A("- P2 生成的风格卡片里，`python_facts` 字段就是本报告的机器可读版本。")

    base = _safe("%s_%s_python分析" % (session.display,
                                      "本人" if who == "self" else "对方"))
    p = os.path.join(out_dir, "%s.md" % base)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    if verbose:
        print("-> %s (%d KB)" % (p, os.path.getsize(p) // 1024))
    return p
