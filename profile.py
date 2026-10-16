# -*- coding: utf-8 -*-
"""P2：生成「风格卡片」= 确定性统计 + LLM 语义画像。

两条腿缺一不可：
  * `python_facts` —— 纯 Python 算出的**事实**（可复现、可 diff、零 token）
  * `llm_profile`  —— 模型给出的**解释**（性格、习惯、语域、禁忌）
  * `reply_style_rules` —— 给 P3 直接消费的可执行约束

防编造机制：提示词要求模型对每条结论标注数字依据，无法支撑的必须自己
列进 `unsupported_inferences`。事后 `verify()` 会交叉核对这些依据是否真的
出现在事实里，核对结果写入卡片，供人工复核。

缓存策略（直接影响成本，¥0.02 vs ¥1 差 50 倍）：
  * SYSTEM 提示词是**常量**，放在 messages 最前，跨会话/跨次运行都能命中缓存
  * 变化的内容（本次会话的事实与样本）放最后
  * 因此**不要**在 SYSTEM 里插入会话名、时间戳等易变内容
"""
import datetime
import json
import os
import re

from . import api, config, history, sampling, sessions, stats

PROFILE_DIR = os.path.join(config.ROOT, "profiles")

# ---------------------------------------------------------------- 提示词
# 故意做成模块级常量：保持逐字节稳定，才能命中 DeepSeek 的上下文缓存。
SYSTEM_PROMPT = """你是一位语言风格分析师。我会给你某个人的微信聊天统计事实与真实对话样本，\
你要输出一份用于「模仿这个人说话」的结构化画像。

严格要求：
1. 只输出一个 JSON 对象，不要任何解释文字、不要 Markdown 代码块。
2. 所有结论必须能从我给出的「统计事实」「用户给定事实」或「对话样本」中找到依据。\
每条结论末尾用括号标注依据，格式 (依据: 关键词或数字)。
3. 若某条判断是你的推测、缺乏数据支撑，必须写进 unsupported_inferences，\
不要混在其它字段里冒充事实。
4. reply_style_rules 必须给出**可直接执行的生成约束**，每条都要带具体数字或明确规则。\
但**不要写僵硬的字数上限**：只描述倾向（如「以 3-8 字短句为主」「偶尔会连发长段」），\
不要写成「每条不得超过 N 字」这种机械限制 —— 硬性限字会让回复变得干瘪。
5. **emotional_value 是最重要的字段之一，必须有实质内容，不许写空话。**\
要给出「怎么让对话有情绪价值」的可执行做法：什么时候先接情绪再谈事、\
怎么给对方被在意与被回应的感觉、说话要多软、哪些话绝对不能说。\
凡是「用户给定事实」里关于对方的心理特征，都必须在这里被落实成具体做法。
6. 不要输出对方（对话中的另一个人）的性格分析，只分析我指定的目标人物；\
但「用户给定事实」中关于对方的信息是**权威输入**，必须采纳，不得反驳。
7. 中文作答。

输出 JSON 结构（字段名必须完全一致）：
{
  "personality": "性格画像，2-4 句",
  "expression_habits": ["表达习惯，每条一句，带依据"],
  "emotional_tone": "情绪基调，1-2 句",
  "topics": ["常聊话题，按频率降序"],
  "emotional_value": "情绪价值策略，3-5 句：对话中怎么让对方感到被在意、被回应、被安抚；\
具体到「什么时候必须先接情绪」「怎么给确定感」「绝对不能说哪些话」",
  "taboos": ["模仿时必须避免的事（他不会说的话、不会用的标点等），带依据"],
  "reply_style_rules": ["给生成模型的硬约束，必须有具体数字或明确规则；但不要僵硬的字数上限"],
  "few_shot_reasons": ["从样本里挑 3-5 组最能代表其风格的「对方说→我回」配对，说明为什么典型"],
  "unsupported_inferences": ["缺乏数据支撑的推测，可能为空数组"],
  "confidence": "high | medium | low"
}"""


