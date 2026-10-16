# -*- coding: utf-8 -*-
"""wxautochat 命令行入口。

    python -m wxautochat probe                环境自检
    python -m wxautochat watch                轮询新消息（P1）
    python -m wxautochat reset                清空游标
    python -m wxautochat api [--test]         API 配置自检
    python -m wxautochat profile --chat X     生成风格卡片（P2）
    python -m wxautochat reply --chat X       生成回复草稿，不发送（P3「拟」）
"""
import argparse
import atexit
import datetime
import json
import os
import sys
import time

from . import (__version__, api, behavior, burst, config, convo, grouproster,
               history, keys as keysmod, lock, sampling, sender, sessions,
               watcher as wmod)


def _ts(t):
    if not t:
        return "-"
    return datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")


def _build(args, verbose=True):
    """组装探测结果：db_dir / 密钥 / 快照 / 映射。"""
    db_dir = config.db_dir(getattr(args, "db_dir", None))
    if not db_dir:
        print("[错误] 未找到微信数据目录。可用 --db-dir、config.local.json 的 db_dir、"
              "或 $env:WXARCHIVE_DB_DIR 指定。", file=sys.stderr)
        return None
    kf = config.keys_file(getattr(args, "keys_file", None))
    keymap = keysmod.resolve_keys(db_dir, kf, verbose=verbose)
    if not keymap:
        print("[错误] 没有可用密钥。请先运行 wxarchive keys 生成 %s" % kf,
              file=sys.stderr)
        return None
    work = config.state_dir(getattr(args, "work", None))
    from .snapshot import Snapshot
    snaps = {}
    for rel, key in sorted(keymap.items()):
        name = os.path.basename(rel)
        if not name.startswith("message_"):
            continue
        src_dir = os.path.dirname(os.path.join(db_dir, rel))
        snaps[name] = Snapshot(key, src_dir, work, name, verbose=verbose)
    return {"db_dir": db_dir, "work": work, "snaps": snaps,
            "keys_file": kf, "keymap": keymap}


def cmd_probe(args):
    print("wxautochat %s" % __version__)
    print("项目根目录: %s" % config.ROOT)
    db_dir = config.db_dir(args.db_dir)
    print("数据目录  : %s" % (db_dir or "(未找到)"))
    if not db_dir:
        return 2
    kf = config.keys_file(args.keys_file)
    print("密钥缓存  : %s%s" % (kf, "" if os.path.exists(kf) else "  (不存在)"))
    keymap = keysmod.resolve_keys(db_dir, kf, verbose=False)
    print("匹配密钥  : %d 个库" % len(keymap))
    for rel in sorted(keymap):
        print("    %s" % rel)

    dec = config.decrypted_dir(args.decrypted)
    md5map = sessions.session_md5_map(dec) if os.path.isdir(dec) else {}
    cmap = sessions.contacts(dec) if os.path.isdir(dec) else {}
    self_id = sessions.self_username(db_dir, dec)
    print("已解密目录: %s%s" % (dec, "" if os.path.isdir(dec) else "  (不存在)"))
    print("会话映射  : %d 个 md5->username，联系人 %d 个" % (len(md5map), len(cmap)))
    print("本机 wxid : %s" % (self_id or "(未识别)"))
    print("工作目录  : %s" % config.state_dir(args.work))

    try:
        import Crypto                                       # noqa: F401
        print("pycryptodome: OK")
    except ImportError:
        print("pycryptodome: 缺失（必需）")
    for mod in ("zstandard", "zstd"):
        try:
            __import__(mod)
            print("%s: OK" % mod)
            break
        except ImportError:
            continue
    else:
        print("zstandard/zstd: 缺失（zstd 压缩消息将无法解压）")
    return 0


def _make_watcher(args, verbose=True):
    info = _build(args, verbose=verbose)
    if not info:
        return None, None
    dec = config.decrypted_dir(getattr(args, "decrypted", None))
    md5map = sessions.session_md5_map(dec) if os.path.isdir(dec) else {}
    cmap = sessions.contacts(dec) if os.path.isdir(dec) else {}
    self_id = sessions.self_username(info["db_dir"], dec)
    w = wmod.Watcher(info["work"], info["snaps"], cmap, md5map,
                     self_wxid=self_id, verbose=verbose)
    return w, info


def _print_msgs(msgs, only=None, show_mine=False):
    for m in msgs:
        if only and only not in (m.display or "") and only != m.username:
            continue
        if m.is_mine and not show_mine:
            continue
        tag = "[我]" if m.is_mine else (m.sender or "?")
        if not m.is_group:
            tag = "对方" if not m.is_mine else "我"
        body = m.text
        if body.startswith("<"):
            body = "[%s]" % m.type_name
        body = body.replace("\n", " ")[:120]
        print("%s  %-14s %-10s %s" % (_ts(m.create_time), (m.display or "")[:12],
                                      tag[:8], body), flush=True)


