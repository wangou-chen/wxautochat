# -*- coding: utf-8 -*-
"""P3「拟」：读最近对话 → 用风格卡片让模型以本人身份生成回复。

**本模块不发送任何内容**，只产出文本并打印/落盘，供人工确认语气。
发送（UI 自动化）是独立的一步，故意分开。

关键设计：
  * 人格来自 `profiles/<会话>_self.json` 的 `reply_style_rules` / `taboos` /
    `expression_habits`。这些是 P2 用大模型基于真实统计产出的硬约束。
  * **关闭思考模式**：官方明确思考模式下 `temperature` 无效，而拟语气需要
    靠 temperature 控制随机性；且回复要低延迟。
  * SYSTEM 提示词由风格卡片拼成，同一会话内**逐字节稳定** → 命中缓存。
"""
import datetime
import json
import os
import re

from . import api, behavior, config, sampling

REPLY_DIR = os.path.join(config.ROOT, "replies")

SYSTEM_HEAD = """你在扮演一个真实的人，用他自己的语气在微信上回消息。

铁律：
1. 你就是他本人，**不是助手**。不要自称 AI、不要解释、不要总结、不要征求意见、不要写"作为…"。
2. 直接输出要发出去的消息本身。多条就用换行分隔（他会一次连发几条）。
3. 不要编号、不要引号、不要 markdown、不要任何前后缀说明。
4. 不要编造事实：不知道的事就不提，或用他惯用的模糊说法带过。
5. 只输出消息内容，输出完立即结束。
6. **至少有一条消息要带实际内容**。绝对不能只发一个 @ 某人就结束 ——
   纯 @ 不构成回复。（实测模型会退化成只输出"@某某"，必须显式禁止。）
7. 不要复述对方的话，不要写成完整句子/书面语；以短句为主，但**字数不必僵化**，
   该说清一件事时可以自然多说几个字。
8. **每条回复都要给情绪价值**：说话软、带语气词、先接情绪再谈事。
"""

SYSTEM_TAIL = """
【他是谁】
{persona}

【他的表达习惯】
{habits}

【他常聊的话题（回复时优先往这些方向靠）】
{topics}

【情绪价值策略（每次回复都要落到实处）】
{emotion}

【必须遵守的硬约束（按此控制语气、标点、连发条数）】
{rules}

【绝对避免】
{taboos}
{roster}"""

ROSTER_TMPL = """
【本群的称呼规则 —— @ 人必须用这里给的名字】
{self_line}{rows}
注意：微信群里 @ 人写错名字对方收不到提醒。上面没列出的人不要 @。
"""


def load_card(name: str) -> dict:
    """按名字或路径加载风格卡片。name 可为文件名、去后缀名、或路径。"""
    cands = []
    if name:
        if os.path.isabs(name) or os.sep in name:
            cands.append(name)
        else:
            base = name[:-5] if name.endswith(".json") else name
            cands.append(os.path.join(config.ROOT, "profiles", base + ".json"))
            cands.append(os.path.join(config.ROOT, "profiles", base + "_self.json"))
            cands.append(os.path.join(config.ROOT, "profiles", name))
    for p in cands:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def card_dirs() -> list:
    """卡片查找目录，按优先级：项目根的 profiles/ → 包内 defaults/。"""
    return [os.path.join(config.ROOT, "profiles"),
            os.path.join(config.PKG_DIR, "defaults")]


def default_card_names(want_group: bool = None) -> list:
    """默认卡片文件名，按优先级。

    为什么要分私聊/群聊两份默认：两者的语域差别是实测出来的 ——
    私聊里是短句、软、连发 2~3 条；群里更短更冲、还要带 @ 与群昵称。
    用一张卡同时覆盖两边，必然有一边不像。
    """
    if want_group is True:
        return ["_default_group_self.json", "_default_self.json"]
    return ["_default_self.json", "_default_group_self.json"]


