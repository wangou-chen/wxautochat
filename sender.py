# -*- coding: utf-8 -*-
"""发送主流程：激活窗口 -> 校验会话 -> 粘贴 -> 校验输入框 -> 回车 -> 回读 DB 确认。

**每一步都先校验再动手**，这是本模块的核心原则。原因：微信 4.x 没有无障碍树，
一切坐标都来自截图 OCR，而 OCR 会读错、窗口会被缩放或移动。任何一步的假设
被打破都必须中止，而不是"照原计划发出去"。

已实测确认的事实（勿凭直觉改）：
  * 窗口 1092x1025 @(1355,132)，**显示缩放 150%**。Python 与 PowerShell 两侧
    都必须声明 DPI 感知，否则坐标差 1.5 倍。
  * 当前会话名可用 OCR 从右侧面板顶部读出（实测 `尼杰布旅游大团（10）`）。
  * `发送（S）` 按钮可被 OCR 定位（实测 (959,968)），**输入框就在它正上方**，
    实测粘贴文本出现在 y=881（按钮上方 87px）。所以输入框位置是**推导**出来的，
    不是硬编码。
  * OCR 读中文有误差（实测 `标记` -> `标讠己`、`123` -> `12《`）。
    因此粘贴校验必须**模糊比对**，不能做严格相等。
  * 激活窗口后输入框**通常已有焦点**，实测直接粘贴即可成功，无需点击。
    点击输入框只作为兜底。
"""
import os
import random
import re
import time

from . import config, ui

# 停止开关：存在该文件即立刻停止一切发送
STOP_FILE = os.path.join(config.ROOT, "STOP")
SENT_LOG = os.path.join(config.ROOT, "replies", "sent.jsonl")
SHOT_DIR = os.path.join(config.ROOT, "work", "sent_shots")


class SendError(Exception):
    pass


def stopped() -> bool:
    return os.path.exists(STOP_FILE)


# ---------------------------------------------------------------- 校验工具

def norm(s: str) -> str:
    """归一化，用于模糊比对：去掉所有非字母数字汉字。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(s or "")).lower()


def lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度（滚动数组，O(min) 空间）。"""
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[-1]


def _split_script(s: str):
    """把文本按字符类别拆成 (ASCII 串, 汉字串)。

    OCR 对 ASCII 的识别远好于汉字（实测 `能不能说chinese` 被读成
    `自刁《自乥兑℃hinese` —— 汉字全错但 `hinese` 基本正确）。
    分开比对能避免汉字噪声把整体相似度拖到阈值以下。
    """
    a = "".join(re.findall(r"[0-9a-zA-Z]+", str(s or "")))
    c = "".join(re.findall(r"[\u4e00-\u9fff]+", str(s or "")))
    return a.lower(), c


def similar(expected: str, got: str, threshold: float = 0.6) -> bool:
    """模糊判断 got 是否就是 expected（容忍 OCR 错字/漏字）。

    分级判定，依据是"OCR 对不同字符类的可靠性差异"：
      * **ASCII/数字**：OCR 很准（实测 `chinese` -> `hinese`，仅漏 1 字母）
      * **汉字**：OCR 很差（实测 `能不能说` -> `自刁自乥兑`，全错）

    规则：
      1. 归一化后互为子串 -> 通过
      2. 长度 ≤4 的短文本 -> 要求整体 LCS ≥0.6（短文本容错空间小，
         `在吗` vs `不在` 必须判否）
      3. 期望文本含 ≥4 字符的 ASCII 段 -> **以 ASCII 段为准**（≥0.75 即通过）。
         不能再用汉字段否决：实测正确文本的汉字段会被 OCR 读成全错。
      4. 其余（纯汉字为主）-> 整体 LCS ≥threshold，或汉字段 LCS ≥0.6
    """
    a, b = norm(expected), norm(got)
    if not a:
        return False
    # got 为空必须直接判否：`"" in a` 恒为 True，会让"粘贴失败、输入框为空"
    # 被误判成通过，随后回车发出空消息。实测踩过。
    if not b:
        return False
    if a in b or b in a:
        return True

    ea, ec = _split_script(expected)
    ga, gc = _split_script(got)

    if len(a) <= 4:
        return lcs_len(a, b) / len(a) >= 0.6

    if len(ea) >= 4:
        # 允许 2 个字符误差（实测 `chinese` 被 OCR 读成 `hines`，漏掉首尾两个
        # 字母；固定比例 0.75 会把它判否）
        return lcs_len(ea, ga) >= max(3, len(ea) - 2)

    if lcs_len(a, b) / len(a) >= threshold:
        return True
    if ec and gc and lcs_len(ec, gc) / len(ec) >= 0.6:
        return True
    return False


