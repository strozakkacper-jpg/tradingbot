"""Obserwatorzy kont traderów: WebSocket giełdy (≈1 s) z zapasowym odpytywaniem.

Każdy trader ze źródłem „api” ma własnego obserwatora. Pary do sprawdzania bot odkrywa sam
z salda tradera, więc nie trzeba ich nigdzie wpisywać.
"""
import asyncio
import logging
import os
from typing import Awaitable, Callable

import ccxt.pro as ccxtpro

from .config import Config
from .db import DB, now_ms
from .engine import STABLES, Markets, make_exchange, resolve_symbol, spot_only, user_exchange
from .security import Vault
from .sizing import LeaderOrder, group_trades, leader_fractions

log = logging.getLogger(__name__)
OnOrders = Callable[[int, list[tuple[LeaderOrder, float]]], Awaitable[None]]
LOOKBACK_MS = 15 * 60 * 1000  # przy odpytywaniu patrzymy 15 min wstecz (duplikaty odsiewa baza)
WS_RETRY_S = 120  # po awarii WebSocketu odpytujemy przez 2 min i próbujemy znowu


def make_pro(cfg: Config, exchange_id: str, key: str, secret: str, password: str | None):
    params = {"enableRateLimit": True, "aiohttp_trust_env": True, "options": {"defaultType": "spot"},
              "apiKey": key, "secret": secret}
    if password:
        params["password"] = password
    if proxy := os.getenv("HTTPS_PROXY"):
        params["wssProxy"] = proxy
    ex = spot_only(getattr(ccxtpro, exchange_id)(params))
    if cfg.sandbox:
        try:
            ex.set_sandbox_mode(True)
        except Exception:  # noqa: BLE001
            pass
    return ex


class LeaderWatcher:
    def __init__(self, cfg: Config, db: DB, vault: Vault, markets: Markets, leader, on_orders: OnOrders,
                 rest=None, pro=None):
        self.cfg, self.db, self.on_orders = cfg, db, on_orders
        self.leader_id, self.name = leader["id"], leader["name"]
        self.exchange_id = user_exchange(cfg, leader)
        self.market = markets.get(self.exchange_id) if isinstance(markets, Markets) else markets
        creds = (vault.dec(leader["api_key"]), vault.dec(leader["api_secret"]), vault.dec(leader["api_password"]))
        self.rest = rest or make_exchange(cfg, *creds, exchange_id=self.exchange_id)
        self.pro = pro if pro is not None else (make_pro(cfg, self.exchange_id, *creds) if cfg.realtime else None)
        self.mode = "start"  # ws | poll
        self.last_event: int | None = None
        self.last_error: str | None = None
        self._known_symbols: set[str] = set()
        self._lock = asyncio.Lock()
        key = f"leader_since:{self.leader_id}"
        if self.db.get_state(key) is None:  # pierwszy start – historii nie kopiujemy
            self.db.set_state(key, str(now_ms()))
        self.start_ts = int(self.db.get_state(key))

    # --- pętla główna ---
    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            if self.pro is not None and self.pro.has.get("watchMyTrades"):
                try:
                    self.mode = "ws"
                    await self._ws_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"WebSocket: {e}"
                    log.warning("Trader %s: WebSocket padł (%s) – przechodzę na odpytywanie", self.name, e)
            self.mode = "poll"
            end = loop.time() + WS_RETRY_S if self.pro is not None else float("inf")
            while loop.time() < end:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)[:200]
                    log.warning("Trader %s: błąd odpytywania: %s", self.name, e)
                await asyncio.sleep(self.cfg.poll_interval)

    async def _ws_loop(self):
        await self.poll_once()  # nadrabiamy to, co mogło umknąć przed połączeniem
        while True:
            trades = await self.pro.watch_my_trades()
            await asyncio.sleep(0.3)  # chwila na pozostałe wypełnienia tego samego zlecenia
            await self.process(list(trades))

    # --- odpytywanie ---
    async def _balance(self) -> dict[str, float]:
        return {k: float(v) for k, v in ((await self.rest.fetch_balance()).get("total") or {}).items() if v}

    def _symbols_for(self, balance: dict[str, float]) -> set[str]:
        q = self.cfg.quote
        markets = self.market.ex.markets or {}
        out = set()
        for a in balance:
            if a != q and a not in STABLES and (sym := resolve_symbol(markets, f"{a}/{q}")):
                out.add(sym)
        return out

    async def poll_once(self):
        await self.market.load()
        if not self.rest.markets:
            self.rest.set_markets(self.market.ex.markets, self.market.ex.currencies)
        if not self._known_symbols:
            self._known_symbols = self._symbols_for(await self._balance())
        symbols = self._known_symbols | set(self.cfg.leader_symbols)
        since = max(self.start_ts, now_ms() - LOOKBACK_MS)
        trades = []
        for sym in symbols:
            trades += await self.rest.fetch_my_trades(sym, since=since)
        balance = await self._balance()
        # pamiętamy pary z poprzedniego i obecnego salda – żeby nie przegapić sprzedaży „do zera”
        self._known_symbols = self._symbols_for(balance) | {t["symbol"] for t in trades}
        await self.process(trades, balance)

    # --- przetwarzanie ---
    async def process(self, trades: list[dict], balance: dict[str, float] | None = None):
        q = self.cfg.quote
        async with self._lock:  # noqa: SIM117
            # tylko pary ze stablecoinem (USDT/USDC/USD) – np. ETH/BTC pomijamy
            trades = [t for t in trades if int(t["timestamp"]) >= self.start_ts
                      and t["symbol"].split("/")[-1].split(":")[0] in (q, *STABLES)]
            new = self.db.unseen_trades(self.leader_id, trades)
            if not new:
                return
            if balance is None:
                balance = await self._balance()
            pairs = leader_fractions(group_trades(new), balance)
            # oznaczamy jako widziane PRZED kopiowaniem: wolimy pominąć niż zdublować zlecenie
            self.db.mark_trades(self.leader_id, new)
            self.last_event = now_ms()
            self._known_symbols |= {t["symbol"] for t in new}
        await self.on_orders(self.leader_id, pairs)

    async def balance(self) -> dict[str, float]:
        return await self._balance()

    async def close(self):
        await self.rest.close()
        if self.pro is not None:
            await self.pro.close()


class Watchers:
    """Uruchamia i zatrzymuje obserwatorów zgodnie z listą aktywnych traderów."""

    def __init__(self, cfg: Config, db: DB, vault: Vault, market: Markets, on_orders: OnOrders):
        self.cfg, self.db, self.vault, self.market, self.on_orders = cfg, db, vault, market, on_orders
        self.running: dict[int, tuple[LeaderWatcher, asyncio.Task]] = {}

    def sync(self):
        want = {lead["id"]: lead for lead in self.db.leaders("active") if lead["source"] == "api"}
        for lid, (w, task) in list(self.running.items()):
            if lid not in want:
                task.cancel()
                asyncio.create_task(w.close())
                del self.running[lid]
        for lid, lead in want.items():
            if lid not in self.running:
                w = LeaderWatcher(self.cfg, self.db, self.vault, self.market, lead, self.on_orders)
                self.running[lid] = (w, asyncio.create_task(w.run()))
                log.info("Śledzę tradera %s (%s)", lead["name"], "WebSocket" if w.pro is not None else "odpytywanie")

    def get(self, leader_id: int) -> LeaderWatcher | None:
        item = self.running.get(leader_id)
        return item[0] if item else None

    async def close(self):
        for w, task in self.running.values():
            task.cancel()
            await w.close()
        self.running.clear()
