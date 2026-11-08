"""Czysta logika: jak duże zlecenie ma złożyć obserwujący, żeby odwzorować lidera.

Model: kopiujemy *procent portfela*, a nie kwotę.
- Lider KUPUJE za 20% swoich USDT  -> obserwujący kupuje za 20% swoich USDT (x mnożnik, max limit).
- Lider SPRZEDAJE 50% swojego BTC   -> obserwujący sprzedaje 50% swojego BTC.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LeaderOrder:
    id: str
    symbol: str
    side: str  # "buy" | "sell"
    amount: float  # ilość waluty bazowej
    cost: float  # wartość w walucie kwotowanej
    price: float
    ts: int  # ms


@dataclass(frozen=True)
class Sizing:
    amount: float
    cost: float
    skip_reason: str | None = None


def split_symbol(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/")
    return base, quote.split(":")[0]


def group_trades(trades: list[dict]) -> list[LeaderOrder]:
    """Łączy pojedyncze wypełnienia (fills) z ccxt w zlecenia."""
    groups: dict[str, dict] = {}
    for t in trades:
        oid = str(t.get("order") or t["id"])
        g = groups.setdefault(oid, {"symbol": t["symbol"], "side": t["side"], "amount": 0.0, "cost": 0.0, "ts": 0})
        amount = float(t["amount"])
        g["amount"] += amount
        g["cost"] += float(t.get("cost") or amount * float(t["price"]))
        g["ts"] = max(g["ts"], int(t["timestamp"]))
    orders = [
        LeaderOrder(id=oid, symbol=g["symbol"], side=g["side"], amount=g["amount"], cost=g["cost"],
                    price=g["cost"] / g["amount"] if g["amount"] else 0.0, ts=g["ts"])
        for oid, g in groups.items()
    ]
    return sorted(orders, key=lambda o: o.ts)


def leader_fractions(orders: list[LeaderOrder], balances_now: dict[str, float]) -> list[tuple[LeaderOrder, float]]:
    """Dla każdego zlecenia lidera liczy, jaki ułamek salda wykorzystał.

    Saldo sprzed każdego zlecenia odtwarzamy cofając się od salda bieżącego
    (od najnowszego zlecenia do najstarszego).
    """
    bal = {k: float(v or 0) for k, v in balances_now.items()}
    out = []
    for o in sorted(orders, key=lambda o: o.ts, reverse=True):
        base, quote = split_symbol(o.symbol)
        if o.side == "buy":
            before = bal.get(quote, 0.0) + o.cost
            frac = o.cost / before if before > 0 else 0.0
            bal[quote] = before
            bal[base] = bal.get(base, 0.0) - o.amount
        else:
            before = bal.get(base, 0.0) + o.amount
            frac = o.amount / before if before > 0 else 0.0
            bal[base] = before
            bal[quote] = bal.get(quote, 0.0) - o.cost
        out.append((o, min(max(frac, 0.0), 1.0)))
    out.reverse()
    return out


def follower_order(side: str, fraction: float, multiplier: float, quote_free: float, base_free: float,
                   price: float, max_trade_quote: float | None, min_cost: float) -> Sizing:
    """Wielkość zlecenia obserwującego. Limit kwotowy dotyczy tylko zakupów –
    sprzedaż (zamykanie pozycji) zawsze odwzorowujemy w pełni."""
    if price <= 0:
        return Sizing(0, 0, "brak ceny")
    f = min(max(fraction, 0.0) * multiplier, 1.0)
    if side == "buy":
        cost = quote_free * f
        if max_trade_quote:
            cost = min(cost, max_trade_quote)
        amount = cost / price
    else:
        amount = base_free * f
        cost = amount * price
    if cost < min_cost:
        return Sizing(amount, cost, f"za mała wartość ({cost:.2f} < {min_cost:.2f})")
    return Sizing(amount, cost)
