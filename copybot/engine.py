"""Silnik copytradingu: obserwuje konto lidera i składa zlecenia u obserwujących."""
import asyncio
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

import ccxt.async_support as ccxt

from .billing import has_access
from .config import Config, exchange_name
from .db import DB, now_ms, ref_id
from .security import Vault
from .sizing import LeaderOrder, follower_order, split_symbol

log = logging.getLogger(__name__)

Notify = Callable[[int, str], Awaitable[None]]
PAPER_FEE = 0.001  # 0,1% prowizji w trybie demo
DEFAULT_MIN_COST = 5.0
# stablecoiny traktujemy jak tę samą walutę (≈1 USD): BTC/USDT u tradera = BTC/USD lub BTC/USDC na Coinbase
STABLES = ("USDT", "USDC", "USD")


def resolve_symbol(markets: dict, symbol: str) -> str | None:
    """Ta sama para na innej giełdzie: najpierw dokładnie, potem z innym stablecoinem."""
    if symbol in markets:
        return symbol
    base, quote = split_symbol(symbol)
    if quote in STABLES:
        for q in STABLES:
            if f"{base}/{q}" in markets:
                return f"{base}/{q}"
    return None


def spot_only(ex):
    """Bot handluje tylko spot: nie pobieramy rynków futures/opcji (mniej zapytań, mniej punktów awarii)."""
    fm = ex.options.get("fetchMarkets")
    if isinstance(fm, dict) and "types" in fm:
        fm = dict(fm)
        fm["types"] = ["spot"] if isinstance(fm["types"], list) else {"spot": True}
        ex.options["fetchMarkets"] = fm
    if ex.id == "mexc" and hasattr(ex, "fetch_spot_markets"):  # MEXC zawsze pobiera też futures
        ex.fetch_markets = ex.fetch_spot_markets
    return ex


def user_exchange(cfg: Config, row) -> str:
    """Giełda użytkownika albo tradera (wiersz z bazy); domyślnie EXCHANGE z .env."""
    ex = row["exchange"] if row is not None and "exchange" in row.keys() else None
    return ex or cfg.exchange_id


def make_exchange(cfg: Config, key: str | None = None, secret: str | None = None, password: str | None = None,
                  exchange_id: str | None = None):
    exchange_id = exchange_id or cfg.exchange_id
    # aiohttp_trust_env: respektuj HTTPS_PROXY, jeśli serwer chodzi przez proxy
    params = {"enableRateLimit": True, "aiohttp_trust_env": True, "timeout": 15000,
              "options": {"defaultType": "spot"}}
    if os.getenv("SSL_CERT_FILE"):
        params["cafile"] = os.environ["SSL_CERT_FILE"]
    if key:
        params.update(apiKey=key, secret=secret)
        if password:
            params["password"] = password
    ex = getattr(ccxt, exchange_id)(params)
    spot_only(ex)
    if cfg.sandbox:
        try:
            ex.set_sandbox_mode(True)
        except ccxt.NotSupported:
            log.warning("%s nie ma testnetu – używam prawdziwej giełdy", exchange_id)
    return ex


def fmt_money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def fmt_amount(x: float) -> str:
    return f"{x:.8g}"


SIDE_PL = {"buy": "🟢 KUPNO", "sell": "🔴 SPRZEDAŻ"}


class Market:
    """Publiczne dane rynkowe (ceny i limity) jednej giełdy – wspólne dla wszystkich."""

    def __init__(self, cfg: Config, exchange_id: str | None = None):
        self.cfg = cfg
        self.exchange_id = exchange_id or cfg.exchange_id
        self.ex = make_exchange(cfg, exchange_id=self.exchange_id)
        self._lock = asyncio.Lock()
        self._prices: dict[str, tuple[float, float]] = {}  # symbol -> (czas, cena)

    async def load(self):
        async with self._lock:
            if not self.ex.markets:
                await self.ex.load_markets()

    async def has(self, symbol: str) -> bool:
        return await self.resolve(symbol) is not None

    async def resolve(self, symbol: str) -> str | None:
        await self.load()
        return resolve_symbol(self.ex.markets, symbol)

    async def price(self, symbol: str, max_age: float = 10.0) -> float:
        cached = self._prices.get(symbol)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        t = await self.ex.fetch_ticker(symbol)
        p = float(t.get("last") or t.get("close") or 0)
        self._prices[symbol] = (time.monotonic(), p)
        return p

    async def min_cost(self, symbol: str) -> float:
        await self.load()
        c = ((self.ex.markets[symbol].get("limits") or {}).get("cost") or {}).get("min")
        return float(c) if c else DEFAULT_MIN_COST

    async def equity(self, balances: dict[str, float]) -> float:
        """Wartość portfela w walucie kwotowanej (np. USDT)."""
        await self.load()
        q = self.cfg.quote
        total = 0.0
        for asset, amount in balances.items():
            if not amount:
                continue
            if asset == q or asset in STABLES:
                total += amount
            elif sym := resolve_symbol(self.ex.markets, f"{asset}/{q}"):
                total += amount * await self.price(sym)
        return total

    async def close(self):
        await self.ex.close()