def pick_card(display: str = "", want_group: bool = None,
              verbose: bool = False) -> tuple:
    """挑一张本人风格卡片。返回 (card, path)，都没有则 ({}, "")。

    查找顺序（**前三级不依赖任何会话专属配置**，所以对任何人开箱即用）：

      1. `profiles/<会话>_self.json`            该会话的专属卡片（P2 生成）
      2. `profiles/_default_self.json`          你自己写的默认卡片
      3. `<包目录>/defaults/_default_self.json`  随仓库分发的出厂默认
      4. 兜底：同类型的任意 `*_self.json`

    **第 4 级只是兜底，会打警告**：早期版本把它当正常路径，结果是"没配置就静默
    借用别的会话的卡片"，语气与关系完全不符（实测会借到 `翔铭_self`）。
    有默认卡片之后不该再走到第 4 级。
    """
    import glob
    dirs = card_dirs()
    if display:
        # 卡片文件名与显示名**不一定逐字相同**：落盘时会把空格等字符换成下划线
        # （`小狗媳妇 ᝰꫛꫀꪝ` -> `小狗媳妇_ᝰꫛꫀꪝ_self.json`）。只按原始名找会静默
        # 命不中，然后悄悄退回默认卡 —— 实测踩过，所以两种写法都试。
        names = ["%s_self.json" % display,
                 "%s_self.json" % config.safe_name(display)]
        for name in names:
            for d in dirs:
                p = os.path.join(d, name)
                if os.path.exists(p):
                    return load_card(p), p
    for name in default_card_names(want_group):
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.exists(p):
                c = load_card(p)
                if c.get("llm_profile"):
                    return c, p
    best, bestp = {}, ""
    for d in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*_self.json"))):
            c = load_card(p)
            if not c.get("llm_profile"):
                continue
            is_group = bool((c.get("session") or {}).get("is_group"))
            if want_group is not None and is_group == want_group:
                best, bestp = c, p
                break
            if not best:
                best, bestp = c, p
        if best:
            break
    if bestp and verbose:
        print("[警告] 没有默认卡片，临时借用 %s —— 语气可能与该会话不符，"
              "建议写一份 defaults/_default_self.json 或跑一次 P2"
              % os.path.basename(bestp))
    return best, bestp


def build_roster_block(roster: dict, self_wxid: str = "",
                       contacts: dict = None) -> str:
    """把群昵称映射渲染成 SYSTEM 里的一段。

    **为什么必须显式给出**：微信群里 @ 人要用**群昵称**，联系人表里只有全局
    备注/昵称。实测本机账号在「尼杰布旅游大团」的群昵称是 `玩狗老师`、全局名
    是 `🎐`；翔铭在该群是 `便秘练腹肌`。不告诉模型就会出现 `@王雨森` 这种
    @ 不到人的输出（正确应为 `@惠新西街南口`）。

    这段只依赖群成员表，同一群内**逐字节稳定** → 不破坏缓存命中。
    """
    if not roster:
        return ""
    self_line = ""
    rows = []
    for wxid, info in sorted(roster.items(), key=lambda kv: kv[1].get("用于@") or ""):
        use = info.get("用于@") or ""
        if not use:
            continue
        if info.get("是我"):
            self_line = ("（你自己的群昵称是「%s」——别人这样 @ 你，"
                         "你不需要 @ 自己）\n" % use)
            continue
        nick = info.get("群昵称") or ""
        g = info.get("全局名") or ""
        if nick and nick != g:
            rows.append("  @%s   ← 群昵称（此人的全局名是「%s」，群里别用）"
                        % (use, g))
        elif nick:
            rows.append("  @%s   ← 群昵称" % use)
        else:
            rows.append("  @%s   ← 未设群昵称，用全局名" % use)
    if not rows and not self_line:
        return ""
    return ROSTER_TMPL.format(self_line=self_line,
                              rows="\n".join(rows) + "\n" if rows else "")


