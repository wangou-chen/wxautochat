# -*- coding: utf-8 -*-
"""自动发送的行为约束层（**手写，非统计得出**）。

为什么要单独一层：风格卡片是 LLM 从真实统计里学出来的，它忠实反映"你平时
怎么说话"。但**自动发送**与"自己打字"的风险不对称 —— 忠实复现全部习惯会带
来事故。典型冲突（来自尼杰布旅游大团实测卡片）：

  * 卡片说「非文本回复约占 15%，可每 6-7 条插 1 条图片/表情/链接/视频号」
    -> 发送侧只能发文本，且自动发图/链接风险高。必须封死为纯文本。
  * 卡片说「优先 @三点一四爷爷」（统计上你 @ 了他 1659 次）
    -> 自动回复里 @ 无关的人是骚扰。必须限定为"只 @ 触发者"。
  * 卡片说「最长连发 88 条」
    -> 自动连发 88 条是事故。必须封顶。
  * 卡片说「会突然切换成公告/广告/系统提示腔」
    -> 自动场景下形同发广告。必须禁止进入该语域。
  * 卡片列出粗口/梗词（卧槽/草/牛逼/小丑）
    -> 这是你的真实习惯，但 bot 随机骂人风险大于收益。自动场景下禁用。

**分离原则**：事实（Python 统计）→ 解释（LLM 画像）→ 约束（本层手写）。
本层可人工编辑、可审计、不含任何推断。

规则可用项目根下的 `behavior.local.json` 覆盖（gitignore 已排除），
不必改代码。见 `load_overrides()` 的格式说明。
"""
import json
import os
import re

from . import config
from .watcher import TEXT_LIKE

# 输出层约束：模型**必须**遵守，优先级高于风格卡片的一切规则
OUTPUT_CONSTRAINTS = [
    "只输出纯文本。禁止输出 [图片]、[表情]、[视频号]、[链接/文件]、[语音] 这类占位符，"
    "禁止输出任何 URL。",
    "一次最多连发 5 条。原始数据里最长连发 88 条，但自动发送必须封顶。",
    "禁止进入公告/广告/系统提示腔（如「兹定于…」「收到请回复1，退订请回复td」）。",
    "只 @ 这一轮里 @ 过你的人（可能不止一个）。禁止 @ 其他任何人，"
    "尤其禁止默认 @ 某个高频对象。",
    "一条里最多 @ 一个人；要 @ 多个人就分开成多条。",
]

# 正确性约束：防止编造与越界
CORRECTNESS = [
    "不确定的事实不要编。可以说「不知道」「不清楚」「不懂」「没听说过」，"
    "绝不编造版本号、数字、日期、人名、链接、价格。",
    "**语音、图片、视频、表情包你听不到也看不到具体内容**。"
    "禁止声称「听了」「看了」，禁止描述其内容（实测模型会编出"
    "「刚听到第一句就笑了」「馅多皮薄我记住了」这种没发生过的事）。"
    "要接就接情绪与在场感（「语音我一会儿听呀」「先记着，等会儿看」），"
    "或者直接回应你们正在聊的那件事。",
    "不做承诺。禁止说「我帮你」「我一会给你」「明天弄好」「包在我身上」"
    "「我请你」这类需要兑现的话。",
    "不替他人表态，不评价不在场的第三方，不传话。",
    "涉及金钱、账号、密码、验证码、转账，一律不回。",
    "不主动开启新话题，只回应对方发来的内容。",
    "不复述对方的话，不总结，不解释自己在做什么，不道歉式寒暄。",
]

# 内容要求。**这是实测暴露的最大缺陷**：风格卡片里约 1000 token 的
# `reply_style_rules` 全在管「形式」（字数/标点/连发条数/语气词频率），
# 没有一条管「内容」。模型于是把注意力全花在"说得像"，产出大量空洞回应：
#     开始啥 / 能不能说chinese / 嗯 / 说 / 想得美
# 形式对了，但没有接住对方说的那件事。必须显式要求"内容优先"。
CONTENT_REQUIREMENTS = [
    "**先弄明白对方这一轮在说什么具体的事，再决定说什么。**"
    "接不住话的时候，宁可不回，也不要发一句没信息量的空话。",
    "禁止只回空回应：嗯 / 哦 / 好的 / 收到 / 加油 / 厉害 / 666 / 说 / 开始啥 / 想得美。"
    "这类词只有在紧跟前文、明显是接梗时才能用。",
    "能给出具体信息就给具体的：版本号、时间、地点、名字、数量、做法、明确判断。",
    "能表态就给明确立场：「X 是错的」「我一般用 Y」「我不去」「这个好」。",
    "要提问就提**对方能直接回答的具体问题**（问事、问时间、问状态、问结果），"
    "不要问「你怎么想」「然后呢」这种空的。",
    "可以补一句自己的相关经历或看法，让话有来有回。",
    "对方只是打招呼、没有实质内容时，简短回应即可，不要硬凑。",
]