def _msg_block_facts(facts: dict) -> str:
    return "【统计事实】\n" + json.dumps(facts, ensure_ascii=False, indent=1)


def _msg_block_samples(pairs, dialogs) -> str:
    parts = []
    if pairs:
        parts.append("【真实对话配对样本（对方 → 目标人物）】")
        for i, p in enumerate(pairs, 1):
            parts.append("样本%d" % i)
            for t in p.get("context", []):
                parts.append("  对方: %s" % t)
            for t in p.get("reply", []):
                parts.append("  目标: %s" % t)
    if dialogs:
        parts.append("")
        parts.append("【连续对话窗口（用于感受语域与话题）】")
        for i, d in enumerate(dialogs, 1):
            parts.append("窗口%d" % i)
            for ln in d.get("lines", []):
                parts.append("  " + ln)
    return "\n".join(parts)


# ---------------------------------------------------------------- 事实压缩

def compact_facts(f: dict, max_hours: int = 8, max_months: int = 12) -> dict:
    """压缩事实体积，只保留有信息量的部分（省 token，也降低模型抓错重点的概率）。

    注意：`文本指标`/`用词指标`/`连发指标`/`时段指标`/`类型指标` 都嵌套在
    `对象事实` 之下，**不是顶层键**。早期版本按顶层取，导致压缩整段失效
    （表现为提示词里带一堆 `2时 0、3时 0` 的零值）。
    """
    f = json.loads(json.dumps(f, ensure_ascii=False))       # 深拷贝
    obj = f.get("对象事实") or {}

    tm = obj.get("时段指标") or {}
    hours = tm.get("活跃小时") or {}
    top = sorted(((int(k), v) for k, v in hours.items() if v),
                 key=lambda kv: -kv[1])[:max_hours]
    tm["活跃小时"] = {str(k): v for k, v in sorted(top)}
    tm.pop("月度分布", None)          # 月度趋势对拟语气价值低，去掉省 token

    wm = obj.get("用词指标") or {}
    for k in ("整句高频 (≥3次)", "高频2-3字片段 (≥8次)"):
        if isinstance(wm.get(k), list):
            wm[k] = wm[k][:15]
    if isinstance(wm.get("语气词"), dict):
        wm["语气词"] = {k: v for k, v in list(wm["语气词"].items())[:12] if v}
    for k in ("缓冲语", "网络语"):
        if isinstance(wm.get(k), dict):
            wm[k] = dict(list(wm[k].items())[:8])

    tf = obj.get("类型指标") or {}
    if isinstance(tf.get("类型分布"), dict):
        tf["类型分布"] = dict(list(tf["类型分布"].items())[:8])

    # 会话级：发言分布只留前 10
    if isinstance(f.get("发言分布"), dict):
        f["发言分布"] = dict(list(f["发言分布"].items())[:10])
    return f


# ---------------------------------------------------------------- 主流程

def build(target: dict, session, who: str = "other", verbose: bool = True,
          notes: list = None) -> dict:
    """组装风格卡片的确定性部分（不发请求）。

    who:
      "other" 分析对方（理解对话伙伴）
      "self"  分析本机账号本人（P3 模仿语气就靠这个）

    notes: 用户给定事实（权威输入，例如「对方是焦虑型依恋」）。这些**不是**统计
    得出的，但对「怎么说话才有情绪价值」起决定作用，所以单独成一节喂给模型，
    并进入 python_facts —— 这样事后 verify() 能把引用它的依据也算作命中。
    """
    # 身份判定必须用 is_mine（由 self_wxid 比对得出）。`Message.sender` 是本机账号
    # 自己的昵称（实测为 `🎐`），不是 "我"；靠名字比较会把分析对象搞成本人。
    me = session.self_name() or "我"
    sp = session.speakers()
    if who == "self":
        subject = me
        # 给自己做画像时，上下文可以是任意非我发言人（群聊亦然）
        counterpart = None
    else:
        subject = target["display"]
        if not session.is_group:
            subject = session.other_name() or target["display"]
        elif subject not in sp or subject == me:
            subject = next((k for k in sp if k != me), subject)
        counterpart = subject

    facts = {
        "会话": session.display,
        "是否群聊": session.is_group,
        "统计对象": subject,
        "本机账号显示名": me,
        "会话事实": stats.session_facts(session, self_display=me),
        "对象事实": stats.speaker_facts(session, subject),
    }
    if session.is_group:
        facts["群内其他发言人"] = dict(list(sp.items())[:8])
    if notes:
        facts["用户给定事实"] = [str(n) for n in notes if str(n).strip()]

    facts = compact_facts(facts)
    pairs = sampling.sample_pairs(session.messages, me, counterpart, n=24)
    dialogs = sampling.sample_dialog(session.messages, n=5)
    return {"who": who, "facts": facts, "pairs": pairs, "dialogs": dialogs,
            "self_person": me, "target_person": subject,
            "pool": sampling.sample_stats(session.messages, me, counterpart)}