def cmd_watch(args):
    w, info = _make_watcher(args)
    if w is None:
        return 2
    # 单实例锁：watch 与 reply 都写同一个 work/_cursors.json，
    # 并发运行会互相覆盖（实测曾因 os.replace 冲突把循环打崩）。
    if not lock.acquire(info["work"], note="watch %s" % (args.chat or "全部"),
                        verbose=True):
        return 2
    # 兜底释放：`--once` 等提前 return 的路径不会走到显式 release，
    # 残留的锁会让 60 秒内的下一次启动被"已有实例在运行"挡住（实测踩过）。
    atexit.register(lock.release, info["work"])
    if args.rebuild:
        for snap in info["snaps"].values():
            snap.invalidate()
        print("已清空本地副本，将全量重建。", flush=True)

    # 已有游标条目 => 本次是"接着看"，应正常输出；无条目 => 首次建立基线，静默。
    # 注意判"有条目"而非"文件存在"：reset 会留下一个空的 {} 文件。
    _cp = os.path.join(info["work"], wmod.CURSOR_FILE)
    had_cursors = False
    if os.path.exists(_cp):
        try:
            with open(_cp, "r", encoding="utf-8-sig") as _f:
                had_cursors = bool(json.load(_f))
        except Exception:                                          # noqa: BLE001
            had_cursors = False
    rounds = 0
    while True:
        rounds += 1
        t0 = time.time()
        try:
            msgs = w.poll()
        except KeyboardInterrupt:
            print("\n已停止。", flush=True)
            lock.release(info["work"])
            return 0
        if msgs:
            _print_msgs(msgs, only=args.chat, show_mine=args.show_mine)
        elif not had_cursors:
            print("已建立基线（现有 %d 个会话表，不输出历史消息）。"
                  % len(w.cursors), flush=True)
            had_cursors = True
        lock.heartbeat(info["work"], note="watch")
        if args.once:
            lock.release(info["work"])
            return 0
        if args.verbose:
            print("  [轮 %d] %d 新消息，%.2fs" % (rounds, len(msgs), time.time() - t0),
                  flush=True)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n已停止。", flush=True)
            lock.release(info["work"])
            return 0


def cmd_reset(args):
    info = _build(args, verbose=False)
    if not info:
        return 2
    w = wmod.Watcher(info["work"], info["snaps"], {}, {})
    w.reset_cursors(args.chat or None)
    print("游标已重置（%s）" % (args.chat or "全部"))
    return 0


