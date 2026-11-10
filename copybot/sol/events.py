"""Zdarzenia na żywo dla aplikacji: kupno/sprzedaż danej osoby i nowe wiadomości na czacie.

Aplikacja pyta /api/sol/live i czeka (do ~25 s), aż coś się zmieni – dostaje wiadomość od razu,
zamiast odpytywać serwer co kilka sekund.
"""
import asyncio
from collections import defaultdict


class Bus:
    def __init__(self):
        self.user_v: dict[int, int] = defaultdict(int)
        self.chat_v = 0
        self._cond: asyncio.Condition | None = None

    def _c(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def _wake(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def notify():
            async with self._c():
                self._c().notify_all()
        loop.create_task(notify())

    def bump_user(self, uid: int):
        if uid > 0:
            self.user_v[uid] += 1
            self._wake()

    def bump_chat(self):
        self.chat_v += 1
        self._wake()

    def snapshot(self, uid: int) -> dict:
        return {"v": self.user_v[uid], "c": self.chat_v}

    async def wait(self, uid: int, v: int, c: int, timeout: float = 25) -> dict:
        if self.user_v[uid] != v or self.chat_v != c:
            return self.snapshot(uid)
        try:
            async with self._c():
                await asyncio.wait_for(self._c().wait_for(lambda: self.user_v[uid] != v or self.chat_v != c),
                                       timeout)
        except asyncio.TimeoutError:
            pass
        return self.snapshot(uid)


BUS = Bus()
