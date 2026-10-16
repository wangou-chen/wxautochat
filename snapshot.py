# -*- coding: utf-8 -*-
"""增量快照：把 live SQLCipher 库维护成可被 SQLite 直接查询的本地解密副本。

设计要点（均来自实测）：
  * live 目录的 `*.db-wal` 是**唯一**的增量来源。微信检查点后会把已解密副本的 WAL 清零
    （实测 decrypted\\message_message_*.db-wal 全为 0），所以不能盯本地 WAL。
  * **一致性读**：微信在后台持续追加 WAL。先用 stat 定长、再单次 read，拿到的是
    "某一时刻的完整前缀"。WAL 的帧 salt 校验保证该前缀自洽，因此是安全快照。
  * **salt 变化 = WAL 重启**：检查点后 salt 会变，此时按 pgno 回填会把陈旧帧写成新数据。
    检测到 salt 改变即拒绝回填，并在下一轮做一次全量重建。
  * 游标用 `local_id`（INTEGER 主键，实测单调递增），不是 create_time（同秒可并列）。
"""
import json
import os
import shutil
import struct
import time

from . import crypto

STATE_FILE = "_state.json"
STATE_VERSION = 2


class DbState(object):
    __slots__ = ("wal_size", "wal_off", "salt1", "salt2", "mtime", "built", "checked")

    def __init__(self, wal_size=0, wal_off=0, salt1=None, salt2=None, mtime=0.0,
                 built=False, checked=0):
        self.wal_size = wal_size          # 已消费到的 WAL 字节长度
        self.wal_off = wal_off            # 保留字段
        # salt 用 None 作"尚未记录"的哨兵。**不要用 0** —— 真实 salt 可以是 0，
        # 且 `0 is not None` 为真会让首轮误判为"WAL 重启"而多跑一次全量重建。
        self.salt1 = salt1
        self.salt2 = salt2
        self.mtime = mtime
        self.built = built                # 本地解密副本是否已建立
        self.checked = checked

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        st = cls()
        for k in cls.__slots__:
            if k in d:
                setattr(st, k, d[k])
        return st


def _wal_header_salt(wal_bytes: bytes):
    """返回 WAL 头的两个 salt 值。"""
    if len(wal_bytes) < crypto.WAL_HEADER_SIZE:
        return None
    return (struct.unpack(">I", wal_bytes[16:20])[0],
            struct.unpack(">I", wal_bytes[20:24])[0])