def build_system(card: dict, roster: dict = None, self_wxid: str = "",
                 max_age_minutes: int = 10, is_group: bool = True,
                 extra_facts: list = None,
                 behavior_overrides: dict = None) -> str:
    """由风格卡片 + 群成员表 + 手写行为约束拼出 SYSTEM 提示词。

    四段结构：
      1. 铁律（角色与输出格式）
      2. **卡片里学到的画像**：性格 / 表达习惯 / 常聊话题 / 情绪价值策略
         —— 早期只用表达习惯与硬约束，把 `topics`、`personality` 丢掉了，
            模型不知道"这人平时聊什么"，是回复空洞的成因之一。
            `register_shifts`（语域切换）已按用户要求**不再注入**：那是从统计里
            切出来的场景分类，实际会让回复变形、指令互相打架。
      3. 风格硬约束与禁忌
      4. **手写行为约束**（内容要求 + 情绪价值 + 私聊/群聊分派 + 安全 + 反重复）
         —— behavior.py，优先级最高

    同一卡片与同一会话下**逐字节稳定**，便于命中 DeepSeek 的上下文缓存。
    """
    p = card.get("llm_profile") or {}

    def fmt(xs, n=None):
        xs = list(xs or [])
        if n:
            xs = xs[:n]
        if not xs:
            return "- （无）"
        return "\n".join(
            "- %s" % re.sub(r"\s*[（(]依据[:：][^）)]*[）)]", "", str(x))
            for x in xs)

    def flat(v):
        """把卡片的字段渲染成一段文字：字符串原样用，列表逐条列。"""
        if isinstance(v, str):
            return re.sub(r"\s*[（(]依据[:：][^）)]*[）)]", "", v).strip() or "- （无）"
        return fmt(v)

    persona = re.sub(r"\s*[（(]依据[:：][^）)]*[）)]", "",
                     str(p.get("personality") or "")).strip() or "（无）"
    base = SYSTEM_HEAD + SYSTEM_TAIL.format(
        persona=persona,
        habits=fmt(p.get("expression_habits"), 8),
        topics=fmt(p.get("topics"), 10),
        emotion=flat(p.get("emotional_value")),
        rules=fmt(p.get("reply_style_rules")),
        taboos=fmt(p.get("taboos")),
        roster=build_roster_block(roster or {}, self_wxid) if is_group else "")
    return base + behavior.as_prompt_block(max_age_minutes, is_group=is_group,
                                           extra_facts=extra_facts,
                                           overrides=behavior_overrides)


# ---------------------------------------------------------------- 该不该回

GATE_SYSTEM = """你在判断一条微信群消息**该不该由某个人来回复**。

只输出一个 JSON 对象：{"reply": true/false, "reason": "简短理由", "confidence": "high/medium/low"}

判断标准（按重要性）：
1. 明确 @ 了他，或直接点名问他 -> 应该回
2. 是向他提的问题、或明显在等他接话 -> 应该回
3. 他平时在这个群高频参与此类话题，且这条是抛给他的梗/邀请 -> 可以回
4. 别人之间的对话，他只是旁观者 -> **不该回**
5. 系统消息、公告、入群退群、撤回提示 -> **不该回**
6. 纯分享（图片/语音/视频号/链接）且没 @ 他 -> **不该回**
7. 话题已经过去或已经有人答了 -> **不该回**

宁可不回，也不要插话。群里绝大多数消息都不需要他回。
只输出 JSON，不要解释。"""

# 私聊判定与群聊**完全相反**：群聊里"克制插话"是对的，私聊里"已读不回"才是问题。
# 实测（小狗媳妇）：她把表情、图片、视频号分享发过来时，群聊那套标准会判"不该回"，
# 于是对方连着发三条也没人接 —— 这正是要修的。
GATE_SYSTEM_PRIVATE = """你在判断一条**一对一私聊**消息**该不该由某个人来回复**。

只输出一个 JSON 对象：{"reply": true/false, "reason": "简短理由", "confidence": "high/medium/low"}

判断标准：
1. 对方就是在跟他一个人说话 -> **默认应该回**。私聊里冷场才是问题。
2. 对方发来表情、图片、语音、视频号、链接分享 -> **也应该回**，
   那是在跟他互动，不是空气。
3. 只有这几种才不回：系统消息/撤回提示/转账提示、消息已过去很久（超过 10 分钟）、
   他刚刚已经回过而对方没有再说新东西。
4. 不要用「别人之间的对话」「群里插话」这类群聊标准来判断。
5. 拿不准时选择回。

只输出 JSON，不要解释。"""


