"""跨进程互斥锁：让多个实例（各自独立的 systemd 进程）严格串行运行 Chromium 任务。

30 个账号各占一个实例，但共用同一份代码；「发送」「获取好友列表」都会拉起无头
Chromium（每个峰值约 500~700MB）。用这把锁保证任意时刻只有一个实例在跑浏览器，
实现「不同时发」，并把内存峰值压到最低。

锁文件放在代码目录（所有实例共享同一份代码），而非各实例的 DATA_DIR——否则每个
实例锁的是各自目录下不同的文件，等于没有互斥。前提：所有实例部署在同一台机器、
共用同一份代码（方案 B）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import BASE_DIR

LOCK_PATH = Path(os.environ.get("LOCK_FILE") or (BASE_DIR / ".send.lock"))

try:
    import fcntl  # Linux / Unix
except ImportError:  # pragma: no cover - Windows 本地开发
    fcntl = None

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover
    msvcrt = None


class ProcessLock:
    """跨进程阻塞互斥锁。默认无限等待（严格排队串行）。"""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            self._file = open(self.path, "a+")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # Windows：忙等 + 单字节锁
            if not self.path.exists() or self.path.stat().st_size == 0:
                with open(self.path, "wb") as f:
                    f.write(b"\0")
            self._file = open(self.path, "r+b")
            while True:
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(1)
        else:  # 既无 fcntl 也无 msvcrt 的极少数平台：退化为无锁，直接继续
            self._file = open(self.path, "a+")

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                self._file.seek(0)
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