def norm_conv(s: str) -> str:
    """归一化会话名：去掉空格、括号里的成员数、表情等。"""
    s = re.sub(r"[（(]\d+[)）]", "", str(s or ""))
    return norm(s)


# ---------------------------------------------------------------- 锚点

def find_anchors(ocr: dict) -> dict:
    """从 OCR 结果里推导布局锚点。

    **发送按钮的判据必须严格**：早期只要求"文本含『发送』且在下半屏"，
    结果聊天消息里出现「发送」二字时会被误认成按钮，锚点跳到消息区，
    推出的输入框区域与点击坐标全错 —— 表现为"点击输入框后粘贴为空"，
    是今天所有「部分发出」失败的根因（实测点击坐标从 y=1144 漂到 y=643）。

    现在要求同时满足：在窗口右侧、位于底部条带、文本形态像按钮、宽度不大。
    """
    W, H = ocr["width"], ocr["height"]
    out = {"width": W, "height": H, "left": ocr["left"], "top": ocr["top"],
           "title": "", "title_y": None, "send": None, "panel_left": None}
    # 标题：右上区域**最靠上**的一行。
    # 为什么不是"取最宽"：实测（22:11:13）消息列表的第一条气泡落在 y=110，仍在
    # 原来的 H*0.09≈115 高度带内，而它比标题宽（292 > 178），"取最宽"于是把
    # **一条消息**当成了标题 -> 会话名校验失败 -> 整轮 5 条只发出 1 条就中止。
    # 顶部那一行只可能是会话标题，所以按 y 取最小；同一高度带（±24px）内再取最宽，
    # 以容纳标题右侧同高的副标题/成员数。
    cands = [ln for ln in ocr["lines"]
             if ln["x"] > W * 0.30 and ln["y"] < H * 0.12]
    best = None
    if cands:
        y_min = min(ln["y"] for ln in cands)
        best = max([ln for ln in cands if ln["y"] <= y_min + 24],
                   key=lambda l: l["w"])
    if best:
        out["title"] = best["text"]
        out["title_y"] = best["y"]
        out["panel_left"] = best["x"]

    # 发送按钮：右下角、底部条带、短文本
    panel_left = out["panel_left"] or W * 0.38
    cands = []
    for ln in ocr["lines"]:
        flat = (ln["text"] or "").replace(" ", "")
        if "发送" not in flat:
            continue
        if ln["x"] < panel_left:                   # 必须在右侧面板
            continue
        if ln["y"] < H * 0.82:                     # 必须在底部条带
            continue
        if ln["w"] > 170:                          # 按钮文字不会很长
            continue
        if len(flat) > 8:                          # 「发送（S）」约 5-6 字
            continue
        cands.append(ln)
    if cands:
        out["send"] = max(cands, key=lambda l: l["y"])   # 取最靠下的那个
    return out


def input_region(anchors: dict) -> tuple:
    """推导输入框矩形（窗口内相对坐标）。

    优先用发送按钮锚定。**顶部必须避开工具栏**：实测 OCR 会把输入框上方那一排
    工具图标读成 `O` / `0`（位于发送按钮上方约 146px），若区域包含它们，
    `input_text()` 会返回 `'O'` 并被误判成"输入框有残留文本"。

    **找不到发送按钮时必须返回 None 而不是猜**：早期用 `H*0.94` 兜底，
    一旦锚点错误就会点进消息区、粘贴落空（实测踩过）。宁可中止也不能瞎点。
    返回 None 时调用方应当放弃本轮发送。
    """
    sb = anchors.get("send")
    if not sb:
        return None
    W, H = anchors["width"], anchors["height"]
    x0 = int(anchors.get("panel_left") or W * 0.38)
    y1 = max(0, int(sb["y"]) - 8)          # 按钮上方 8px 处收口
    y0 = max(0, y1 - 135)                  # 再往上 135px：跳过工具栏图标行
    if y1 - y0 < 40:                       # 区域太小说明锚点可疑
        return None
    return (x0, y0, W, y1)