def build_gate_prompt(context: list, self_name: str, self_nick: str,
                      is_group: bool, burst_start: int = -1) -> list:
    """构造判断"该不该回"的请求。判断对象是**整轮**消息，不是单条。"""
    if burst_start < 0:
        burst_start = 0
        for i in range(len(context) - 1, -1, -1):
            if getattr(context[i], "is_mine", False):
                burst_start = i + 1
                break
    lines = ["【他是谁】", "在本群里的昵称/称呼：%s" % (self_nick or self_name)]
    lines += ["", "【最近的对话】"]
    for i, m in enumerate(context[-16:]):
        who = "他本人" if m.is_mine else (m.sender or "?")
        body = m.text or ("[%s]" % m.type_name)
        if body.startswith("<"):
            body = "[%s]" % m.type_name
        lines.append("%s: %s" % (who, body.replace("\n", " ")[:120]))
    burst = context[burst_start:]
    if burst:
        senders = []
        for m in burst:
            s = m.sender or "?"
            if s not in senders:
                senders.append(s)
        lines += ["", "【需要判断的】对方（%s）最后连发了 %d 条消息。"
                  % ("、".join(senders), len(burst))]
    lines += ["", "这**一轮消息**该不该由他回复？输出 JSON。"]
    return [{"role": "system",
             "content": GATE_SYSTEM if is_group else GATE_SYSTEM_PRIVATE},
            {"role": "user", "content": "\n".join(lines)}]


def gate(context: list, self_name: str, self_nick: str, is_group: bool,
         cfg: dict = None, burst_start: int = -1, verbose: bool = False) -> dict:
    """LLM 判断该不该回。返回 {reply, reason, confidence, usage, cost_cny}。"""
    msgs = build_gate_prompt(context, self_name, self_nick, is_group,
                             burst_start=burst_start)
    text, usage, _ = api.chat(msgs, cfg=cfg, json_mode=True, thinking=False,
                             temperature=0.0, max_tokens=300, verbose=verbose)
    out = {"reply": False, "reason": "", "confidence": ""}
    try:
        out.update(json.loads(text))
    except Exception:                                             # noqa: BLE001
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                out.update(json.loads(m.group(0)))
            except Exception:                                     # noqa: BLE001
                out["reason"] = "判断结果无法解析: %s" % (text or "")[:120]
        else:
            out["reason"] = "判断结果无法解析: %s" % (text or "")[:120]
    out["usage"] = usage
    out["cost_cny"] = round(api.cost(usage, (cfg or api.load_api())["model"]), 6)
    return out


def _clean(text: str) -> list:
    """把模型输出切成消息列表，去掉编号/引号/markdown 噪声。"""
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        s = re.sub(r"^[-*•]\s+", "", s)
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        s = re.sub(r"^\[?\s*(我|🎐)\s*[:：]\s*", "", s)
        s = s.strip().strip('"').strip("“”").strip("'")
        if s:
            out.append(s)
    return out


