# -*- coding: utf-8 -*-
"""wxautochat · 微信自动回复工具

P1 阶段：实时读取新消息。

模块划分：
  crypto.py   SQLCipher 4 解密（与 wxarchive.crypto 同源，独立以免依赖）
  sessions.py 会话标识 -> username/显示名 解析
  snapshot.py 维护可增量套用 WAL 的本地解密副本
  watcher.py  轮询 new-message 检测
  config.py   配置（DeepSeek key 等，从环境变量读）
"""

__version__ = "0.1.0"