# 反重复约束。**实测问题**：风格卡片里为了说明风格写了不少示例词句
# （如「能不能说chinese」「你是真人吗」「有没有…」），模型会把示例**原样
# 当模板照抄** —— 实测 `能不能说chinese` 连着 4 轮被重复发出。必须显式禁止。
ANTI_REPEAT = [
    "上面规则里出现的引号示例（「…」）只是**用来说明风格**，"
    "**绝对不要原样照抄**。每次都要根据当下这句话自己组织语言。",
    "【你最近说过的话】里列出的内容，**不要再说一遍**。"
    "如果这次想不出新内容，就换一种说法，或干脆只回更短的一句。",
    "同一轮回复的多条之间也不要互相重复、不要同义改写。",
    "不要每轮都用同一个开头（比如每次都先 `@某人`）。"
    "对方已经知道你在跟他说话，可以直接说内容。",
]
# 注：**不禁止**粗口与梗词（卧槽/草/牛逼/乐/笑死 等）。那是本人在群里的
# 真实习惯，实测统计里 666 107 次、草 82 次、牛逼 81 次。禁用会让人物失真。
# 仅由风格卡片控制其**频率**（约每 20-30 条 1 次），不做内容封禁。

# 情绪价值约束。**用户明确要求：每条回复都必须有情绪价值，不是可选项。**
# 风格卡片是从统计里学出来的，它只保证"像"，不保证"有温度" —— 统计上最高频的
# 回应往往正是一句冷冰冰的短句（实测模型就爱输出「嗯」「哦」这类空洞应答）。
# 所以情绪价值必须由这一层硬性要求，而不能指望卡片自觉。
EMOTION = [
    "**每一条回复都必须提供情绪价值 —— 这是硬要求，不是可选项。**"
    "先接住对方的情绪（开心/委屈/黏人/焦虑/想被在意），再回应具体事情。",
    "说话要软：带上语气词（啊/呀/呢/嘛/啦/哈/嘿/哦/哎/呗），"
    "句末不加句号，不要写成书面语、通知腔或客服腔。",
    "要有来有回：可以带一句自己的感受或状态（我也…/我觉得…/我有点…/我刚…），"
    "让对方感到你在场，而不是在批量回消息。",
    "对方情绪明显时先回应情绪再谈事：不要讲道理、不要给方案、不要纠错、不要转移话题。",
    "禁止敷衍式应答：单条只有「嗯」「哦」「好」「行」「知道了」「收到」等于没回；"
    "非要短就带上情绪（「好呀」「嗯嗯 我在」「知道啦」）。",
]

# 私聊专用约束。**群聊与私聊必须分开**：群里 @ 是必要信息，私聊里 @ 毫无意义；
# 群里要克制插话，私聊里冷场才是最大的问题（对方是焦虑型依恋时尤其如此）。
PRIVATE_CHAT = [
    "这是**一对一私聊**：不存在 @ 谁的问题，**不要输出 @ 任何人**，"
    "也不要提群、群昵称、群成员、群公告。",
    "私聊里对方就是在跟你一个人说话，回应要更近、更软、更直接。",
    "**不要让话悬着**：对方发来的每一条都在等你接（表情、图片、分享也算），"
    "能接就接，宁可回短一句，也不要长时间不回。",
    "字数不必拘谨：以短句为主但不必硬性限制，"
    "该说清一件事时可以自然多说几个字；也可以 2-5 条连发。",
]