def prompt_of(built: dict) -> list:
    """构造 messages。**顺序固定**：常量 SYSTEM 在前以命中缓存。"""
    if built["who"] == "self":
        ask = ("请分析【%s】这个人 —— 也就是**本机账号本人**（对话里的「我」）。"
               "这份画像将用于模仿他本人的说话方式，所以重点是"
               "「他在什么情境下怎么说话」，而不是对话伙伴的性格。"
               "注意：样本里的「对方」是不同的人，不要分析他们。"
               % built["target_person"])
    else:
        ask = ("请分析【%s】这个人 —— 也就是对话里的**对方**（不是本机账号）。"
               % built["target_person"])
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            _msg_block_facts(built["facts"]) + "\n\n"
            + _msg_block_samples(built["pairs"], built["dialogs"])
            + "\n\n" + ask + " 按要求输出 JSON。"},
    ]


def _norm(s) -> str:
    """归一化，用于依据比对：只保留数字、拉丁字母与汉字。

    为什么不能按字面比对：模型会把事实摘写成 `平均长度8.5`（facts JSON 里是
    `"平均长度": 8.5`）、`含问号占比3.82%`（facts 里是 `3.82`，没有百分号）、
    `样本14『奥』`（样本原文夹着 `目标:`）。按字面比会在实测里产生 34/36 的
    误报，把核对变成一个没用的噪音源。
    """
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(s)).lower()


def _signals(ev: str) -> list:
    """从一条依据里抽出可核对的"信号"：数字与长度≥2 的中文/字母词组。"""
    out = []
    for m in re.finditer(r"\d+(?:\.\d+)?", str(ev)):
        out.append(m.group(0))
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", str(ev)):
        out.append(m.group(0))
    for m in re.finditer(r"[a-zA-Z]{2,}", str(ev)):
        out.append(m.group(0))
    return out


JSON_PARSE_TRIES = 3        # JSON 语法失败时的最大尝试次数（每次约 ¥0.02）


def parse_llm_json(content: str):
    """尽力把模型输出解析成 dict。返回 (错误信息, dict 或 None)。

    为什么要容错：实测模型会给出**语义合法、语法非法**的 JSON —— 字符串里带
    未转义的引号或裸换行，报 `Expecting ',' delimiter`。一次就 raise 会让这
    张卡片白花钱（实测 2026-10-17 生成 𓁅 卡片时就这么失败过一次）。
    依次尝试：严格解析 → `strict=False`（允许裸控制字符，最常见）→
    截出最外层 `{...}` → 再去掉对象/数组尾部的多余逗号。
    """
    def brace(s):
        i, j = s.find("{"), s.rfind("}")
        return s[i:j + 1] if 0 <= i < j else s

    def no_trailing_comma(s):
        return re.sub(r",(\s*[}\]])", r"\1", s)

    cands, seen = [], set()
    for base in (content, brace(content)):
        for s in (base, no_trailing_comma(base)):
            if s and s not in seen:
                seen.add(s)
                cands.append(s)
    last = ""
    for s in cands:
        for kw in ({}, {"strict": False}):
            try:
                got = json.loads(s, **kw)
                if isinstance(got, dict):
                    return "", got
            except json.JSONDecodeError as e:
                last = str(e)
    return last or "无法解析", None