def build_user(context: list, self_name: str, is_group: bool,
               mention: str = "", burst_start: int = -1,
               recent_mine: list = None, few_shot: list = None) -> str:
    """拼出 user 消息：最近对话 + ★标出待回的那一轮 + 我最近说过的话。

    **关键**：对话单位是「轮次」而不是单条消息。对方（或几个人）连发 N 条
    应当整合成一次回应（多对一），而不是逐条回 N 次；反过来一条消息也可以
    回成 2-5 条（一对多）。所以这里把待回的整轮用 ★ 标出来，并明确要求
    "当成一个整体回应"。

    `recent_mine` 是我最近发过的话，**显式列出来并禁止重复**。实测模型会把
    风格卡片里的示例词句当模板照抄（`能不能说chinese` 连着 4 轮被重复发出），
    只靠"不要重复"的空泛指令无效，必须把已说过的内容摊在它面前。

    burst_start: context 里待回轮次的起始下标（-1 表示"最后一条"）。
    """
    if burst_start < 0:
        # 未指定则从"我最后一条之后"开始，即整段尾巴都属于本轮
        burst_start = 0
        for i in range(len(context) - 1, -1, -1):
            if getattr(context[i], "is_mine", False):
                burst_start = i + 1
                break
    burst = context[burst_start:]

    lines = ["【最近的对话（按时间先后，「我」就是他本人）】"]
    for i, m in enumerate(context):
        who = "我" if m.is_mine else (m.sender or "?")
        body = m.text or ("[%s]" % m.type_name)
        if body.startswith("<") or body.startswith("<?xml"):
            body = "[%s]" % m.type_name
        body = body.replace("\n", " ")[:120]
        mark = "★" if i >= burst_start else " "
        lines.append("%s%s: %s" % (mark, who, body))

    lines.append("")
    if burst:
        senders = []
        for m in burst:
            s = m.sender or "?"
            if s not in senders:
                senders.append(s)
        lines.append("【需要你回复】上面标 ★ 的 %d 条是对方刚发来的（来自 %s）。"
                     % (len(burst), "、".join(senders) or "对方"))
        lines.append("★ 把这 %d 条**当成一个整体**来回应，不要逐条各回一次。"
                     % len(burst))
        if len(burst) > 1:
            lines.append("★ 它们可能是同一件事的补充、也可能是连续几个点，"
                         "先读懂再决定说什么；不必全部回应，挑该接的接。")

    if few_shot:
        lines.append("")
        lines.append(sampling.format_fewshot(few_shot))

    if recent_mine:
        lines.append("")
        lines.append("【你最近说过的话 —— **不要说这些，不要换个说法重复**】")
        for t in recent_mine:
            lines.append("  · %s" % str(t).replace("\n", " ")[:60])
        lines.append("（如果实在想不出新内容，就回得更短一句，"
                     "不要重复上面任何一条）")

    if mention:
        lines.append("")
        lines.append("【这些人 @ 了你：%s】" % mention)
        lines.append("回他们时要用**群昵称**（见上面「本群的称呼规则」），"
                     "不要用他们的全局备注名。")
        lines.append("不必每次都 @ 回去；@ 的时候一条只 @ 一个人。")
    if is_group:
        lines.append("这是群聊，注意群里的说话方式（比如 @ 某人是否单独成条）。")
    lines.append("请以「%s」的身份，按上面的硬约束回复。"
                 "回复可以是一条，也可以是 2-5 条连发（你平时习惯连发）。"
                 "**每次都要用不一样的措辞**。只输出消息内容。"
                 % (self_name or "我"))
    return "\n".join(lines)


def check_mentions(messages: list, valid: set) -> list:
    """检查草稿里的 @ 名字是否都在群成员表里。

    微信里 @ 错名字对方收不到提醒，且会显得很奇怪，所以这是一个硬校验。
    返回可疑项列表。
    """
    bad = []
    if not valid:
        return bad
    for s in messages:
        for m in re.finditer(r"@([^\s@，。！？、,.!?]{1,24})", s):
            name = m.group(1).strip()
            if not name:
                continue
            if name not in valid and not any(name.startswith(v)
                                            for v in valid if len(v) >= 2):
                bad.append({"名字": name, "消息": s[:60]})
    return bad