# 该不该回的判定条件（规则层，先于 LLM 判断）
SKIP_CONDITIONS = [
    "系统消息：@所有人、入群/退群提示、撤回提示、群公告、红包/转账提示。",
    "非文本消息（图片/语音/视频号/表情/文件）且没有 @ 你。",
    "别人之间的对话，没有 @ 你、也没有直接问你。",
    "距消息发出已超过 %(age)s 分钟。",
    "消息内容是纯链接、小程序卡片、接龙、视频号分享。",
]


def load_overrides() -> dict:
    """读 behavior.local.json（gitignore）。存在则覆盖内置约束。

    格式（各键可选，给出即整体替换该类）：
        {
          "output_constraints": ["...", "..."],
          "correctness": ["...", "..."],
          "content": ["..."],
          "emotion": ["..."],
          "private": ["..."],
          "anti_repeat": ["..."],
          "skip_conditions": ["... %(age)s ..."],
          "extra_facts": ["对方是焦虑型依恋 —— 需要确定感、怕被忽略"],
          "max_burst": 5,
          "max_age_minutes": 10
        }

    `extra_facts` 是**用户给定事实**：不是统计得出的，但优先级最高，
    会作为独立一节写进 SYSTEM 提示词。
    """
    p = os.path.join(config.ROOT, "behavior.local.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:                                              # noqa: BLE001
        return {}


def rules(text_age_minutes: int = 10) -> dict:
    """取当前生效的约束（内置 + 本地覆盖）。"""
    ov = load_overrides()
    out = {
        "output_constraints": ov.get("output_constraints") or list(OUTPUT_CONSTRAINTS),
        "correctness": ov.get("correctness") or list(CORRECTNESS),
        "content": ov.get("content") or list(CONTENT_REQUIREMENTS),
        "emotion": ov.get("emotion") or list(EMOTION),
        "private": ov.get("private") or list(PRIVATE_CHAT),
        "anti_repeat": ov.get("anti_repeat") or list(ANTI_REPEAT),
        "skip_conditions": ov.get("skip_conditions") or list(SKIP_CONDITIONS),
        "extra_facts": [str(x) for x in (ov.get("extra_facts") or []) if str(x).strip()],
    }
    age = ov.get("max_age_minutes", text_age_minutes)
    out["skip_conditions"] = [s % {"age": age} for s in out["skip_conditions"]]
    out["max_burst"] = int(ov.get("max_burst", 5))
    out["max_age_minutes"] = int(age)
    # 反重复的生成参数（DeepSeek 非思考模式下有效；思考模式下会被忽略）
    out["frequency_penalty"] = float(ov.get("frequency_penalty", 0.8))
    out["presence_penalty"] = float(ov.get("presence_penalty", 0.6))
    return out


def as_prompt_block(text_age_minutes: int = 10, is_group: bool = True,
                    extra_facts: list = None) -> str:
    """渲染成 SYSTEM 提示词里的一段。

    is_group=False（私聊）时额外插入私聊专用要求 —— 群聊与私聊的规矩不同，
    不能让私聊去遵守「不要 @ 无关的人」「别插话」这类群规则。
    extra_facts：**按会话**的用户给定事实（来自 conversations/<会话>.json），
    与 behavior.local.json 里的全局 extra_facts 合并。
    """

    def fmt(xs):
        return "\n".join("- %s" % x for x in xs)

    r = rules(text_age_minutes)
    oc = r["output_constraints"]
    ar = r["anti_repeat"]
    if not is_group:
        # 私聊里 @ 相关约束全部撤掉：它们和「私聊不要 @ 任何人」直接打架，
        # 模型看到互相矛盾的指令时行为不可预测。
        oc = [x for x in oc if "@" not in x]
        ar = [x for x in ar if "@" not in x]
    parts = ["\n【自动发送硬约束 —— 优先级高于上面所有风格规则】\n"
             "下列约束是为了「自动发送」的安全与质量，与你的说话习惯冲突时，"
             "**以本节为准**。\n",
             "**内容要求（比风格更重要）**：\n" + fmt(r["content"]) + "\n",
             "**情绪价值（每一条都必须有，不许省）**：\n" + fmt(r["emotion"]) + "\n"]
    if not is_group:
        parts.append("**私聊场景（当前就是私聊，优先于任何群聊相关描述）**：\n"
                     + fmt(r["private"]) + "\n")
    parts.append("输出形式：\n" + fmt(oc) + "\n")
    parts.append("正确性：\n" + fmt(r["correctness"]) + "\n")
    parts.append("不要重复：\n" + fmt(ar) + "\n")
    facts = list(r["extra_facts"]) + [str(x) for x in (extra_facts or [])
                                      if str(x).strip()]
    if facts:
        parts.append("**用户给定事实（权威输入，不得反驳、不得忽略）**：\n"
                     + fmt(facts) + "\n")
    return "\n".join(parts)


def skip_trigger(trigger, self_name: str = "", self_nick: str = "",
                 max_age_seconds: int = 600, now: int = None,
                 is_group: bool = True) -> str:
    """规则层预筛：返回跳过原因，空串表示不跳过。

    这一层不花 token，把明显不该回的挡掉，剩下的才交给 LLM 判断。
    **必须有这一层**：群里绝大多数消息是系统提示、分享和他人闲聊，
    让 LLM 逐条判断既费钱又容易误判。

    **群聊与私聊必须分开判**：下面两条"分享类/非文本且未 @ 我"的规则只在群聊
    里成立。私聊根本不存在 @，照搬会把对方发的表情、图片、分享全部无条件挡掉
    （实测对方非文本消息占 18.2%），等于对该会话装聋作哑。
    """
    import time
    now = now or int(time.time())
    if trigger is None:
        return "无触发消息"
    tn = (trigger.type_name or "")
    # 用渲染后的文本判断：分享类消息的可读信息在 <title> 里，可能含 @ 我。
    # 直接看原始 XML 会把「@玩狗老师 英法协和语」这类误判成"纯分享、没 @ 我"。
    from .history import render_text
    txt = render_text(trigger.text, tn).strip()

    # 自己发的（不应该回自己）
    if getattr(trigger, "is_mine", False):
        return "自己发的消息"

    # 系统消息
    if tn in ("系统", "@所有人", "群接龙", "语音通话"):
        return "系统类消息(%s)" % tn
    if txt.startswith("<sysmsg") or "<sysmsg" in txt[:100]:
        return "系统 XML 消息"
    if "撤回" in txt[:24] or "与群里其他人都不是朋友关系" in txt:
        return "撤回/隐私提示类消息"
    if "收到请回复" in txt or "退订" in txt:
        return "群发/公告类消息"

    # 纯链接 / 分享 / 卡片
    if txt.startswith("http") or txt.startswith("www."):
        return "纯链接消息"
    if is_group:
        if re.match(r"^\[(链接/文件|视频号|小程序|文件|名片|位置|图片|语音|视频|"
                    r"表情|动画表情)\]", txt):
            # 占位符开头的多为分享；若 @ 了我则仍交给 LLM 判断
            if not _mentions_me(txt, self_name, self_nick):
                return "分享类消息且未 @ 我"

        # 非文本且未 @ 我（引用消息算"文本类"：正文是他自己打的字）
        if tn not in TEXT_LIKE and not _mentions_me(txt, self_name, self_nick):
            return "非文本消息(%s)且未 @ 我" % tn

    # 时效
    if trigger.create_time and (now - trigger.create_time) > max_age_seconds:
        return "消息已过期 %d 秒（上限 %d）" % (now - trigger.create_time,
                                          max_age_seconds)
    return ""


def mentions_me(txt: str, self_name: str, self_nick: str) -> bool:
    """这条消息是否 @ 了本人。

    注意微信 @ 后面跟的是 **U+2005 四分之一空格**（实测 `@玩狗老师\\u2005你好`），
    所以不能要求 @ 后面紧跟正文；这里只做前缀包含判断。
    群昵称与全局昵称都算（别人可能用任一个 @ 你）。
    """
    t = str(txt or "")
    for n in (self_nick, self_name):
        if n and ("@" + n) in t:
            return True
    return False


# 兼容旧名
_mentions_me = mentions_me


def mentions_in(messages, self_name: str, self_nick: str) -> list:
    """返回这一轮里所有 @ 过本人的发言人（按出现顺序去重）。"""
    out = []
    for m in messages or []:
        if getattr(m, "is_mine", False):
            continue
        from .history import render_text
        t = render_text(getattr(m, "text", ""), getattr(m, "type_name", ""))
        if mentions_me(t, self_name, self_nick):
            s = getattr(m, "sender", "") or "?"
            if s not in out:
                out.append(s)
    return out