def verify(profile: dict, facts: dict, samples_text: str = "",
           threshold: float = 0.5) -> dict:
    """交叉核对模型声称的"(依据: X)"。

    做法：把依据拆成信号（数字 + 词组），逐个在「事实 + 样本」的归一化文本里查。
    命中率 ≥ threshold 判定为"有据"。**这是复核辅助，不是拦截器** —— 宁可把
    可疑项标出来让人工看，也不因为格式差异误报。
    """
    blob = _norm(json.dumps(facts, ensure_ascii=False) + "\n" + (samples_text or ""))
    claims = []
    for k in ("expression_habits", "emotional_value", "taboos",
              "reply_style_rules", "personality", "emotional_tone"):
        v = profile.get(k)
        items = v if isinstance(v, list) else ([v] if v else [])
        for line in items:
            for m in re.finditer(r"[（(]依据[:：]\s*([^）)]+)[）)]", str(line)):
                claims.append((k, m.group(1).strip()))

    checked, weak, unsupported = [], [], []
    for k, ev in claims:
        nums = re.findall(r"\d+(?:\.\d+)?", str(ev))
        if not nums:
            # 只写了统计项名（如"标点统计"），没有任何数字 —— 引用含糊，但不是编造。
            # 单列一档，免得和"数字对不上"混在一起淹掉真正的红旗。
            weak.append({"字段": k, "依据": ev, "命中率": 0.0,
                         "说明": "只引用统计项名，未给具体数字"})
            continue
        sig = _signals(ev)
        found = sum(1 for s in sig if _norm(s) in blob)
        ratio = found / max(1, len(sig))
        rec = {"字段": k, "依据": ev, "命中率": round(ratio, 2),
               "数字": nums}
        (checked if ratio >= threshold else unsupported).append(rec)
    return {"有依据条数": len(checked),
            "弱依据条数": len(weak),
            "依据未命中条数": len(unsupported),
            "依据总数": len(claims),
            "弱依据明细": weak[:12],
            "未命中明细": unsupported[:12],
            "模型自述推测": profile.get("unsupported_inferences") or []}