class Snapshot(object):
    """管理 `<work>/<name>.db` 的增量解密副本。

    目录布局：
        <work>/message_message_0.db        解密后的可查询副本
        <work>/_state.json                 每库的 WAL 消费位置与 salt
    """

    def __init__(self, key: bytes, src_dir: str, work_dir: str, name: str,
                 verbose: bool = True):
        self.key = key
        self.src = os.path.join(src_dir, name)
        self.name = name
        self.work_dir = work_dir
        self.dst = os.path.join(work_dir, name)
        self.verbose = verbose
        os.makedirs(work_dir, exist_ok=True)

    # ------------------------------------------------------------------ 状态

    def _state_path(self):
        return os.path.join(self.work_dir, STATE_FILE)

    def _load_state(self) -> DbState:
        p = self._state_path()
        if not os.path.exists(p):
            return DbState()
        try:
            with open(p, "r", encoding="utf-8") as f:
                blob = json.load(f)
            return DbState.from_dict(blob.get(self.name, {}))
        except Exception:                                          # noqa: BLE001
            return DbState()

    def _save_state(self, st: DbState):
        """原子保存状态。失败只告警，不抛异常（下一轮会重写）。"""
        from . import lock
        p = self._state_path()
        blob = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    blob = json.load(f)
            except Exception:                                      # noqa: BLE001
                blob = {}
        blob["_version"] = STATE_VERSION
        blob[self.name] = st.to_dict()
        if not lock.write_json(p, blob) and self.verbose:
            print("  [warn] %s 状态保存失败（可能有另一个实例在写）" % self.name,
                  flush=True)

    # ------------------------------------------------------------------ 构建

    def _full_build(self, st: DbState):
        """整库解密（首次或检测到 WAL 重启时调用）。

        主库定长读（一致性快照），并把 WAL 从 0 重新消费 —— 因为全量重建后
        本地副本只反映"主库 + 已检查点的帧"，当前 WAL 内的未检查点帧仍需补上。
        """
        t0 = time.time()
        size = os.path.getsize(self.src)
        if self.verbose:
            print("  [build] %s (%.1f MB) 全量解密…" % (self.name, size / 1048576.0),
                  flush=True)
        npages = crypto.decrypt_db(self.src, self.dst, self.key, limit=size)
        st.built = True
        st.wal_size = 0
        if self.verbose:
            print("  [build] 完成 %d 页，%.1fs" % (npages, time.time() - t0), flush=True)

    # ------------------------------------------------------------------ 增量

    def sync(self) -> dict:
        """把 live 库的新增 WAL 帧应用到本地副本。返回统计信息。"""
        if not os.path.exists(self.src):
            return {"ok": False, "reason": "src-missing"}

        st = self._load_state()

        if not st.built or not os.path.exists(self.dst):
            self._full_build(st)

        wal_path = self.src + "-wal"
        wal_size_now = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        mtime_now = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0.0

        result = {"ok": True, "full_rebuild": False, "frames": 0,
                  "wal_size": wal_size_now, "changed": False}

        # WAL 被截断（微信做了检查点）-> 帧已并入主库，但本地副本是旧主库，
        # 不能简单归零游标，必须重建一次以拿到检查点后的主库内容。
        if wal_size_now < st.wal_size:
            if self.verbose:
                print("  [wal] %s 被检查点截断 %d -> %d，重建"
                      % (self.name, st.wal_size, wal_size_now), flush=True)
            self._full_build(st)
            result["full_rebuild"] = True

        # 无新增字节：直接返回。**必须有此门槛** —— 否则每轮都要重读整个 WAL
        # （实测 message_1.db-wal 达 62 MB，每轮重读会拖垮轮询）。
        # mtime 变但长度不变时仍走下方流程（内容可能被原地改写）。
        if wal_size_now == st.wal_size and mtime_now == st.mtime:
            result["changed"] = st.built is False
            self._save_state(st)
            return result

        if wal_size_now <= crypto.WAL_HEADER_SIZE:
            st.wal_size = wal_size_now
            st.mtime = mtime_now
            st.checked = int(time.time())
            self._save_state(st)
            result["changed"] = result["full_rebuild"]
            return result

        # 一致性读：按当前长度取整个前缀
        wal = crypto.read_wal_snapshot(wal_path, wal_size_now)
        salt = _wal_header_salt(wal)
        if salt is None:
            st.wal_size = wal_size_now
            st.mtime = mtime_now
            self._save_state(st)
            return result

        # salt 变化 = WAL 被重建（非单纯追加）。此时旧帧的 pgno 语义已变，
        # 直接回填会把陈旧页覆盖到正确数据上 -> 必须全量重建。
        if st.salt1 is not None and (salt[0] != st.salt1 or salt[1] != st.salt2):
            if self.verbose:
                print("  [wal] %s salt 变化 (%s,%s)->(%s,%s)，全量重建"
                      % (self.name, st.salt1, st.salt2, salt[0], salt[1]), flush=True)
            self._full_build(st)
            result["full_rebuild"] = True
            wal = crypto.read_wal_snapshot(wal_path, wal_size_now)

        st.salt1, st.salt2 = salt

        frames = crypto.replay_wal(wal, self.dst, self.key, verbose=self.verbose)
        st.wal_size = len(wal)
        st.mtime = mtime_now
        st.checked = int(time.time())
        self._save_state(st)

        result["frames"] = frames
        result["changed"] = frames > 0 or result["full_rebuild"]
        return result

    def invalidate(self):
        """删除本地副本与状态，强制下次全量重建。"""
        for p in (self.dst, self.dst + "-wal", self.dst + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        st = self._load_state()
        st.built = False
        st.wal_size = 0
        st.salt1 = st.salt2 = None
        self._save_state(st)
