# -*- coding: utf-8 -*-
"""SQLCipher 4 页面解密（微信 4.0.x / 4.1.0.x 实测参数）。

与 wxarchive.crypto 同源，此处独立实现使 wxautochat 可单目录携带。
关键点（踩坑记录，勿改）：
  * 内存里的 32 字节 enc_key **直接作 AES-256 密钥**，不做任何 KDF 派生。
    误加派生会让 page1 仍"恰好"解出 SQLite 头前 16 字节，极难定位。
  * 密文区间：page1 = [16 : 4016)（4000 B），pageN = [0 : 4016)。
    IV 位于 [4016 : 4032)。写成 [16:4032) 会多取一整块导致全错。
"""
import os
import struct

from Crypto.Cipher import AES

PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVED = 80                       # IV(16) + HMAC(64)
CT_END = PAGE_SIZE - RESERVED       # 4016
SQLITE_HDR = b"SQLite format 3\x00"

WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_FRAME_SIZE = WAL_FRAME_HEADER_SIZE + PAGE_SIZE
BTREE_TYPES = (0x0D, 0x05, 0x0A, 0x02)


def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    """解密单页，输出 4096 字节标准 SQLite 页。"""
    if len(page) < PAGE_SIZE:
        page = page + b"\x00" * (PAGE_SIZE - len(page))
    iv = page[CT_END:CT_END + IV_SIZE]
    if pgno == 1:
        body = page[SALT_SIZE:CT_END]
        dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(body)
        return SQLITE_HDR + dec + b"\x00" * RESERVED
    dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page[0:CT_END])
    return dec + b"\x00" * RESERVED


def check_key(enc_key: bytes, page1: bytes, page2: bytes = None) -> bool:
    """判定密钥正确性：page1 解出 SQLite 头 + page_size=4096；page2 首字节为 b-tree 类型。

    这比 HMAC 更可靠 —— 内存里同一 salt 会残留多把历史密钥。
    """
    try:
        d1 = decrypt_page(enc_key, page1, 1)
    except Exception:                                          # noqa: BLE001
        return False
    if d1[:16] != SQLITE_HDR or int.from_bytes(d1[16:18], "big") != PAGE_SIZE:
        return False
    if page2:
        try:
            d2 = decrypt_page(enc_key, page2, 2)
        except Exception:                                      # noqa: BLE001
            return False
        if d2[0] not in BTREE_TYPES:
            return False
    return True


def read_salt(path: str) -> bytes:
    """读加密库前 16 字节（page1 的明文 salt）。"""
    with open(path, "rb") as f:
        return f.read(SALT_SIZE)


def decrypt_db(src: str, dst: str, enc_key: bytes, progress=None,
               limit: int = None) -> int:
    """解密整库，返回页数。

    limit 用于**一致性读**：先 stat 取长度，只解密该长度的整数页。
    微信在后台持续写主库，若边读边被追加会撕裂；定长后单次顺序读即安全快照。
    """
    size = os.path.getsize(src)
    if limit is not None:
        size = min(size, limit)
    npages = size // PAGE_SIZE
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for pgno in range(1, npages + 1):
            page = fin.read(PAGE_SIZE)
            if len(page) < PAGE_SIZE:
                break
            fout.write(decrypt_page(enc_key, page, pgno))
            if progress and pgno % 20000 == 0:
                progress(pgno, npages)
    return npages


def read_wal_snapshot(wal_path: str, limit: int = None) -> bytes:
    """**一致性读**：先定长、再一次性读取 WAL 前 limit 字节。

    微信在后台持续写 WAL。若边读边被追加，直接 open().read() 可能拿到撕裂数据。
    先 stat 取长度、再按该长度单次 read，可保证得到的是"某一时刻的完整前缀"——
    WAL 的帧校验机制保证该前缀本身是自洽快照。
    limit=None 时取当前全长。
    """
    if not os.path.exists(wal_path):
        return b""
    size = os.path.getsize(wal_path)
    if limit is not None:
        size = min(size, limit)
    if size <= WAL_HEADER_SIZE:
        return b""
    with open(wal_path, "rb") as f:
        return f.read(size)


def replay_wal(wal_bytes: bytes, decrypted_db: str, enc_key: bytes,
               verbose: bool = False) -> int:
    """把 WAL 帧按 pgno 回填到已解密的库，返回回填帧数。

    wal_bytes 必须是 read_wal_snapshot() 得到的自洽前缀。
    帧的 salt 校验用于丢弃 WAL 重启（检查点后 salt 会变）前的陈旧帧。
    """
    if len(wal_bytes) <= WAL_HEADER_SIZE:
        return 0
    ws1 = struct.unpack(">I", wal_bytes[16:20])[0]
    ws2 = struct.unpack(">I", wal_bytes[20:24])[0]
    patched = 0
    skipped = 0
    off = WAL_HEADER_SIZE
    with open(decrypted_db, "r+b") as df:
        while off + WAL_FRAME_SIZE <= len(wal_bytes):
            fh = wal_bytes[off:off + WAL_FRAME_HEADER_SIZE]
            ep = wal_bytes[off + WAL_FRAME_HEADER_SIZE:off + WAL_FRAME_SIZE]
            off += WAL_FRAME_SIZE
            pgno = struct.unpack(">I", fh[0:4])[0]
            s1 = struct.unpack(">I", fh[8:12])[0]
            s2 = struct.unpack(">I", fh[12:16])[0]
            if pgno == 0 or pgno > 10_000_000:
                continue
            if s1 != ws1 or s2 != ws2:
                skipped += 1
                continue
            df.seek((pgno - 1) * PAGE_SIZE)
            df.write(decrypt_page(enc_key, ep, pgno))
            patched += 1
    if verbose and skipped:
        print("    WAL: 回填 %d 帧，跳过陈旧 %d 帧" % (patched, skipped))
    return patched