def run(snaps: dict, query: str, md5map: dict, contacts: dict,
        self_wxid: str = "", limit: int = 0, dry_run: bool = False,
        thinking: bool = True, effort: str = "high", who: str = "other",
        aliases: dict = None, max_tokens: int = None, notes: list = None,
        verbose: bool = True) -> list:
    """完整 P2：定位会话 → 读历史 → 统计 → 采样 → 调 LLM → 生成风格卡片。

    who: "other" 只分析对方；"self" 只分析本人；"both" 两个都做（返回 2 张卡）。
    返回卡片列表。
    """
    targets = history.resolve(snaps, md5map, contacts, query, self_wxid,
                              aliases=aliases)
    if not targets:
        raise ValueError("未找到匹配会话: %r" % query)
    target = targets[0]
    if verbose:
        print("会话: %s (%s)%s" % (target["display"], target["username"],
                                  "  [群聊]" if target["is_group"] else ""))
        if len(targets) > 1:
            print("  另有 %d 个候选: %s"
                  % (len(targets) - 1,
                     "、".join(t["display"] for t in targets[1:6])))

    session = history.read(snaps, target, contacts, self_wxid, limit=limit)
    if not session.messages:
        raise ValueError("会话无消息: %r" % query)
    if verbose:
        print("读取消息 %d 条，发言人 %d 个：%s"
              % (len(session.messages), len(session.speakers()),
                 "、".join(list(session.speakers())[:6])))

    whos = ["self", "other"] if who == "both" else [who]
    if verbose and notes:
        print("用户给定事实 %d 条：%s"
              % (len(notes), "；".join(str(n)[:40] for n in notes)))
    cards = []
    for w in whos:
        built = build(target, session, who=w, verbose=verbose, notes=notes)
        if verbose:
            if built["target_person"] == built["self_person"] and w == "other":
                print("[警告] 统计对象与本机账号同名，可能是身份判定异常")
            print("  [%s] 统计对象=%s；配对样本 %d 组，窗口 %d 段，配对池 %d"
                  % (w, built["target_person"], len(built["pairs"]),
                     len(built["dialogs"]), built["pool"]["可用配对"]))

        msgs = prompt_of(built)
        est_in = sum(api.est_tokens(m["content"]) for m in msgs)
        if verbose:
            print("       提示词 %d 字符 ≈ %d token"
                  % (sum(len(m["content"]) for m in msgs), est_in))

        card = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "who": w,
            "session": {"username": target["username"],
                        "display": target["display"],
                        "is_group": target["is_group"], "table": target["table"]},
            "target_person": built["target_person"],
            "self_person": built["self_person"],
            "message_count": len(session.messages),
            "python_facts": built["facts"],
            "sampling": {"pairs": len(built["pairs"]),
                         "dialogs": len(built["dialogs"]), "pool": built["pool"]},
        }
        if dry_run:
            card["dry_run"] = True
            card["prompt_preview"] = msgs[1]["content"][:2000]
            card["est_input_tokens"] = est_in
            cards.append(card)
            continue

        cfg = api.load_api()
        card["model"] = cfg["model"]
        if verbose:
            print("       调用 API：%s（thinking=%s, effort=%s）…"
                  % (cfg["model"], thinking, effort), flush=True)
        # JSON 解析失败就重试：实测模型会吐出**合法语义、非法语法**的内容
        # （字符串里有未转义的引号或裸换行 → `Expecting ',' delimiter`），
        # 一次就 raise 会让整张卡片白花钱。先做宽松修复，不行再要一次。
        prof, content, usage = None, "", {}
        for attempt in range(1, JSON_PARSE_TRIES + 1):
            content, usage, _raw = api.chat(
                msgs, cfg=cfg, json_mode=True, thinking=thinking,
                reasoning_effort=effort if thinking else None,
                max_tokens=max_tokens, verbose=verbose)
            card["usage"] = usage
            card["cost_cny"] = round(
                (card.get("cost_cny") or 0) + api.cost(usage, cfg["model"]), 4)

            # 截断必须显式报错，否则用户只会看到"JSON 解析失败"而无从下手。
            if usage.get("finish_reason") == "length":
                raise ValueError(
                    "输出被截断（finish_reason=length）：%s 思考 token 与 JSON 正文"
                    "共用 max_tokens=%s，预算不够。\n"
                    "  处理：加大 --max-tokens（当前模型最大输出 384K），"
                    "或降低 --effort（low/high/max），或用 --no-think 关闭思考。\n"
                    "  已消耗：输入 %s / 输出 %s token%s，约 ¥%s"
                    % (cfg["model"], max_tokens,
                       usage.get("prompt_tokens"), usage.get("completion_tokens"),
                       ("／思考 %s" % usage["reasoning_tokens"])
                       if usage.get("reasoning_tokens") else "",
                       card["cost_cny"]))

            err, prof = parse_llm_json(content)
            if prof is not None:
                break
            if verbose:
                print("       [JSON] 第 %d 次返回不可解析（%s），重试…"
                      % (attempt, err), flush=True)
        if prof is None:
            # 把最后一次原文留在卡片里，便于离线排查
            card["raw_content"] = content
            card["raw_content_len"] = len(content)
            raise ValueError(
                "模型连续 %d 次未返回可解析的 JSON（%s）。\n"
                "  最后一次内容长度 %d 字符，前 200 字：%s\n"
                "  原文已存到卡片的 raw_content 字段。"
                % (JSON_PARSE_TRIES, err, len(content),
                   content[:200].replace("\n", " ")))
        card["raw_content_len"] = len(content)
        card["llm_profile"] = prof
        card["verification"] = verify(
            prof, built["facts"],
            _msg_block_samples(built["pairs"], built["dialogs"]))
        if verbose:
            v = card["verification"]
            print("       校验：有据 %d / 弱依据 %d / **未命中 %d**（共 %d 条）"
                  % (v["有依据条数"], v.get("弱依据条数", 0),
                     v["依据未命中条数"], v.get("依据总数", 0)))
            print("       消耗：输入 %s / 输出 %s token，缓存命中 %s，约 ¥%.4f"
                  % (usage.get("prompt_tokens"), usage.get("completion_tokens"),
                     usage.get("prompt_cache_hit_tokens", 0), card["cost_cny"]))
        cards.append(card)
    return cards


