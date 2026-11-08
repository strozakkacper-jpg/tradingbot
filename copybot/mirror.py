"""Tryb lustra: portfel kopiującego ma te same proporcje co portfel tradera (np. 50% BTC, 20% ETH, 30% USDT).

Bot ciągle porównuje oba portfele – zaraz po każdym ruchu tradera i dodatkowo co kilka minut – i wyrównuje
różnice większe niż tolerancja. Dzięki temu nadrabia też transakcje, które w zwykłym trybie by umknęły
(minimalna wartość zlecenia, przerwa w działaniu, wpłaty i wypłaty tradera).

Bezpieczeństwo: lustro zarządza tylko monetami, które ma trader, i tymi, które bot wcześniej kupił.
Innych monet na koncie użytkownika nie rusza i nie wlicza ich do podziału.
"""
import asyncio
import html
import logging
from collections import Counter
from dataclasses import dataclass, field

from .db import ref_id
from .engine import STABLES, Skip, fmt_money
from .sizing import split_symbol

log = logging.getLogger(__name__)
DUST = 0.002  # pozycje poniżej 0,2% portfela tradera pomijamy
_locks: dict[int, asyncio.Lock] = {}


@dataclass
class Plan:
    equity: float  # wartość zarządzanej części portfela kopiującego
    current: dict[str, float]  # aktywo -> udział (0–1), gotówka pod kluczem QUOTE
    target: dict[str, float]
    orders: list[tuple[str, str, float]] = field(default_factory=list)  # (aktywo, buy/sell, wartość)

    @property
    def match(self) -> float:
        """Zgodność z traderem: 100% = identyczne proporcje."""
        keys = set(self.current) | set(self.target)
        return max(0.0, 1 - sum(abs(self.current.get(k, 0) - self.target.get(k, 0)) for k in keys) / 2)


async def _values(market, balances: dict[str, float], quote: str, only: set[str] | None = None) -> dict[str, float]:
    """Wartość każdego aktywa w walucie kwotowanej; stablecoiny łączymy jako gotówkę."""
    out: dict[str, float] = {}
    for asset, amount in balances.items():
        if not amount or amount <= 0:
            continue
        if asset == quote or asset in STABLES:
            out[quote] = out.get(quote, 0) + amount
            continue
        if only is not None and asset not in only:
            continue
        sym = await market.resolve(f"{asset}/{quote}") if hasattr(market, "resolve") else f"{asset}/{quote}"
        if not sym:
            continue
        out[asset] = amount * await market.price(sym)
    return out


async def trader_weights(s, lead) -> dict[str, float]:
    """Proporcje portfela tradera. Konto giełdowe: prawdziwe saldo; sygnały: portfel wzorcowy."""
    q = s.cfg.quote
    w = s.watchers.get(lead["id"]) if (s.watchers and lead["source"] == "api") else None
    if w is not None:
        bal, market = await w.balance(), w.market
    else:
        bal, market = s.db.paper_balances(ref_id(lead["id"])), s.engine.m(lead)
    vals = await _values(market, bal, q)
    total = sum(vals.values())
    if total <= 0:
        return {q: 1.0}
    weights = {k: v / total for k, v in vals.items() if v / total >= DUST or k == q}
    norm = sum(weights.values())
    return {k: v / norm for k, v in weights.items()}


async def target_weights(s, u) -> dict[str, float]:
    """Kilku kopiowanych traderów = równy podział kapitału między ich portfele."""
    leaders = s.db.copied_leaders(u["tg_id"])
    if not leaders:
        return {}
    parts = [await trader_weights(s, lead) for lead in leaders]
    out: dict[str, float] = {}
    for p in parts:
        for k, v in p.items():
            out[k] = out.get(k, 0) + v / len(parts)
    return out


