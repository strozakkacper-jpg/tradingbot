"""Nasłuch portfeli Solany w czasie rzeczywistym.

Szybka ścieżka (z HELIUS_API_KEY): Helius Enhanced WebSocket `transactionSubscribe` na statusie „processed” –
pełna transakcja przychodzi w powiadomieniu, więc nie ma dodatkowego zapytania getTransaction.
Zapasowa ścieżka: `logsSubscribe` (processed) + getTransaction (confirmed) z szybkimi ponowieniami.
"""
import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Awaitable, Callable

import aiohttp

from .chain import TX_VERSION, Rpc
from .parser import Swap, parse_swap

log = logging.getLogger(__name__)
OnSwap = Callable[[str, Swap, float], Awaitable[None]]  # (portfel, wymiana, czas wykrycia – time.monotonic())


class SolWatcher:
    def __init__(self, ws_url: str, enhanced_url: str, rpc: Rpc, session: aiohttp.ClientSession,
                 get_wallets: Callable[[], set[str]], on_swap: OnSwap):
        self.ws_url, self.enhanced_url, self.rpc, self.session = ws_url, enhanced_url, rpc, session
        self.get_wallets, self.on_swap = get_wallets, on_swap
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.mode = "start"  # enhanced | logs
        self.connected = False
        self.enhanced_failed = not enhanced_url
        self.last_error: str | None = None
        self.stats = {"events": 0, "swaps": 0}

    async def run(self):
        backoff = 1
        while True:
            try:
                if not self.enhanced_failed:
                    self.mode = "enhanced"
                    await self._session(self.enhanced_url, enhanced=True)
                else:
                    self.mode = "logs"
                    await self._session(self.ws_url, enhanced=False)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log.warning("Solana WebSocket (%s) rozłączony: %s – ponawiam za %ss", self.mode, e, backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

    async def _session(self, url: str, enhanced: bool):
        if not self.get_wallets():
            await asyncio.sleep(5)  # nikogo nie śledzimy – nie trzymamy połączenia
            return
        proxy = os.getenv("WSS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or None
        async with self.session.ws_connect(url, heartbeat=20, max_msg_size=0, proxy=proxy) as ws:
            self.connected = True
            log.info("Solana: połączono (%s)", "Helius Enhanced – pełne transakcje" if enhanced else "logsSubscribe")
            subs: dict[int, str] = {}
            pending: dict[int, str] = {}
            req_id = 0

            async def sync():
                nonlocal req_id
                want = self.get_wallets()
                have = set(subs.values()) | set(pending.values())
                for w in want - have:
                    req_id += 1
                    pending[req_id] = w
                    if enhanced:
                        params = [{"accountInclude": [w], "vote": False, "failed": False},
                                  {"commitment": "processed", "encoding": "jsonParsed", "transactionDetails": "full",
                                   "showRewards": False, "maxSupportedTransactionVersion": TX_VERSION}]
                        await ws.send_json({"jsonrpc": "2.0", "id": req_id, "method": "transactionSubscribe",
                                            "params": params})
                    else:
                        await ws.send_json({"jsonrpc": "2.0", "id": req_id, "method": "logsSubscribe",
                                            "params": [{"mentions": [w]}, {"commitment": "processed"}]})
                for sid, w in list(subs.items()):
                    if w not in want:
                        req_id += 1
                        del subs[sid]
                        await ws.send_json({"jsonrpc": "2.0", "id": req_id,
                                            "method": "transactionUnsubscribe" if enhanced else "logsUnsubscribe",
                                            "params": [sid]})

            await sync()
            loop = asyncio.get_running_loop()
            next_sync = loop.time() + 5
            while True:
                try:
                    msg = await ws.receive(timeout=max(next_sync - loop.time(), 0.1))
                except asyncio.TimeoutError:
                    await sync()
                    next_sync = loop.time() + 5
                    continue
                t_recv = time.monotonic()
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"zamknięte ({msg.type.name})")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data and data["id"] in pending:
                    w = pending.pop(data["id"])
                    if "result" in data:
                        subs[data["result"]] = w
                    elif enhanced:  # plan Helius bez Enhanced WebSocket – przechodzimy na zapasową ścieżkę
                        self.enhanced_failed = True
                        raise ConnectionError(f"Helius Enhanced WebSocket niedostępny: {data.get('error')}")
                    else:
                        log.warning("Nie zasubskrybowano %s: %s", w, data.get("error"))
                    continue
                method = data.get("method")
                if method == "transactionNotification":
                    p = data["params"]
                    w = subs.get(p["subscription"])
                    res = p["result"]
                    if w:
                        asyncio.create_task(self._handle_tx(w, res["signature"], res["transaction"], t_recv))
                elif method == "logsNotification":
                    p = data["params"]
                    value = p["result"]["value"]
                    w = subs.get(p["subscription"])
                    if w and value.get("err") is None:
                        asyncio.create_task(self._handle_sig(w, value["signature"], t_recv))

    def _first(self, wallet: str, sig: str) -> bool:
        key = (wallet, sig)
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > 10_000:
            self._seen.popitem(last=False)
        self.stats["events"] += 1
        return True

    async def _handle_tx(self, wallet: str, sig: str, tx: dict, t_recv: float):
        if not self._first(wallet, sig):
            return
        await self._emit(wallet, parse_swap(tx, wallet), t_recv)

    async def _handle_sig(self, wallet: str, sig: str, t_recv: float):
        if not self._first(wallet, sig):
            return
        tx = None
        for _ in range(20):  # transakcja „processed” trafia do getTransaction dopiero po potwierdzeniu
            try:
                tx = await self.rpc.transaction(sig)
            except Exception as e:  # noqa: BLE001
                log.debug("getTransaction %s: %s", sig, e)
            if tx:
                break
            await asyncio.sleep(0.25)
        await self._emit(wallet, parse_swap(tx, wallet), t_recv)

    async def _emit(self, wallet: str, swap: Swap | None, t_recv: float):
        if not swap:
            return
        self.stats["swaps"] += 1
        log.info("Solana %s: %s %s (%.4g %s, %.1f%%) – wykryto w %.0f ms od sygnału", wallet[:6], swap.side,
                 swap.mint[:6], swap.quote_amount, swap.quote_symbol, swap.fraction * 100,
                 (time.monotonic() - t_recv) * 1000)
        try:
            await self.on_swap(wallet, swap, t_recv)
        except Exception:  # noqa: BLE001
            log.exception("Błąd kopiowania %s", swap.signature)