def generate(card: dict, context: list, self_name: str, is_group: bool,
             cfg: dict = None, temperature: float = 0.9,
             thinking: bool = False, max_tokens: int = 512,
             mention: str = "", roster: dict = None, self_wxid: str = "",
             valid_mentions: set = None, max_age_minutes: int = 10,
             burst_start: int = -1, recent_mine: list = None,
             few_shot: list = None, retry_on_dup: int = 2,
             max_burst: int = 5, extra_facts: list = None,
             behavior_overrides: dict = None,
             verbose: bool = True) -> dict:
    """生成一次轮次回复（可能 1 条，也可能多条连发）。

    反重复做了三层：
      1. 提示词里显式列出【你最近说过的话】并禁止重复
      2. `frequency_penalty` / `presence_penalty`（仅非思考模式有效）
      3. **生成后机械查重**：与最近说过的话完全一样就重试，仍重复则丢弃
    """
    sys_prompt = build_system(card, roster=roster, self_wxid=self_wxid,
                              max_age_minutes=max_age_minutes,
                              is_group=is_group, extra_facts=extra_facts,
                              behavior_overrides=behavior_overrides)
    r = behavior.rules(max_age_minutes)
    ban = {str(x).strip() for x in (recent_mine or []) if str(x).strip()}
    attempts, last = 0, None
    while attempts <= max(0, retry_on_dup):
        attempts += 1
        user_prompt = build_user(context, self_name, is_group, mention,
                                 burst_start=burst_start,
                                 recent_mine=recent_mine, few_shot=few_shot)
        text, usage, _resp = api.chat(
            msgs := [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_prompt}],
            cfg=cfg, json_mode=False, thinking=thinking,
            temperature=None if thinking else temperature,
            max_tokens=max_tokens, verbose=verbose,
            frequency_penalty=None if thinking else r["frequency_penalty"],
            presence_penalty=None if thinking else r["presence_penalty"])
        clean = enforce_output_rules(_clean(text), max_burst=max_burst)
        dup = [s for s in clean if s.strip() in ban]
        last = {"messages": clean, "raw": text, "usage": usage,
                "cost_cny": round(
                    api.cost(usage, (cfg or api.load_api())["model"]), 6),
                "system": sys_prompt, "user": user_prompt,
                "duplicates": dup, "attempts": attempts,
                "bad_mentions": check_mentions(clean, valid_mentions or set())}
        if not dup:
            return last
        if verbose:
            print("       [反重复] 第 %d 次生成与最近说过的话重复：%s -> 重试"
                  % (attempts, dup), flush=True)
    # 重试用尽仍有重复：丢掉重复的那些条；若全被丢掉则整体弃用
    keep = [s for s in last["messages"] if s.strip() not in ban]
    if verbose and last["duplicates"]:
        print("       [反重复] 重试用尽，丢弃重复条 %d 条，保留 %d 条"
              % (len(last["duplicates"]), len(keep)), flush=True)
    last["messages"] = keep
    last["dropped_duplicates"] = last.pop("duplicates", [])
    return last


# 代码级兜底：模型可能不遵守提示词，这些必须由程序强制
PLACEHOLDER_RE = re.compile(r"^\s*\[(图片|表情|视频号|链接/文件|语音|视频|位置|"
                            r"名片|文件|小程序|动画表情|系统)\]\s*$")
URL_RE = re.compile(r"https?://|www\.", re.I)
MAX_BURST = 5


def enforce_output_rules(messages: list, max_burst: int = MAX_BURST) -> list:
    """把模型输出裁剪到「只能发文本」的硬边界内。

    提示词层已经写了这些约束，但**不能只靠提示词** —— 一旦模型输出
    `[图片]`，发送侧会真的把方括号文字发出去。所以这里做机械过滤。
    max_burst 可按会话收紧（例如跟翔铭只发 3 条以内）。
    """
    out = []
    for s in messages:
        s = s.strip()
        if not s:
            continue
        if PLACEHOLDER_RE.match(s):          # 纯占位符 -> 丢弃
            continue
        s = URL_RE.sub("", s).strip()        # 链接 -> 去掉
        if not s:
            continue
        out.append(s)
        if len(out) >= max(1, int(max_burst)):
            break
    return out


def log(record: dict, out_dir: str = None) -> str:
    out_dir = out_dir or REPLY_DIR
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "drafts.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p


def make_record(chat: str, trigger, result: dict, card_path: str,
                sent: bool = False) -> dict:
    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "chat": chat,
        "trigger": {"sender": getattr(trigger, "sender", ""),
                    "text": getattr(trigger, "text", ""),
                    "time": getattr(trigger, "create_time", 0)} if trigger else None,
        "draft": result["messages"],
        "raw": result["raw"],
        "usage": result["usage"],
        "cost_cny": result["cost_cny"],
        "card": card_path,
        "sent": sent,
    }