class Markets:
    """Rynki wszystkich giełd (tworzone przy pierwszym użyciu). Metody bez giełdy = giełda domyślna."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._m: dict[str, Market] = {}

    def get(self, exchange_id: str | None = None) -> Market:
        ex = exchange_id or self.cfg.exchange_id
        if ex not in self._m:
            self._m[ex] = Market(self.cfg, ex)
        return self._m[ex]

    @property
    def ex(self):
        return self.get().ex

    async def load(self):
        await self.get().load()

    async def has(self, symbol: str) -> bool:
        return await self.get().has(symbol)

    async def resolve(self, symbol: str) -> str | None:
        return await self.get().resolve(symbol)

    async def price(self, symbol: str, max_age: float = 10.0) -> float:
        return await self.get().price(symbol, max_age)

    async def min_cost(self, symbol: str) -> float:
        return await self.get().min_cost(symbol)

    async def equity(self, balances: dict[str, float]) -> float:
        return await self.get().equity(balances)

    async def close(self):
        for m in self._m.values():
            await m.close()


async def _resolve(market, symbol: str) -> str | None:
    if hasattr(market, "resolve"):
        return await market.resolve(symbol)
    return symbol if await market.has(symbol) else None


@dataclass
class Fill:
    amount: float
    price: float
    cost: float


class Skip(Exception):
    pass


class CopyEngine:
    def __init__(self, cfg: Config, db: DB, vault: Vault, market: Market, notify: Notify, concurrency: int = 5):
        self.cfg, self.db, self.vault, self.market, self.notify = cfg, db, vault, market, notify
        self.sem = asyncio.Semaphore(concurrency)

    async def copy(self, order: LeaderOrder, fraction: float, leader_id: int) -> Counter:
        users = [u for u in self.db.active_followers(leader_id) if has_access(self.cfg, u)]
        leader = self.db.get_leader(leader_id)
        name = leader["name"] if leader else f"#{leader_id}"
        log.info("Trader %s: %s %s %.4f%% → %d obserwujących", name, order.side, order.symbol, fraction * 100,
                 len(users))
        await self._copy_reference(order, fraction, leader_id)
        title = f"✅ Skopiowano: {name}"

        async def run(u):
            async with self.sem:
                return await self._copy_for(u, order, fraction, title=title)

        res = Counter(await asyncio.gather(*(run(u) for u in users)))
        # śledzący bez kopiowania dostają samo powiadomienie
        pct = f" ({fraction * 100:.0f}% {'salda' if order.side == 'buy' else 'pozycji'})" if fraction else ""
        price = f" @ {fmt_money(order.price)}" if order.price else ""
        for u in self.db.watchers(leader_id):
            await self.notify(u["tg_id"], f"👁 {name}: {SIDE_PL[order.side]} {order.symbol}{price}{pct}")
            res["alert"] += 1
        return res

    def m(self, row) -> "Market":
        return self.market.get(user_exchange(self.cfg, row)) if isinstance(self.market, Markets) else self.market

    async def _copy_reference(self, order: LeaderOrder, fraction: float, leader_id: int):
        """Portfel wzorcowy: każde zlecenie 1:1, bez limitu – z niego liczymy publiczne wyniki tradera."""
        rid = ref_id(leader_id)
        ref = {"tg_id": rid, "paper": 1, "multiplier": 1.0, "max_trade": 0}
        try:
            market = self.m(self.db.get_leader(leader_id))
            order = replace(order, symbol=await _resolve(market, order.symbol) or order.symbol)
            price = await market.price(order.symbol)
            fill = self._paper(ref, order, fraction, price, min_cost=0.01)
            self.db.record_copy(order.id, rid, order.symbol, order.side, fill.amount, fill.price,
                                fill.cost, "filled", None, True)
        except Skip:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Błąd portfela wzorcowego")

    async def _copy_for(self, u, order: LeaderOrder, fraction: float, title: str = "✅ Skopiowano zlecenie lidera",
                        quiet_skip: bool = False) -> str:
        paper = bool(u["paper"])
        mode = "🧪 demo" if paper else "💰 LIVE"
        side_txt = f"{SIDE_PL[order.side]} {order.symbol}"
        try:
            market = self.m(u)
            local = await _resolve(market, order.symbol)
            if not local:
                raise Skip(f"para {order.symbol} niedostępna na {exchange_name(market.exchange_id)}")
            if local != order.symbol:  # np. BTC/USDT u tradera → BTC/USD na Coinbase
                order = replace(order, symbol=local)
                side_txt = f"{SIDE_PL[order.side]} {order.symbol}"
            price = await market.price(order.symbol)
            min_cost = await market.min_cost(order.symbol)
            move = u["max_price_move"] if "max_price_move" in u.keys() else 0
            if order.side == "buy" and order.price > 0 and move and price > order.price * (1 + move / 100):
                raise Skip(f"cena uciekła o {(price / order.price - 1) * 100:.1f}% od ceny tradera "
                           f"(limit {move:g}%) – nie gonimy rynku")
            if paper:
                fill = self._paper(u, order, fraction, price, min_cost)
            else:
                fill = await self._live(u, order, fraction, price, min_cost)
        except Skip as e:
            if not quiet_skip:
                self.db.record_copy(order.id, u["tg_id"], order.symbol, order.side, 0, 0, 0, "skipped", str(e), paper)
                await self.notify(u["tg_id"], f"⚠️ Pominięto {side_txt}: {e}")
            return "skipped"
        except Exception as e:  # noqa: BLE001 – każdy błąd giełdy raportujemy użytkownikowi
            log.exception("Błąd kopiowania dla %s", u["tg_id"])
            self.db.record_copy(order.id, u["tg_id"], order.symbol, order.side, 0, 0, 0, "error", str(e)[:500], paper)
            await self.notify(u["tg_id"], f"❌ Nie udało się skopiować {side_txt}\n{type(e).__name__}: {str(e)[:200]}")
            return "error"

        self.db.record_copy(order.id, u["tg_id"], order.symbol, order.side, fill.amount, fill.price, fill.cost,
                            "filled", None, paper)
        await self.notify(u["tg_id"], (
            f"{title}\n{side_txt}\n"
            f"Ilość: {fmt_amount(fill.amount)} · Cena: {fmt_money(fill.price)}\n"
            f"Wartość: {fmt_money(fill.cost)} {self.cfg.quote} · Tryb: {mode}"))
        return "filled"

    def _paper(self, u, order, fraction, price, min_cost) -> Fill:
        base, quote = split_symbol(order.symbol)
        if quote in STABLES:  # konto demo trzyma jeden stablecoin (QUOTE) – USD/USDC liczymy jak USDT
            quote = self.cfg.quote
        bal = self.db.paper_balances(u["tg_id"])
        s = follower_order(order.side, fraction, u["multiplier"], bal.get(quote, 0), bal.get(base, 0),
                           price, u["max_trade"], min_cost)
        if s.skip_reason:
            raise Skip(s.skip_reason)
        if order.side == "buy":
            deltas = {quote: -s.cost, base: s.amount * (1 - PAPER_FEE)}
        else:
            deltas = {base: -s.amount, quote: s.cost * (1 - PAPER_FEE)}
        self.db.paper_apply(u["tg_id"], deltas)
        return Fill(s.amount, price, s.cost)

    async def _live(self, u, order, fraction, price, min_cost) -> Fill:
        base, quote = split_symbol(order.symbol)
        market = self.m(u)
        ex = make_exchange(self.cfg, self.vault.dec(u["api_key"]), self.vault.dec(u["api_secret"]),
                           self.vault.dec(u["api_password"]), exchange_id=market.exchange_id)
        try:
            ex.set_markets(market.ex.markets, market.ex.currencies)
            free = (await ex.fetch_balance()).get("free") or {}
            s = follower_order(order.side, fraction, u["multiplier"], float(free.get(quote) or 0),
                               float(free.get(base) or 0), price, u["max_trade"], min_cost)
            if s.skip_reason:
                raise Skip(s.skip_reason)
            try:
                amount = float(ex.amount_to_precision(order.symbol, s.amount))
            except ccxt.InvalidOrder as e:
                raise Skip("ilość poniżej minimalnej precyzji giełdy") from e
            if amount <= 0:
                raise Skip("ilość po zaokrągleniu = 0")
            # cenę podajemy, bo część giełd wymaga jej do policzenia wartości zlecenia rynkowego
            o = await ex.create_order(order.symbol, "market", order.side, amount, price)
            filled = float(o.get("filled") or amount)
            avg = float(o.get("average") or o.get("price") or price)
            return Fill(filled, avg, float(o.get("cost") or filled * avg))
        finally:
            await ex.close()

    async def close_positions(self, u) -> Counter:
        """Sprzedaje wszystko, co bot kupił użytkownikowi (np. po stop-lossie).
        Nie rusza innych monet, których bot nigdy nie handlował."""
        u = dict(u, multiplier=1.0)
        res = Counter()
        for symbol in self.db.traded_symbols(u["tg_id"]):
            order = LeaderOrder(id=f"stoploss-{now_ms()}", symbol=symbol, side="sell", amount=0, cost=0,
                                price=0, ts=now_ms())
            res[await self._copy_for(u, order, 1.0, title="🛑 Stop-loss: sprzedano", quiet_skip=True)] += 1
        return res

    async def equity_of(self, u) -> float:
        bal = self.db.paper_balances(u["tg_id"]) if u["paper"] else await self.live_balance(u)
        return await self.m(u).equity(bal)

    async def verify_keys(self, key: str, secret: str, password: str | None,
                          exchange_id: str | None = None) -> dict[str, float]:
        """Sprawdza klucze; zwraca niezerowe salda."""
        ex = make_exchange(self.cfg, key, secret, password, exchange_id=exchange_id)
        try:
            return {k: v for k, v in ((await ex.fetch_balance()).get("total") or {}).items() if v}
        finally:
            await ex.close()

    async def live_balance(self, u) -> dict[str, float]:
        return await self.verify_keys(self.vault.dec(u["api_key"]), self.vault.dec(u["api_secret"]),
                                      self.vault.dec(u["api_password"]), user_exchange(self.cfg, u))