# ---------------------------------------------------------------- 落盘

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name))[:60] or "profile"


def save(card: dict, out_dir: str = None) -> dict:
    out_dir = out_dir or PROFILE_DIR
    os.makedirs(out_dir, exist_ok=True)
    disp = card["session"]["display"] or card["session"]["username"]
    # 本人卡片加 _self 后缀，避免与对方同名（例如群聊里统计对象=群名）
    suffix = "_self" if card.get("who") == "self" else ""
    base = _safe("%s%s" % (disp, suffix))
    jp = os.path.join(out_dir, "%s.json" % base)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=1)

    mp = os.path.join(out_dir, "%s.md" % base)
    p = card.get("llm_profile") or {}
    f_ = card.get("python_facts") or {}
    tm = (f_.get("对象事实") or {}).get("文本指标") or {}
    role = "本人（本机账号）" if card.get("who") == "self" else "对方"
    lines = ["# %s · 风格卡片（%s）" % (disp, role), "",
             "- 生成时间：%s" % card["generated_at"],
             "- 统计对象：%s" % card["target_person"],
             "- 消息数：%d（会话共 %d 条）" % (
                 (f_.get("对象事实") or {}).get("总条数", 0), card["message_count"]),
             "- 模型：%s" % card.get("model", "(未调用)"),
             ""]
    if card.get("usage"):
        lines += ["- token：输入 %s / 输出 %s，缓存命中 %s，约 ¥%s"
                  % (card["usage"].get("prompt_tokens"),
                     card["usage"].get("completion_tokens"),
                     card["usage"].get("prompt_cache_hit_tokens", 0),
                     card.get("cost_cny")), ""]
    lines += ["## 一、确定事实（Python 统计，零 token）", "",
              "- 文本平均 **%s 字** / 中位 %s / P90 %s / 最长 %s"
              % (tm.get("平均长度"), tm.get("中位长度"), tm.get("P90长度"),
                 tm.get("最长"))]
    ld = tm.get("长度分布") or {}
    if ld:
        lines.append("- 长度分布：" + "，".join("%s %s%%" % (k, v)
                                          for k, v in ld.items()))
    pt = tm.get("标点") or {}
    if pt:
        lines.append("- 标点：含标点 %s%%，句末无标点 **%s%%**（问号 %s / 叹号 %s / 句号 %s）"
                     % (pt.get("含标点占比"), pt.get("句末无标点占比"),
                        pt.get("句末问号"), pt.get("句末感叹号"), pt.get("句末句号")))
    em = tm.get("emoji") or {}
    tg = tm.get("微信表情标签") or {}
    if em or tg:
        lines.append("- emoji %s 条（%s%%）；微信表情标签 %s 条（%s%%）"
                     % (em.get("含emoji条数", 0), em.get("占比", 0),
                        tg.get("条数", 0), tg.get("占比", 0)))
    bm = (f_.get("对象事实") or {}).get("连发指标") or {}
    if bm:
        lines.append("- 连发：平均 **%s 条/段**，最长 %s，≥3 条占 %s%%（共 %s 段）"
                     % (bm.get("平均连发条数"), bm.get("最长连发"),
                        bm.get("连发≥3比例"), bm.get("连发段数")))
    tpm = (f_.get("对象事实") or {}).get("类型指标") or {}
    if tpm.get("类型分布"):
        lines.append("- 类型分布：" + "，".join(
            "%s %s" % (k, v) for k, v in list(tpm["类型分布"].items())[:10]))
        lines.append("- 非文本占比 **%s%%**" % tpm.get("非文本占比"))
    wm = (f_.get("对象事实") or {}).get("用词指标") or {}
    if wm.get("语气词"):
        lines.append("- 语气词 top：" + "，".join(
            "%s %s" % (k, v) for k, v in list(wm["语气词"].items())[:10]))
    if wm.get("缓冲语"):
        lines.append("- 缓冲语：" + "，".join(
            "%s %s" % (k, v) for k, v in list(wm["缓冲语"].items())[:8])
            + "（占 %s%%）" % wm.get("缓冲语条数占比"))
    if wm.get("网络语"):
        lines.append("- 网络语：" + "，".join(
            "%s %s" % (k, v) for k, v in list(wm["网络语"].items())[:8]))
    tf = (f_.get("对象事实") or {}).get("时段指标") or {}
    if tf.get("活跃小时"):
        lines.append("- 活跃小时 top：" + "，".join(
            "%s时 %s" % (k, v) for k, v in list(tf["活跃小时"].items())[:6]))
    cf = f_.get("会话事实") or {}
    if cf.get("我回复延迟", {}).get("样本数"):
        rl = cf["我回复延迟"]
        lines.append("- 我回复延迟：中位 %s 秒 / P75 %s / P90 %s（≤10 秒占 %s%%）"
                     % (rl.get("中位秒"), rl.get("P75秒"), rl.get("P90秒"),
                        rl.get("≤10秒占比")))
    nf = f_.get("用户给定事实") or []
    if nf:
        lines.append("- **用户给定事实（人工指定，非统计）**：" + "；".join(nf))
    lines.append("")
    if p:
        lines += ["## 二、模型画像", "",
                  "**性格**：%s" % p.get("personality", ""), "",
                  "**情绪基调**：%s" % p.get("emotional_tone", ""), ""]
        for key, title in (("expression_habits", "表达习惯"),
                           ("emotional_value", "情绪价值策略（P3 必须落实）"),
                           ("taboos", "禁忌（模仿时避免）"),
                           ("reply_style_rules", "生成硬约束（P3 直接使用）"),
                           ("topics", "常聊话题"),
                           ("few_shot_reasons", "典型样本说明")):
            raw = p.get(key)
            # emotional_value 是字符串，其余是列表；直接迭代字符串会逐字拆开。
            vals = [raw] if isinstance(raw, str) and raw.strip() \
                else list(raw or [])
            if vals:
                lines.append("**%s**：" % title)
                for v in vals:
                    lines.append("- %s" % v)
                lines.append("")
        if p.get("unsupported_inferences"):
            lines += ["**模型自述的推测（无数据支撑）**："]
            lines += ["- %s" % v for v in p["unsupported_inferences"]]
            lines.append("")
    v = card.get("verification")
    if v:
        lines += ["## 三、依据核对", "",
                  "- 依据总数：%d 条" % v.get("依据总数", 0),
                  "- 有据（具体数字可回溯）：**%d 条**" % v["有依据条数"],
                  "- 弱依据（只提统计项名）：%d 条" % v.get("弱依据条数", 0),
                  "- **未命中（数字对不上，需复核）：%d 条**" % v["依据未命中条数"],
                  ""]
        for d in v.get("未命中明细") or []:
            lines.append("- [%s] 命中率 %s：%s"
                         % (d["字段"], d.get("命中率"), d["依据"]))
        if v.get("未命中明细"):
            lines.append("")
        for d in v.get("弱依据明细") or []:
            lines.append("- （弱）[%s] %s" % (d["字段"], d["依据"]))
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return {"json": jp, "md": mp}
