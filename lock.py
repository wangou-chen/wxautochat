# -*- coding: utf-8 -*-
"""单实例锁 + 原子写文件的竞态兜底。

**为什么需要**：`work/` 下的状态文件（`_cursors.json`、`_state.json`、
`_lock.json`）被多个进程同时写时会冲突。实测踩过：同时跑两个 `reply` 实例，
`os.replace(tmp, dst)` 抛 `PermissionError: [WinError 32] 另一个程序正在使用
此文件`，直接把整个轮询循环打崩。

两层防御：
  1. `acquire()` —— 启动时拿单实例锁，发现别的实例在活跃就拒绝启动（明确报错，
     而不是让它跑起来再随机崩）
  2. `write_json()` —— 原子写：临时文件名带 pid（避免两个进程撞同一个 .tmp），
     并对 `os.replace` 做重试；最终失败只告警不抛异常（丢一次保存不影响正确性，
     下一轮会重写；但崩掉整个循环是致命的）
"""
import json
import os
import time

LOCK_NAME = "_lock.json"
# 心跳超过这个秒数就认为锁已失效（进程可能被强杀，来不及删锁）
STALE_SECONDS = 60.0


def _lock_path(work_dir: str) -> str:
    return os.path.join(work_dir, LOCK_NAME)


def read_lock(work_dir: str) -> dict:
    p = _lock_path(work_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:                                              # noqa: BLE001
        return {}


def heartbeat(work_dir: str, note: str = "") -> None:
    """刷新心跳。每轮 poll 调一次。"""
    try:
        write_json(_lock_path(work_dir),
                   {"pid": os.getpid(), "at": time.time(), "note": note})
    except Exception:                                              # noqa: BLE001
        pass


def acquire(work_dir: str, note: str = "", verbose: bool = True) -> bool:
    """尝试获取单实例锁。返回 True 表示可以运行。

    判断依据是**心跳时间**而不是进程是否存在 —— 拿 PID 去查进程在受限环境下
    不一定可用，而心跳只管"最近有没有人在写"，语义更贴近需求。
    """
    os.makedirs(work_dir, exist_ok=True)
    cur = read_lock(work_dir)
    age = time.time() - float(cur.get("at") or 0)
    if cur and age < STALE_SECONDS and cur.get("pid") != os.getpid():
        if verbose:
            print("[拒绝启动] 已有实例在运行（pid=%s，%.0f 秒前还在写状态）。\n"
                  "  状态文件 %s 被两个进程同时写会导致崩溃。\n"
                  "  处理：先停掉那个实例，或删除 %s 后重试。"
                  % (cur.get("pid"), age, LOCK_NAME, _lock_path(work_dir)))
        return False
    heartbeat(work_dir, note)
    return True


def release(work_dir: str) -> None:
    """正常退出时删除锁。"""
    try:
        cur = read_lock(work_dir)
        if cur.get("pid") == os.getpid():
            os.remove(_lock_path(work_dir))
    except Exception:                                              # noqa: BLE001
        pass


def write_json(path: str, obj, retries: int = 5, delay: float = 0.15) -> bool:
    """原子写 JSON。临时文件名带 pid，避免两进程撞同一个 .tmp。

    返回是否成功。**失败只返回 False，不抛异常** —— 状态文件丢一次保存不影响
    正确性（下一轮会重写），但把整个循环打崩是致命的。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception:                                              # noqa: BLE001
        return False
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(delay * (i + 1))
        except Exception:                                          # noqa: BLE001
            break
    try:
        os.remove(tmp)
    except Exception:                                              # noqa: BLE001
        pass
    return False
