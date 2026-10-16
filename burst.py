# -*- coding: utf-8 -*-
"""轮次（burst）聚合与发送时机决策。

**为什么需要这一层**：微信群里人是一串一串说话的，不是一问一答。实测王雨森
在同一分钟内连发 `@玩狗老师`、`切换之前说一声`、`Bonjour` 三条。若按"每条
消息触发一次回复"，机器人会回三次，既不像人也很吵。

所以对话的单位是**轮次**：

    多对一：对方（或几个人）连发 N 条  ->  我整合成**一次**回应
    一对多：对方 1 条                  ->  我连发 2~5 条

**轮次边界靠"静默窗口"自然切分**：对方停止发言 QUIET 秒后，把这段时间内所有
未回的消息视为一轮，交给闸门判断与生成。这同时解决了"轮询该设多久"的问题 ——
轮询只负责发现新消息，**发送时机由静默窗口决定**，与轮询间隔解耦。

另外还有两条边界：
  * 对方一直在说、超过 MAX_WAIT 秒仍未静默 -> 强制成轮（否则永远不回）
  * 自己发的**文本**消息 -> 清空待回队列（我已回过，之前的不必再回）。
    发的是分享/图片/引用等则**不清空** —— "发了张图 = 我已经回答"这个推断不成立，
    实测（翔铭 23:18）就是这样丢掉对方一条正经问题的。规则见 `discard_on`。
"""
import time
from typing import List, Optional

# 默认参数（可用 --quiet / --max-wait 覆盖）
QUIET_SECONDS = 6.0        # 对方静默这么久即认为一轮说完了
MAX_WAIT_SECONDS = 45.0    # 从本轮第一条算起，最多等这么久就强制成轮
MIN_BURST_MSGS = 1         # 一轮至少几条才算数


class Burst(object):
    """一轮待回的对方消息。"""

    __slots__ = ("messages", "started_at", "last_at", "senders")

    def __init__(self):
        self.messages: List = []
        self.started_at: float = 0.0     # 本轮第一条的到达时间
        self.last_at: float = 0.0        # 最后一条的到达时间
        self.senders: List[str] = []

    def __len__(self):
        return len(self.messages)

    def add(self, m):
        if not self.messages:
            self.started_at = time.time()
        self.messages.append(m)
        self.last_at = time.time()
        s = m.sender or "?"
        if s not in self.senders:
            self.senders.append(s)

    def clear(self):
        self.messages = []
        self.started_at = self.last_at = 0.0
        self.senders = []

    def to_dict(self):
        return {"count": len(self.messages), "senders": self.senders,
                "texts": [(m.text or "")[:80] for m in self.messages]}


class BurstTracker(object):
    """把新消息流聚合成轮次，并决定何时触发回复。"""

    def __init__(self, quiet: float = QUIET_SECONDS,
                 max_wait: float = MAX_WAIT_SECONDS,
                 min_msgs: int = MIN_BURST_MSGS,
                 discard_on=None,
                 verbose: bool = True):
        self.quiet = quiet
        self.max_wait = max_wait
        self.min_msgs = min_msgs
        # 本人发哪些类型的消息才算"我已经回过了"，从而丢弃待回队列。
        # 默认**只有纯文本**：实测（翔铭 23:18）用户手发一条引用/分享就把
        # 对方 23:17 的问题整条丢掉，"分享了张图 = 我已经回答"这个推断不成立。
        # 取值：类型名列表（"文本"/"引用"/…），或 ["*"] 表示任何本人发言都丢弃。
        self.discard_on = list(discard_on) if discard_on else ["文本"]
        self.verbose = verbose
        self.burst = Burst()
        self.generation = 0          # 每次成功成轮递增，便于幂等
        self.last_fired_gen = -1

    def _discards(self, m) -> bool:
        if "*" in self.discard_on:
            return True
        return (getattr(m, "type_name", "") or "") in self.discard_on

    # ---------------------------------------------------------------- 输入

    def feed(self, messages: List) -> dict:
        """喂入新消息（时间升序）。自己发的**文本**消息会清空待回队列。

        返回 {"mine": n, "others": n, "cleared": n}，便于调用方打日志 ——
        「收到新消息但仍成不了轮」几乎总是因为**那是本人自己发言**
        （实测踩过：测试时从自己账号发消息，工具正确地清空了队列，
        却被误以为"没反应"）。
        """
        stat = {"mine": 0, "others": 0, "cleared": 0, "kept": 0}
        for m in messages:
            if getattr(m, "is_mine", False):
                stat["mine"] += 1
                if not self._discards(m):
                    # 本人发的分享/图片等不算"已回答"，保留待回队列
                    stat["kept"] += 1
                    continue
                if self.burst.messages:
                    stat["cleared"] = len(self.burst.messages)
                    if self.verbose:
                        print("  [burst] 本人发言(%s) -> 丢弃 %d 条待回消息"
                              % (getattr(m, "type_name", "?"),
                                 stat["cleared"]), flush=True)
                self.burst.clear()
                continue
            self.burst.add(m)
            stat["others"] += 1
        return stat

    # ---------------------------------------------------------------- 判定

    def reason_to_wait(self, now: float = None) -> str:
        """返回还要等的原因，空串表示可以成轮了。"""
        now = now or time.time()
        b = self.burst
        if not b.messages:
            return "本无待回消息"
        if len(b.messages) < self.min_msgs:
            return "待回消息不足 %d 条" % self.min_msgs
        since_last = now - b.last_at
        elapsed = now - b.started_at
        if since_last >= self.quiet:
            return ""
        if elapsed >= self.max_wait:
            # 对方一直在说，不能无限等
            return ""
        return "对方仍在发言（静默 %.1f/%.1f 秒，本轮已 %.1f 秒）" % (
            since_last, self.quiet, elapsed)

    def ready(self, now: float = None) -> bool:
        return self.reason_to_wait(now) == ""

    def take(self) -> Burst:
        """取出当前轮次并清空（成轮）。"""
        b = self.burst
        self.burst = Burst()
        self.generation += 1
        self.last_fired_gen = self.generation
        return b

    # ---------------------------------------------------------------- 展示

    def describe(self, b: Optional[Burst] = None) -> str:
        b = b or self.burst
        who = "、".join(b.senders) or "?"
        span = 0.0
        if b.messages:
            span = (b.last_at - b.started_at)
        return "一轮 %d 条（%s，跨度 %.1f 秒）" % (len(b), who, span)