# 输入框里不该出现的 UI 文本（按钮/提示/工具栏图标）
_UI_NOISE = re.compile(r"发送|折叠|聊天文件|聊天记录|表情|截图|文件|语音|视频|"
                       r"^\d+$|^[0-9？?]+$")


def input_text(ocr: dict, anchors: dict) -> str:
    """取输入框区域内的文本，过滤掉按钮等 UI 噪声。

    区域无法确定时返回空串（调用方会把"取不到内容"当成校验不过而中止，
    符合"宁可少发也不发错"的原则）。
    """
    reg = input_region(anchors)
    if not reg:
        return ""
    x0, y0, x1, y1 = reg
    out = []
    for ln in ocr.get("lines") or []:
        x, y = ln.get("x", 0), ln.get("y", 0)
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        t = (ln.get("text") or "").strip()
        if not t:
            continue
        # 去掉空格后若整体像 UI 文本则丢弃
        flat = t.replace(" ", "")
        if _UI_NOISE.search(flat):
            continue
        out.append(t)
    return " ".join(out)


# ---------------------------------------------------------------- 发送器

class Sender(object):
    """把一条（或一轮多条）文本发到指定会话。"""

    def __init__(self, target_display: str, dry_run: bool = True,
                 verbose: bool = True, allow_click_focus: bool = True,
                 shot: bool = True, no_enter: bool = False,
                 fast: bool = False, verify: str = "send"):
        self.target = target_display
        self.dry_run = dry_run
        # no_enter：排练模式。走完全部真实动作（激活/校验/粘贴/输入框确认），
        # 但**不按回车**，随后清空输入框。用于上线前的实链路验证。
        self.no_enter = no_enter
        # fast：回车前不再抓图核会话名（省约 1.8 秒/条）。仅保留零成本的前台
        # 窗口检查。用户承诺发送期间不抢焦点时可用。
        self.fast = fast
        # verify：OCR 校验档位（每次 OCR 约 1.8 秒，是主要耗时来源）
        #   "full" —— 粘贴后校验输入框内容 + 回车前再核会话名（最稳，最慢）
        #   "send" —— 只保留发送前的会话名校验（它同时提供点击锚点，省不掉），
        #             粘贴后不再校验、回车前不再校验；正确性由**发送后回读 DB** 保证
        #   "none" —— 连会话名都不核（不推荐）
        self.verify = verify
        self.verbose = verbose
        self.allow_click_focus = allow_click_focus
        self.shot = shot
        self.win = None
        self.anchors = None
        self.log = []

    # ------------------------------------------------------------ 基础

    def _say(self, msg):
        if self.verbose:
            print("    [send] %s" % msg, flush=True)

    def _ensure_foreground(self, where: str) -> bool:
        """确认微信在前台；不在则**重新激活一次**再判，仍不在才放弃。

        为什么要重试：用户中途切窗口是常态，直接中止会把整轮剩余消息全部丢掉
        （实测 7 次中止里有 2 次是这个原因，另有 6 次是会话名 OCR 误读）。
        重新激活与每轮发送开始时的 `refresh()` 行为一致，不额外抢焦点。
        """
        if ui.is_foreground(self.win.hwnd):
            return True
        self._say("%s：微信不在前台，尝试重新激活…" % where)
        try:
            ui.activate(self.win.hwnd)
        except Exception as e:                                     # noqa: BLE001
            self._say("重新激活失败: %s" % e)
        time.sleep(0.25)
        return ui.is_foreground(self.win.hwnd)

    def _check(self):
        if stopped():
            raise SendError("检测到 STOP 文件，已停止发送")

    def attach(self, shots: bool = True) -> dict:
        """首次定位并激活微信窗口。"""
        self._check()
        return self.refresh(shots=shots)

    def refresh(self, shots: bool = False) -> dict:
        """**每次发送前重新定位窗口并重新截图 OCR。**

        为什么不能缓存：窗口会被移动/缩放（实测用户把微信挪到右半屏后，
        窗口从 728x683@(966,110) 变成 1092x1025@(1355,132)）。若沿用旧的
        `rect`，所有点击的屏幕坐标都会打偏。另外微信重启后 hwnd 也会变。

        同时重新激活窗口 —— 用户可能中途切到别的程序。
        """
        self._check()
        win = ui.find_wechat()
        if not win:
            raise SendError("未找到微信主窗口（标题「微信」的 Qt 窗口）")
        if self.win is None or win.hwnd != self.win.hwnd:
            self._say("窗口变更: %s -> %s" % (self.win, win))
        self.win = win
        if not ui.activate(self.win.hwnd):
            raise SendError("无法把微信窗口激活到前台（可能被系统拒绝）")
        if not ui.is_foreground(self.win.hwnd):
            raise SendError("激活后微信不是前台窗口，中止以免误输入到别的程序")
        ocr = ui.capture_ocr(self.win.hwnd,
                             tag="s_%d" % int(time.time() * 1000) if shots
                             else "s_cur")
        # 截图期间的几何变化要重新同步（PrintWindow 用的是它自己读的 rect）
        self.win = ui.find_wechat() or self.win
        self.anchors = find_anchors(ocr)
        return ocr

    def verify_conversation(self, ocr: dict) -> bool:
        """确认当前打开的会话就是目标会话。**不符即中止。**"""
        title = self.anchors.get("title") or ""
        ok = norm_conv(title) == norm_conv(self.target)
        if not ok:
            # 容忍 OCR 错字
            ok = similar(self.target, title, threshold=0.7)
        self._say("当前会话 OCR=%r 目标=%r -> %s"
                  % (title, self.target, "一致" if ok else "不一致"))
        return ok

    def verify_conversation_retry(self, ocr: dict, tries: int = 3,
                                  gap: float = 0.4) -> bool:
        """会话名核验失败时**重拍再判**，避免把偶发误读当成切了会话。

        实测（22:11:13）：窗口几何正常，标题那一次 OCR 却抓到一条**消息气泡
        文本**（`笑这么开心是不是在等我夸你呀`），于是整轮 5 条只发出 1 条就
        中止。中止的保守方向是对的（宁可漏发也不发错会话），但代价不该由一次
        误读来付 —— 所以重拍 2 次，只要有一次读到正确标题就继续。
        """
        if self.verify_conversation(ocr):
            return True
        for i in range(1, max(1, tries)):
            time.sleep(gap)
            self._say("会话名核验失败，重拍第 %d 次…" % i)
            try:
                ocr2 = self._ocr_now("conv_retry%d" % i)
            except Exception as e:                                 # noqa: BLE001
                self._say("重拍失败: %s" % e)
                continue
            if self.verify_conversation(ocr2):
                return True
        return False

    # ------------------------------------------------------------ 输入

    def _ocr_now(self, tag: str) -> dict:
        ocr = ui.capture_ocr(self.win.hwnd, tag=tag)
        self.anchors = find_anchors(ocr)
        return ocr

    def focus_input(self, ocr: dict = None) -> bool:
        """**点击输入框确保焦点**。返回是否成功。

        为什么每次都要点：实测有两次失败是"输入框真的是空的"——`Ctrl+V` 没落
        进去。根因是焦点不在输入框（用户可能点过消息区或会话列表），而无障碍
        树又拿不到输入框控件，只能靠坐标点击。

        **区域未知时抛错而不是猜坐标** —— 瞎点会落到消息区，粘贴必然失败。
        """
        self._check()
        reg = input_region(self.anchors)
        if not reg:
            raise SendError("未能定位输入框（OCR 没找到发送按钮锚点），中止以免点错位置")
        if not self._ensure_foreground("点击输入框前"):
            raise SendError("点击输入框前微信已不是前台（重新激活后仍失败），中止")
        x0, y0, x1, y1 = reg
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        ui.click(self.win.rect[0] + cx, self.win.rect[1] + cy)
        if not ui.is_foreground(self.win.hwnd):
            raise SendError("点击后微信不再是前台，中止")
        if self.verbose:
            self._say("已点击输入框 (%d,%d) 确保焦点" % (cx, cy))
        return True

    def type_text(self, text: str, before: str = "") -> bool:
        """粘贴文本。`verify=full` 时才做 OCR 内容校验，否则只负责粘贴。

        **为什么默认不再校验**：每次 OCR 约 1.8 秒，是主要耗时来源。实测
        「点击输入框确保焦点」这一步修好后，粘贴本身已很可靠；而**正确性由
        发送后回读数据库保证**（`_verify_sent`），比截图可靠得多。
        万一粘贴落空，回车对空输入框无效（微信不发空消息），回读会报未通过。

        需要更强保证时用 `--verify full`：粘贴后 OCR 比对内容 + 回车前再核会话。
        """
        self._check()

        ui.clip_set(text)
        time.sleep(0.12)
        ui.key_combo("CTRL", "V")
        time.sleep(0.55)

        if self.verify != "full":
            return True

        ocr = self._ocr_now("in_after")
        if not self.verify_conversation(ocr):
            self._say("粘贴后会话变成别的了，清空并中止")
            self.clear_input()
            return False
        after = input_text(ocr, self.anchors)
        self._say("粘贴后输入框=%r" % after[:60])
        if similar(text, after):
            return True
        # 输入框由空变非空 + 长度量级相当，也算落在框里（OCR 汉字可能全错）
        if not before.strip() and after.strip() \
                and len(norm(after)) >= max(2, int(len(norm(text)) * 0.4)):
            self._say("文字比对未过，但输入框由空变非空且长度相当 -> 认可")
            return True

        # 第 2 次机会：只重读，不清空（文字很可能已经在了，是 OCR 读错）
        self._say("换个时刻重读一次输入框（不清空）")
        time.sleep(0.7)
        ocr = self._ocr_now("in_recheck")
        after = input_text(ocr, self.anchors)
        self._say("重读输入框=%r" % after[:60])
        if similar(text, after):
            return True

        # 最后兜底：清空 -> 点击输入框 -> 重贴 -> 再校验
        self._say("重读仍不确认，清空后点击输入框重贴")
        self.clear_input()
        time.sleep(0.4)
        if not (self.allow_click_focus and ui.is_foreground(self.win.hwnd)):
            return False
        reg = input_region(self.anchors)
        if not reg:
            self._say("输入框区域无法确定，放弃重贴")
            return False
        x0, y0, x1, y1 = reg
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        ui.click(self.win.rect[0] + cx, self.win.rect[1] + cy)
        ui.clip_set(text)
        ui.key_combo("CTRL", "V")
        time.sleep(1.1)
        ocr = self._ocr_now("in_after2")
        if not self.verify_conversation(ocr):
            self._say("重试后会话又变了，清空并中止")
            self.clear_input()
            return False
        after = input_text(ocr, self.anchors)
        self._say("点击重贴后输入框=%r" % after[:60])
        return similar(text, after)

    def clear_input(self):
        ui.key_combo("CTRL", "A")
        ui.press("DEL")
        time.sleep(0.2)
        ui.press("DEL")

    def press_enter(self):
        """按回车。**按键前重新校验前台窗口**；非 fast 模式再抓图核会话。

        从激活窗口到此刻已过数秒，期间用户可能切走了窗口；若在前台不是
        微信时按回车，回车会打到别的程序上。前台检查是**零成本**的，必定执行。
        抓图核会话名约 1.8 秒，可用 `fast=True` 关掉（用户承诺不抢焦点时安全）。
        """
        self._check()
        if not self._ensure_foreground("回车前"):
            raise SendError("回车前微信已不是前台（重新激活后仍失败），中止"
                            "（避免回车打到别的窗口）")
        if self.verify == "full" and not self.fast:
            ocr = self._ocr_now("pre_enter")
            if not self.verify_conversation(ocr):
                self._say("回车前会话变了，清空并中止")
                self.clear_input()
                raise SendError("回车前会话已切换，中止")
        if not ui.is_foreground(self.win.hwnd):
            raise SendError("回车前前台又变了，中止")
        ui.press("RETURN")
        time.sleep(0.3)

    # ------------------------------------------------------------ 单条

    def send_one(self, text: str, index: int = 1, total: int = 1) -> dict:
        """发送单条消息。返回结果字典。dry_run 时只打印不按键。"""
        rec = {"text": text, "index": index, "total": total,
               "ok": False, "reason": "", "shot": None}
        if self.dry_run:
            self._say("[dry-run] 将发送第 %d/%d 条：%r" % (index, total, text))
            rec.update(ok=True, reason="dry-run")
            self.log.append(rec)
            return rec

        # 发送前留档截图（出问题可回溯）
        if self.shot:
            try:
                os.makedirs(SHOT_DIR, exist_ok=True)
                p = os.path.join(SHOT_DIR, "%s_%d_%d.png"
                                 % (time.strftime("%Y%m%d_%H%M%S"),
                                    index, int(time.time() * 1000) % 1000))
                ui.capture_ocr(self.win.hwnd, tag="pre_send")
                src = os.path.join(config.ROOT, "work", "shots", "pre_send",
                                   "shot.png")
                if os.path.exists(src):
                    import shutil
                    shutil.copyfile(src, p)
                    rec["shot"] = p
            except Exception as e:                                 # noqa: BLE001
                self._say("留档截图失败（不影响发送）: %s" % e)

        # 每次发送前**重新定位窗口 + 重新截图**（窗口可能已被移动/缩放）
        ocr = self.refresh(shots=bool(self.shot))
        if not self.verify_conversation_retry(ocr):
            rec["reason"] = "会话不符，中止（已重拍核验）"
            self.log.append(rec)
            raise SendError(rec["reason"])
        # 这一份 OCR 同时用于"输入框是否残留"，省掉一次截图（每次约 1.8 秒）
        left = input_text(ocr, self.anchors)
        if left.strip():
            self._say("输入框有残留 %r，先清空" % left[:40])
            self.clear_input()
            time.sleep(0.2)
        # **每次粘贴前点击输入框确保焦点** —— 实测有两次失败是焦点不在输入框，
        # Ctrl+V 无处可去，输入框始终是空的。
        self.focus_input(ocr)

        if not self.type_text(text, before=left):
            self.clear_input()
            rec["reason"] = "粘贴后未在输入框确认到该文本，已清空并中止"
            self.log.append(rec)
            return rec

        if self.no_enter:
            self._say("排练模式：输入框已确认，清空且不回车 -> %r" % text)
            self.clear_input()
            time.sleep(0.3)
            ocr2 = self._ocr_now("noenter_%d" % index)
            left = input_text(ocr2, self.anchors)
            rec.update(ok=not left.strip(),
                       reason="排练完成（未发送）" if not left.strip()
                       else "清空后输入框仍有残留: %r" % left[:40])
            self.log.append(rec)
            return rec

        self.press_enter()
        rec.update(ok=True, reason="已回车")
        self.log.append(rec)
        return rec

    def send_burst(self, messages: list, gap=0.8) -> dict:
        """按轮次连发多条。任一条失败即停止后续。

        gap 可以是单个秒数，也可以是 (最小, 最大) —— 后者每条随机取，
        连发间隔不固定才像人。实测对方（翔铭）明确说「你打字的速度能不能
        稍微慢一些，让我感受到是真人」，固定 0.8 秒 + 每条约 3.5 秒的
        机械节奏会被一眼看穿。
        """
        sent, failed = [], None
        for i, t in enumerate(messages, 1):
            try:
                r = self.send_one(t, index=i, total=len(messages))
            except SendError as e:
                failed = str(e)
                break
            if r["ok"]:
                sent.append(t)
            else:
                failed = r["reason"]
                break
            if i < len(messages):
                time.sleep(random.uniform(gap[0], gap[1])
                           if isinstance(gap, (list, tuple)) else float(gap))
        return {"sent": sent, "failed": failed,
                "complete": len(sent) == len(messages)}