async def plan(s, u, target: dict[str, float] | None = None) -> Plan:
    q = s.cfg.quote
    target = target if target is not None else await target_weights(s, u)
    market = s.engine.m(u)
    bal = s.db.paper_balances(u["tg_id"]) if u["paper"] else await s.engine.live_balance(u)
    bot_assets = {split_symbol(sym)[0] for sym in s.db.traded_symbols(u["tg_id"])}
    managed = (set(target) | bot_assets) - {q}
    vals = await _values(market, bal, q, only=managed)
    equity = sum(vals.values())
    current = {k: v / equity for k, v in vals.items()} if equity > 0 else {}
    p = Plan(equity, current, target)
    if equity <= 0 or not target:
        return p
    tol = (u["mirror_tolerance"] if "mirror_tolerance" in u.keys() else 2) / 100
    try:
        min_cost = await market.min_cost(f"BTC/{q}")
    except Exception:  # noqa: BLE001
        min_cost = 5.0
    for asset in sorted(managed):
        diff = (target.get(asset, 0) - current.get(asset, 0)) * equity
        if abs(diff) >= max(min_cost, tol * equity):
            p.orders.append((asset, "buy" if diff > 0 else "sell", abs(diff)))
    p.orders.sort(key=lambda o: o[1] != "sell")  # najpierw sprzedaż – zwalnia gotówkę na zakupy
    return p


async def sync(s, u, reason: str = "", notify: bool = True) -> Counter:
    """Wyrównuje portfel do traderów. Jedno wyrównanie na użytkownika naraz."""
    lock = _locks.setdefault(u["tg_id"], asyncio.Lock())
    if lock.locked():
        return Counter()
    async with lock:
        u = s.db.get_user(u["tg_id"]) or u
        p = await plan(s, u)
        res, lines = Counter(), []
        for asset, side, value in p.orders:
            try:
                fill = await s.engine.trade_value(u, f"{asset}/{s.cfg.quote}", side, value)
                res["filled"] += 1
                lines.append(f"{'🟢 kupno' if side == 'buy' else '🔴 sprzedaż'} {asset}: {fmt_money(fill.cost)} "
                             f"{s.cfg.quote}")
            except Skip as e:
                res["skipped"] += 1
                log.info("Lustro %s %s %s: %s", u["tg_id"], side, asset, e)
            except Exception as e:  # noqa: BLE001
                res["error"] += 1
                log.exception("Lustro %s %s %s", u["tg_id"], side, asset)
                lines.append(f"❌ {asset}: {str(e)[:120]}")
        if res:
            log.info("Lustro %s (%s): %s · zgodność przed %.0f%%", u["tg_id"], reason or "-", dict(res), p.match * 100)
        if notify and lines:
            after = await plan(s, s.db.get_user(u["tg_id"]) or u, p.target)
            await s.engine.notify(u["tg_id"], "🪞 Lustro: wyrównano portfel do tradera"
                                  + (f" ({reason})" if reason else "") + "\n" + "\n".join(lines)
                                  + f"\nZgodność: {after.match * 100:.0f}%")
        return res


def plan_text(s, u, p: Plan) -> str:
    q = s.cfg.quote
    names = ", ".join(html.escape(lead["name"]) for lead in s.db.copied_leaders(u["tg_id"])) or "—"
    lines = ["🔍 <b>Porównanie z traderem</b>", f"Trader: {names}", ""]
    if not p.target:
        return "\n".join(lines + ["Nie kopiujesz jeszcze żadnego tradera."])
    lines.append("<code>Aktywo     Ty   Trader</code>")
    keys = sorted(set(p.current) | set(p.target), key=lambda k: (k == q, -p.target.get(k, 0)))
    for k in keys:
        cur, tgt = p.current.get(k, 0) * 100, p.target.get(k, 0) * 100
        flag = "" if abs(cur - tgt) < 1 else (" ⬆" if tgt > cur else " ⬇")
        lines.append(f"<code>{k[:8]:<8} {cur:5.1f}% {tgt:5.1f}%</code>{flag}")
    lines += ["", f"Zgodność: <b>{p.match * 100:.0f}%</b> · zarządzana wartość: {fmt_money(p.equity)} {q}"]
    if p.orders:
        lines.append("\nDo wyrównania:")
        for asset, side, value in p.orders:
            lines.append(f"• {'kup' if side == 'buy' else 'sprzedaj'} {asset} za ~{fmt_money(value)} {q}")
    else:
        lines.append("✅ Portfel zgodny z traderem (w granicach tolerancji).")
    return "\n".join(lines)