def cmd_api(args):
    """API 配置自检（不发起请求，除非 --test）。"""
    from . import api
    cfg = api.load_api()
    print("配置文件   : %s" % api.find_api_file())
    for p in api.candidate_files():
        print("   候选      : %s%s" % (p, "  ← 存在" if os.path.exists(p) else ""))
    print("key 来源   : %s" % api.source_of())
    print("api_key    : %s" % api.mask(cfg["api_key"]))
    print("model      : %s" % cfg["model"])
    print("base_url   : %s" % cfg["base_url"])
    print("价格档     : %s" % ("已知" if cfg["model"] in api.PRICES
                               else "未知（按 flash 估算）"))
    if not cfg["api_key"]:
        print()
        print("未配置。请任选其一写入一行：")
        for p in api.candidate_files():
            print("    api_key=sk-你的密钥      ->  %s" % p)
        return 2
    if args.test:
        print()
        print("发起最小测试请求…")
        try:
            content, usage, _ = api.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                cfg=cfg, thinking=False, max_tokens=20, verbose=True)
            print("响应: %r" % content.strip())
            print("usage: %s" % usage)
            print("估算费用: ¥%.6f" % api.cost(usage, cfg["model"]))
            return 0
        except Exception as e:                                     # noqa: BLE001
            print("[失败] %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return 1
    print()
    print("配置就绪。用 --test 做一次真实最小请求验证连通性。")
    return 0


def cmd_profile(args):
    """P2：生成风格卡片。"""
    from . import profile as prof
    info = _build(args, verbose=not args.quiet)
    if not info:
        return 2
    dec = config.decrypted_dir(getattr(args, "decrypted", None))
    if not dec or not os.path.isdir(dec):
        print("[错误] 未找到已解密目录（读会话名/联系人映射用）：%s" % dec,
              file=sys.stderr)
        return 2
    md5map = sessions.session_md5_map(dec)
    cmap = sessions.contacts(dec)
    aliases = sessions.contact_aliases(dec)
    self_id = sessions.self_username(info["db_dir"], dec)

    # 先同步一次快照，保证分析的是最新数据
    for name, snap in sorted(info["snaps"].items()):
        r = snap.sync()
        if not args.quiet and r.get("full_rebuild"):
            print("  [build] %s 已重建" % name)

    try:
        cards = prof.run(info["snaps"], args.chat, md5map, cmap,
                         self_wxid=self_id, limit=args.limit,
                         dry_run=args.dry_run, thinking=not args.no_think,
                         effort=args.effort, who=args.who, aliases=aliases,
                         max_tokens=args.max_tokens, notes=args.note,
                         verbose=not args.quiet)
    except ValueError as e:
        print("[错误] %s" % e, file=sys.stderr)
        return 1
    except Exception as e:                                         # noqa: BLE001
        print("[错误] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1

    for card in cards:
        paths = prof.save(card, args.out)
        print("-> %s" % paths["json"])
        print("-> %s" % paths["md"])
    return 0

def cmd_reply(args):
    """P3「拟」：按轮次生成回复草稿。**不发送**，只打印与落盘。

    对话单位是**轮次**而非单条消息：对方（或几个人）连发 N 条 -> 整合成一次
    回应（多对一）；对方一条 -> 可回 2~5 条（一对多）。轮次边界由"静默窗口"
    切分，见 burst.py。
    """
    from . import reply as rep
    info = _build(args, verbose=not args.quiet)
    if not info:
        return 2
    dec = config.decrypted_dir(getattr(args, "decrypted", None))
    if not dec or not os.path.isdir(dec):
        print("[错误] 未找到已解密目录：%s" % dec, file=sys.stderr)
        return 2
    md5map = sessions.session_md5_map(dec)
    cmap = sessions.contacts(dec)
    aliases = sessions.contact_aliases(dec)
    self_id = sessions.self_username(info["db_dir"], dec)

    targets = history.resolve(info["snaps"], md5map, cmap, args.chat, self_id,
                              aliases=aliases)
    if not targets:
        print("[错误] 未找到匹配会话: %r" % args.chat, file=sys.stderr)
        return 1
    target = targets[0]
    # 按会话的配置（命令行显式参数优先，其次配置文件，最后内置默认）
    ccfg = convo.apply(args, target)
    # --card 显式指定人格卡片；否则按会话名自动挑（同群类型优先）
    if args.card:
        card = rep.load_card(args.card)
        card_path = args.card
        if not card.get("llm_profile"):
            print("[错误] 卡片 %s 不存在或不含 llm_profile（可能是 --dry-run 产物）"
                  % args.card, file=sys.stderr)
            return 2
    else:
        card, card_path = rep.pick_card(target["display"],
                                        want_group=target["is_group"],
                                        verbose=not args.quiet)
        if not card or not card.get("llm_profile"):
            print("[错误] 没有可用的本人风格卡片。先运行：\n"
                  "  python -m wxautochat profile --chat \"%s\" --who self"
                  % target["display"], file=sys.stderr)
            return 2

    # 群成员表：@ 必须用群昵称，否则对方收不到提醒
    roster, valid_mentions, self_nick = {}, set(), ""
    if target["is_group"]:
        roster = grouproster.display_map(dec, target["username"], cmap,
                                        self_wxid=self_id, verbose=True)
        valid_mentions = grouproster.valid_mentions(dec, target["username"],
                                                   cmap, self_id)
        mine = [v for v in roster.values() if v.get("是我")]
        self_nick = mine[0].get("用于@") if mine else ""

    print("会话: %s (%s)%s" % (target["display"], target["username"],
                              "  [群聊]" if target["is_group"] else ""))
    print("人格卡片: %s" % card_path)
    if target["is_group"]:
        named = sum(1 for v in roster.values() if v.get("群昵称"))
        print("群成员表: %d 人，%d 人有群昵称；你的群昵称 = %s"
              % (len(roster), named, self_nick or "(未设)"))

    cfg = api.load_api()
    tracker = burst.BurstTracker(quiet=args.quiet_window,
                                 max_wait=args.max_wait,
                                 discard_on=ccfg.get("discard_pending_on"),
                                 verbose=not args.quiet)
    w = wmod.Watcher(info["work"], info["snaps"], cmap, md5map,
                     self_wxid=self_id, verbose=False)

    # 单实例锁：两个进程同时写 work/_cursors.json 会崩（实测 [WinError 32]）
    if not lock.acquire(info["work"], note="reply %s" % target["display"],
                        verbose=True):
        return 2
    # 兜底释放：`--once` / 单轮异常等提前 return 的路径不会走到显式 release，
    # 残留锁会让 60 秒内的下一次启动被"已有实例在运行"挡住（实测：连续两次
    # `reply --once` 第二次必被拒，而 --once 正是推荐的试语气方式）。
    atexit.register(lock.release, info["work"])

    # 首轮 poll 只建立游标基线，不产出历史消息
    w.poll()
    session = history.read(info["snaps"], target, cmap, self_id,
                           limit=args.history)
    self_name = session.self_name() or "我"
    print("本机显示名: %s" % self_name)
    print("轮询 %.1f 秒｜静默窗口 %.1f 秒（对方停下这么久即成一「轮」）"
          "｜最长等 %.1f 秒" % (args.interval, args.quiet_window, args.max_wait))
    print("连发上限 %d 条｜连发间隔 %s｜丢弃待回队列的本人消息类型 %s"
          % (args.max_burst or 5, args.send_gap,
             "、".join(ccfg.get("discard_pending_on") or ["文本"])))
    if ccfg.get("notes"):
        print("用户给定事实 %d 条（来自会话配置）" % len(ccfg["notes"]))
    if ccfg.get("behavior"):
        print("按会话行为覆盖 %d 段：%s"
              % (len(ccfg["behavior"]), "、".join(sorted(ccfg["behavior"]))))
    print("等待新消息…（Ctrl+C 停止）")

    while True:
        # STOP 是**总开关**，不只是"别发送"：早期只在 sender 里拦截，
        # 结果是循环照常轮询、照常调 API 生成草稿，钱一直花、一条也发不出去
        # （实测用户建了 STOP 后又被扣了两轮生成费 ¥0.0036，还以为是"坏了"）。
        # 现在在循环顶部就退出，并把锁释放掉，让下一次启动能起来。
        if sender.stopped():
            print("[停止] 检测到 STOP 文件，进程退出（删掉 %s 即可继续）"
                  % sender.STOP_FILE)
            lock.release(info["work"])
            return 0
        try:
            try:
                msgs = w.poll()
            except KeyboardInterrupt:
                print("\n已停止。")
                lock.release(info["work"])
                return 0
            lock.heartbeat(info["work"], note="reply")
            if msgs:
                mine = [m for m in msgs if m.table == target["table"]]
                if mine:
                    st = tracker.feed(mine)
                    if not args.quiet:
                        if st["others"]:
                            print("  [burst] +%d 条对方消息 -> %s"
                                  % (st["others"], tracker.describe()), flush=True)
                        if st["mine"] and not st["others"]:
                            print("  [burst] 本会话新消息 %d 条全部是本人发言"
                                  "（其中 %d 条不算'已回答'，待回队列未清空），"
                                  "不触发回复"
                                  % (st["mine"], st.get("kept", 0)), flush=True)

            if not tracker.ready():
                if args.verbose and tracker.burst.messages:
                    print("  [wait] %s" % tracker.reason_to_wait(), flush=True)
                if args.once and not tracker.burst.messages:
                    print("没有待回消息。")
                    return 0
                time.sleep(args.interval)
                continue

            b = tracker.take()
            session = history.read(info["snaps"], target, cmap, self_id,
                                   limit=max(args.context, len(b.messages) + 5))
            # 待回轮次在完整消息序列里的起点（靠 (表, local_id) 定位）
            burst_start = None
            for i, m in enumerate(session.messages):
                if m.table == b.messages[0].table \
                        and m.local_id == b.messages[0].local_id:
                    burst_start = i
                    break
            if burst_start is None:
                print("[警告] 无法在历史里定位该轮次，跳过")
                continue
            ctx_lo = max(0, burst_start - (args.context - 1))
            context = session.messages[ctx_lo:]
            bstart = len(context) - len(b.messages)

            print()
            print("=" * 66)
            print("成轮：%s" % tracker.describe(b))
            for m in b.messages:
                print("   %s %-12s %s"
                      % (datetime.datetime.fromtimestamp(m.create_time
                                                         ).strftime("%H:%M:%S"),
                         (m.sender or "")[:12],
                         (m.text or "[%s]" % m.type_name).replace("\n", " ")[:80]))
            # 注意：mentioners 必须**先算后用**。曾把它打印在赋值之前，
            # 触发 UnboundLocalError 直接把循环打崩（实测：一条「语音通话」消息触发）。
            mentioners = behavior.mentions_in(b.messages, self_name, self_nick)
            mention = "、".join(mentioners)
            if mentioners:
                print("   @ 了我：%s" % mention)
            print("-" * 66)

            # 第 1 道闸：规则预筛（零 token）。整轮里任一条该回即通过。
            skips = [behavior.skip_trigger(m, self_name, self_nick,
                                           is_group=target["is_group"])
                     for m in b.messages]
            if all(skips) and not mentioners:
                print("[规则层：不回复] %s" % skips[-1])
                if args.once:
                    return 0
                continue

            # 第 2 道闸：LLM 判断整轮该不该回。
            # **但 @ 过我的消息必须回**（用户明确要求），此时跳过 LLM 判断，
            # 既保证不漏回，也省掉一次 ¥0.0005 的调用。
            if mentioners:
                g = {"reply": True, "confidence": "forced",
                     "reason": "被 @（%s），按用户要求强制回复" % mention,
                     "cost_cny": 0.0}
                print("[该不该回] reply=True (强制)  理由：%s" % g["reason"])
            else:
                g = rep.gate(context, self_name, self_nick, target["is_group"],
                             cfg=cfg, burst_start=bstart, verbose=args.verbose)
                print("[该不该回] reply=%s confidence=%s  理由：%s  (¥%.6f)"
                      % (g.get("reply"), g.get("confidence"), g.get("reason"),
                         g.get("cost_cny")))
            if not g.get("reply"):
                if args.once:
                    return 0
                continue

            # 我最近说过的话（用于反重复）：从会话里取最近 N 条本人的发言
            recent_mine = []
            for m in reversed(session.messages):
                if m.is_mine and (m.type_name == "文本") and (m.text or "").strip():
                    t = m.text.strip()
                    if t not in recent_mine:
                        recent_mine.append(t)
                    if len(recent_mine) >= args.no_repeat:
                        break
            recent_mine.reverse()

            # 与当前话题相近的真实配对，作为 few-shot（体会"说什么内容"）
            few = []
            if args.fewshot:
                trig_text = " ".join((m.text or "") for m in b.messages)
                other_name = session.other_name() or target["display"]
                few = sampling.relevant_pairs(session.messages, self_name,
                                              other_name, trig_text,
                                              n=args.fewshot)
                if few and not args.quiet:
                    print("   few-shot: %d 组相近话题的真实配对" % len(few))

            try:
                r = rep.generate(card, context, self_name, target["is_group"],
                                 cfg=cfg, temperature=args.temperature,
                                 thinking=args.think, max_tokens=args.max_tokens,
                                 mention=mention, roster=roster, self_wxid=self_id,
                                 valid_mentions=valid_mentions, burst_start=bstart,
                                 recent_mine=recent_mine, few_shot=few,
                                 max_burst=args.max_burst or 5,
                                 extra_facts=ccfg.get("notes"),
                                 behavior_overrides=ccfg.get("behavior"),
                                 verbose=args.verbose or not args.quiet)
            except Exception as e:                                     # noqa: BLE001
                print("[错误] %s: %s" % (type(e).__name__, e), file=sys.stderr)
                if args.once:
                    return 1
                continue

            print("DeepSeek 返回（%d 条连发）:" % len(r["messages"]))
            for i, s in enumerate(r["messages"], 1):
                print("   %d| %s" % (i, s))
            if r.get("bad_mentions"):
                for x in r["bad_mentions"][:6]:
                    print("   [警告] @%s 不在群成员表里（在「%s」）"
                          % (x["名字"], x["消息"]))
            if not r["messages"]:
                print("   [警告] 输出被硬约束过滤后为空，不发送")
            u = r["usage"]
            print("-" * 66)
            print("token 输入 %s / 输出 %s / 缓存命中 %s，费用 ¥%.6f"
                  % (u.get("prompt_tokens"), u.get("completion_tokens"),
                     u.get("prompt_cache_hit_tokens", 0), r["cost_cny"]))
            rp = rep.log(rep.make_record(target["display"], b.messages[-1], r,
                                         card_path))
            print("已记录 -> %s" % rp)

            if not r["messages"]:
                if args.once:
                    return 0
                continue

            # ------------------------------------------------ 发送
            if not args.live:
                print("【未发送】加 --live 才会真发送（先建议用 --dry-send 看计划）")
                if args.once:
                    return 0
                continue

            snd = sender.Sender(target["display"], dry_run=args.dry_send,
                                verbose=not args.quiet,
                                allow_click_focus=not args.no_click,
                                fast=args.fast, verify=args.verify)
            try:
                ocr = snd.attach()
                if not snd.verify_conversation(ocr):
                    print("[发送中止] 当前打开的会话不是「%s」——请先在微信里切到该会话"
                          % target["display"])
                    if args.once:
                        return 1
                    continue
            except sender.SendError as e:
                print("[发送中止] %s" % e, file=sys.stderr)
                if args.once:
                    return 1
                continue

            print("-" * 66)
            res = snd.send_burst(r["messages"], gap=args.send_gap)
            if res["failed"]:
                print("[发送失败] %s" % res["failed"])
                print("  已发出 %d/%d 条" % (len(res["sent"]), len(r["messages"])))
            else:
                print("已发送 %d 条（dry-run=%s）" % (len(res["sent"]), args.dry_send))

            # 回读 DB 校验：确认真的发出去了（dry-run 跳过）
            if res["sent"] and not args.dry_send:
                ok, detail = _verify_sent(info, target, res["sent"], self_id, cmap)
                print("[回读校验] %s %s" % ("通过" if ok else "未通过", detail))
                snd.log[-1].update(verified=ok)
            _append_sent_log(target["display"], b.messages, r, res, card_path,
                             args.dry_send)
            if args.once:
                return 0


        except Exception as _e:                             # noqa: BLE001
            # 单轮异常不能打死整个循环 —— 实测被两处代码 bug 各崩一次。
            # 这里兜住所有非 KeyboardInterrupt 的异常，打印后继续下一轮。
            import traceback
            traceback.print_exc()
            print("[错误] 本轮处理异常已跳过：%s: %s"
                  % (type(_e).__name__, _e), file=sys.stderr)
            time.sleep(2.0)
            continue


def _verify_sent(info, target, sent, self_id, cmap, timeout: float = 10.0):
    """回读 DB 确认消息真的发出去了。

    **不依赖 OCR**：直接从快照读该会话最新若干条，确认刚发的文本确实入库。

    为什么用**子序列匹配**而不是"取末尾 N 条逐条 zip"：实测本人在同一窗口手打
    消息时 `is_mine` 同样为真，bot 的 3 条之间被插进 `[视频号] 这不是我说的`，
    末尾对齐后逐条比对全部错位 —— 3 条明明都已入库却报「未通过」（21:57:46
    实测）。子序列匹配只要求文本**按序出现**，中间插入多少人工消息都不影响。
    """
    texts = [t.strip() for t in sent]
    t0 = time.time()
    while True:
        for s in info["snaps"].values():
            try:
                s.sync()
            except Exception:                                      # noqa: BLE001
                pass
        sess = history.read(info["snaps"], target, cmap, self_id,
                            limit=max(24, 4 * len(texts) + 8))
        mine = [(m.text or "").strip() for m in sess.messages if m.is_mine]
        pos, missing = 0, []
        for t in texts:
            found = -1
            for i in range(pos, len(mine)):
                if mine[i] == t or sender.similar(t, mine[i], threshold=0.9):
                    found = i
                    break
            if found < 0:
                missing.append(t)
            else:
                pos = found + 1
        if not missing:
            return True, "已在数据库确认 %d 条" % len(texts)
        if time.time() - t0 >= timeout:
            return False, ("%.0f 秒内仍有 %d 条未在数据库确认：%s"
                           % (timeout, len(missing),
                              "、".join(m[:12] for m in missing[:3])))
        time.sleep(1.0)


def _append_sent_log(chat, burst_msgs, r, res, card_path, dry):
    import json as _json
    try:
        os.makedirs(os.path.join(config.ROOT, "replies"), exist_ok=True)
        p = os.path.join(config.ROOT, "replies", "sent.jsonl")
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "chat": chat, "dry_run": bool(dry), "card": card_path,
            "burst": [(m.sender, (m.text or "")[:120]) for m in burst_msgs],
            "draft": r["messages"], "sent": res["sent"],
            "failed": res["failed"], "complete": res["complete"],
            "cost_cny": r.get("cost_cny"),
        }
        with open(p, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                              # noqa: BLE001
        pass


def cmd_convo(args):
    """查看/生成「按会话的配置」。"""
    if args.list:
        fs = convo.files()
        print("配置目录: %s" % convo.DIR)
        for p in fs:
            print("  %s" % p)
        if not fs:
            print("  （无）用 `python -m wxautochat convo --chat \"会话名\" --write` 生成")
        print("默认配置（未配置的会话走这里）:")
        found = False
        for p in convo.default_files():
            if os.path.exists(p):
                print("  %s" % p)
                found = True
        if not found:
            print("  （无默认配置 —— 未配置的会话将不发送）")
        return 0
    if not args.chat:
        print("[错误] 需要 --chat（或用 --list 列出全部）", file=sys.stderr)
        return 2
    if args.write:
        p = convo.write_template(args.chat, force=args.force)
        print("已写入 -> %s" % p)
        print("改完直接 `python -m wxautochat reply --chat \"%s\"` 即可生效"
              % args.chat)
        return 0
    path, cfg = convo.find(display=args.chat)
    if not cfg:
        print("未找到 %r 的配置，也没有默认配置。生成模板：" % args.chat)
        print("  python -m wxautochat convo --chat \"%s\" --write" % args.chat)
        return 1
    if convo.is_default(path):
        print("该会话**没有专属配置**，当前生效的是默认配置。"
              "（改它会同时影响所有未配置的会话；只想改这个会话就先 --write 派生一份）")
    print("文件: %s" % path)
    print(json.dumps(cfg, ensure_ascii=False, indent=1))
    return 0


def cmd_nicks(args):
    """查看/核对群昵称映射（@ 人必须用群昵称）。"""
    info = _build(args, verbose=False)
    if not info:
        return 2
    dec = config.decrypted_dir(getattr(args, "decrypted", None))
    if not dec or not os.path.isdir(dec):
        print("[错误] 未找到已解密目录：%s" % dec, file=sys.stderr)
        return 2
    cmap = sessions.contacts(dec)
    md5map = sessions.session_md5_map(dec)
    aliases = sessions.contact_aliases(dec)
    self_id = sessions.self_username(info["db_dir"], dec)

    targets = history.resolve(info["snaps"], md5map, cmap, args.chat, self_id,
                              aliases=aliases)
    if not targets:
        print("[错误] 未找到匹配会话: %r" % args.chat, file=sys.stderr)
        return 1
    t = targets[0]
    print("会话: %s (%s)%s" % (t["display"], t["username"],
                              "  [群聊]" if t["is_group"] else ""))
    if not t["is_group"]:
        print("不是群聊，无需群昵称。")
        return 0

    raw = grouproster.nicks_for(dec, t["username"], verbose=True)
    ov = (grouproster.load_overrides().get(t["username"]) or {})
    if not raw:
        print()
        print("[警告] 未解析出任何群昵称。可能原因：")
        print("  1) 该群成员都没设群昵称（此时 @ 用全局名即可，属正常）")
        print("  2) 微信版本改动导致 ext_buffer 格式变化（需手动配置）")
        print("  3) 该群不在 chat_room 表里（从未在本机打开过）")
        print()
        print("手动兜底：编辑 %s，格式：" % grouproster.OVERRIDE_FILE)
        print('  {"%s": {"wxid_xxx": "群昵称"}}' % t["username"])
        return 0

    print("解析出 %d 个成员（手动覆盖 %d 条）" % (len(raw), len(ov)))
    print()
    print("  %-28s %-18s %-16s %s" % ("wxid", "群昵称", "全局名", "用于@"))
    for wxid, nick in sorted(raw.items(), key=lambda kv: kv[1] or ""):
        g = cmap.get(wxid, "?")
        use = nick or g
        mark = "  <- 本机" if wxid == self_id else ""
        src = "  [手动]" if wxid in ov else ""
        print("  %-28s %-18s %-16s %s%s%s"
              % (wxid, nick or "(空)", g[:16], use, mark, src))
    named = sum(1 for v in raw.values() if v)
    print()
    print("共 %d 人，%d 人有群昵称。用于@的合法名字 %d 个。"
          % (len(raw), named,
             len(grouproster.valid_mentions(dec, t["username"], cmap, self_id))))
    print("手动覆盖文件：%s%s"
          % (grouproster.OVERRIDE_FILE,
             "" if os.path.exists(grouproster.OVERRIDE_FILE) else "（不存在）"))
    return 0


def cmd_analyze(args):
    """导出聊天记录 + 纯 Python 统计报告（**零 token，不调用大模型**）。"""
    from . import analyze as ana
    info = _build(args, verbose=not args.quiet)
    if not info:
        return 2
    dec = config.decrypted_dir(getattr(args, "decrypted", None))
    if not dec or not os.path.isdir(dec):
        print("[错误] 未找到已解密目录：%s" % dec, file=sys.stderr)
        return 2
    md5map = sessions.session_md5_map(dec)
    cmap = sessions.contacts(dec)
    aliases = sessions.contact_aliases(dec)
    self_id = sessions.self_username(info["db_dir"], dec)

    for name, snap in sorted(info["snaps"].items()):
        r = snap.sync()
        if not args.quiet and r.get("full_rebuild"):
            print("  [build] %s 已重建" % name)

    targets = history.resolve(info["snaps"], md5map, cmap, args.chat, self_id,
                              aliases=aliases)
    if not targets:
        print("[错误] 未找到匹配会话: %r" % args.chat, file=sys.stderr)
        return 1
    target = targets[0]
    if len(targets) > 1 and not args.quiet:
        print("另有 %d 个候选：%s" % (len(targets) - 1,
                                    "、".join(t["display"] for t in targets[1:5])))
    session = history.read(info["snaps"], target, cmap, self_id,
                           limit=args.limit)
    if not session.messages:
        print("[错误] 会话无消息", file=sys.stderr)
        return 1

    print("会话: %s (%s)%s" % (target["display"], target["username"],
                              "  [群聊]" if target["is_group"] else "  [私聊]"))
    print("消息 %d 条%s" % (len(session.messages),
                          "（最近 %d 条）" % args.limit if args.limit else ""))
    print("纯 Python 统计，不调用大模型（零 token）")
    print()

    out_dir = args.out or ana.EXPORT_DIR
    if not args.no_history:
        ana.export_history(session, cmap, out_dir, verbose=not args.quiet)
    whos = ["self", "other"] if args.who == "both" else [args.who]
    for w in whos:
        ana.python_report(session, cmap, who=w, out_dir=out_dir,
                          verbose=not args.quiet)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="wxautochat",
                                description="微信自动回复工具（P1：实时读取）")
    p.add_argument("--version", action="version", version="wxautochat %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--db-dir", default=None, help="微信 db_storage 路径")
        sp.add_argument("--work", default=None, help="本地增量工作目录")
        sp.add_argument("--decrypted", default=None, help="已解密目录（读会话名映射）")
        sp.add_argument("--keys-file", default=None, help="密钥缓存 JSON")

    sp = sub.add_parser("probe", help="环境自检")
    common(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("watch", help="轮询新消息")
    common(sp)
    sp.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒（默认 3）")
    sp.add_argument("--once", action="store_true", help="只同步一次并退出")
    sp.add_argument("--chat", default="", help="只看显示名包含该串的会话")
    sp.add_argument("--show-mine", action="store_true", help="也显示自己发的消息")
    sp.add_argument("--rebuild", action="store_true", help="先丢弃本地副本强制重建")
    sp.add_argument("--verbose", action="store_true", help="打印每轮统计")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("reset", help="清空游标")
    common(sp)
    sp.add_argument("--chat", default="", help="只重置该会话的表游标")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("api", help="API 配置自检（P2/P3 共用 api.txt）")
    sp.add_argument("--test", action="store_true", help="发一次最小请求验证连通性")
    sp.set_defaults(func=cmd_api)

    sp = sub.add_parser("profile", help="P2：生成某人的风格卡片")
    common(sp)
    sp.add_argument("--chat", required=True, help="显示名 / username / Msg_表名")
    sp.add_argument("--who", default="other", choices=("self", "other", "both"),
                    help="分析谁：other=对方（默认），self=本人，both=两个都出")
    sp.add_argument("--limit", type=int, default=0, help="只分析最近 N 条（0=全部）")
    sp.add_argument("--dry-run", action="store_true",
                    help="只生成统计与提示词，不调用 API")
    sp.add_argument("--no-think", action="store_true",
                    help="关闭思考模式（更快更省，但画像深度下降）")
    sp.add_argument("--effort", default="high",
                    choices=("low", "high", "max"), help="思考强度（默认 high）")
    sp.add_argument("--max-tokens", type=int, default=None,
                    help="输出上限（含思考 token）。默认：思考 32000 / 非思考 8192")
    sp.add_argument("--note", action="append", default=None,
                    help="用户给定事实（权威输入，可重复），"
                         "例如 --note \"对方是焦虑型依恋，需要确定感和及时回应\"")
    sp.add_argument("--out", default=None, help="风格卡片输出目录")
    sp.add_argument("--quiet", action="store_true", help="安静模式")
    sp.set_defaults(func=cmd_profile)
    sp = sub.add_parser("reply", help="P3「拟」：生成回复草稿（不发送）")
    common(sp)
    sp.add_argument("--chat", required=True, help="显示名 / username / Msg_表名")
    sp.add_argument("--config", default=None,
                    help="按会话的配置文件（名字或路径），默认在 "
                         "<项目根>\\conversations\\<会话>.json 里按会话名自动找。"
                         "文件里的项只填命令行没显式给的参数")
    sp.add_argument("--card", default=None,
                    help="指定人格卡片（名字或路径）。不指定则按会话名自动挑；"
                         "跨群借用他人卡片时用这个，例如 --card 尼杰布旅游大团_self")
    sp.add_argument("--history", type=int, default=0,
                    help="先读入多少条历史（0=全部，仅用于定位触发点）")
    sp.add_argument("--context", type=int, default=20,
                    help="喂给模型的最近对话条数（默认 20）")
    sp.add_argument("--temperature", type=float, default=0.9,
                    help="随机性（默认 0.9；思考模式下无效）")
    sp.add_argument("--think", action="store_true",
                    help="启用思考模式（注意：会令 temperature 失效）")
    sp.add_argument("--max-tokens", type=int, default=512, help="输出上限（默认 512）")
    sp.add_argument("--once", action="store_true", help="只生成一次即退出")
    sp.add_argument("--interval", type=float, default=2.0,
                    help="数据库轮询间隔秒（默认 2）")
    sp.add_argument("--quiet-window", type=float, default=6.0,
                    help="静默窗口秒：对方停止发言这么久即聚成一「轮」并开始拟稿（默认 6）")
    sp.add_argument("--max-wait", type=float, default=45.0,
                    help="从本轮第一条算起最多等这么久就强制成轮（默认 45）")
    sp.add_argument("--no-repeat", type=int, default=12,
                    help="反重复：把最近 N 条本人发言列为禁用（默认 12）")
    sp.add_argument("--fewshot", type=int, default=4,
                    help="喂入 N 组同话题真实配对，让回复有实质内容（0=关闭，默认 4）")
    sp.add_argument("--verify", default="send", choices=("full", "send", "none"),
                    help="OCR 校验档位：full=粘贴后+回车前都校验（慢）；"
                         "send=只保留发送前会话名校验，正确性靠发送后回读 DB（默认，快）；"
                         "none=连会话名都不核（不推荐）")
    sp.add_argument("--show-raw", action="store_true", help="也打印模型原始返回")
    sp.add_argument("--live", action="store_true",
                    help="真的发送（默认只生成草稿，不发送）")
    sp.add_argument("--no-live", dest="no_live", action="store_true",
                    help="只生成草稿、不发送；用来覆盖会话配置里的 live=true")
    sp.add_argument("--dry-send", action="store_true",
                    help="配合 --live：走完发送流程但最后不按键，验证锚点与校验")
    sp.add_argument("--send-gap", type=convo.parse_gap, default=0.8,
                    help="连发多条之间的间隔秒（默认 0.8）。"
                         "可写成区间以像真人：--send-gap 3,8 每次随机取")
    sp.add_argument("--max-burst", type=int, default=None,
                    help="本轮最多连发几条（默认取硬约束的 5）")
    sp.add_argument("--no-click", action="store_true",
                    help="禁用「点击输入框」兜底（只靠激活窗口后的既有焦点）")
    sp.add_argument("--fast", action="store_true",
                    help="回车前不再抓图核会话名（省约 1.8 秒/条，仅保留前台检查）")
    sp.add_argument("--verbose", action="store_true", help="打印 API 调用细节")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("convo", help="查看/生成「按会话的配置」")
    common(sp)
    sp.add_argument("--chat", default=None, help="会话显示名 / 备注名 / wxid")
    sp.add_argument("--write", action="store_true", help="生成/补齐配置模板")
    sp.add_argument("--force", action="store_true", help="覆盖已有配置（默认只补齐缺失键）")
    sp.add_argument("--list", action="store_true", help="列出全部配置文件")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_convo)

    sp = sub.add_parser("nicks", help="查看群昵称映射（@ 人必须用群昵称）")
    common(sp)
    sp.add_argument("--chat", required=True, help="群名 / username")
    sp.set_defaults(func=cmd_nicks)

    sp = sub.add_parser("analyze", help="导出聊天记录 + 纯 Python 统计报告（零 token）")
    common(sp)
    sp.add_argument("--chat", required=True, help="显示名 / username / Msg_表名")
    sp.add_argument("--who", default="other", choices=("self", "other", "both"),
                    help="统计谁：other=对方（默认）/ self=本人 / both")
    sp.add_argument("--limit", type=int, default=0, help="只用最近 N 条（0=全部）")
    sp.add_argument("--out", default=None, help="输出目录（默认 <项目根>\\exports）")
    sp.add_argument("--no-history", action="store_true",
                    help="只出统计报告，不导出聊天记录")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    # 记录本轮**显式出现**的开关：会话配置文件只填没被显式给出的项，
    # 保证「命令行 > 配置文件 > 默认」的优先级。
    args._given = {a.split("=", 1)[0] for a in argv if a.startswith("--")}
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
