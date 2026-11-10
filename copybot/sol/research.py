"""Badanie traderów: z ostatnich transakcji portfela liczy zysk, skuteczność, czas trzymania i aktywność.

Bierzemy tylko transakcje PODPISANE przez portfel (spam od innych się nie liczy) i rozpoznane jako wymiany
(parse_swap). Ocena nagradza zysk i skuteczność, a karze flipperów (trzymają sekundy – kopia nie zdąży)
i brak aktywności.
"""
import asyncio
import logging
import statistics
import time

from .parser import parse_swap

log = logging.getLogger(__name__)
DAY = 86400


async def analyze_wallet(rpc, wallet: str, days: float = 3, max_tx: int = 30) -> dict:
    now = time.time()
    sigs = await rpc.call("getSignaturesForAddress", [wallet, {"limit": 200, "commitment": "confirmed"}])
    ok = [x for x in sigs if x.get("err") is None and x.get("blockTime") and now - x["blockTime"] < days * DAY]
    swaps = []
    fetched = 0
    for x in ok:
        if fetched >= max_tx:
            break
        tx = await rpc.transaction(x["signature"])
        fetched += 1
        keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys") or []
        payer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if payer != wallet:
            continue  # spam / cudza transakcja z jego adresem
        sw = parse_swap(tx, wallet)
        if sw and sw.quote_symbol == "SOL":
            swaps.append((x["blockTime"], sw))
    return summarize(wallet, swaps, days, len(ok), fetched)


def summarize(wallet: str, swaps: list, days: float, ok_count: int = 0, fetched: int = 0) -> dict:
    swaps.sort(key=lambda t: t[0])
    pos: dict[str, dict] = {}
    closed = []
    buys = []
    for ts, sw in swaps:
        p = pos.setdefault(sw.mint, {"cost": 0.0, "tok": 0, "start": ts, "got": 0.0})
        if sw.side == "buy":
            if p["tok"] <= 0:
                p.update(cost=0.0, got=0.0, start=ts)
            p["cost"] += sw.quote_amount
            p["tok"] += sw.token_raw
            buys.append(sw.quote_amount)
        elif p["tok"] > 0:
            p["got"] += sw.quote_amount
            p["tok"] -= sw.token_raw
            if p["tok"] <= 0 or sw.fraction > 0.97:
                closed.append({"pnl": p["got"] - p["cost"], "pct": (p["got"] / p["cost"] - 1) * 100 if p["cost"] else 0,
                               "hold": ts - p["start"], "cost": p["cost"], "ts": ts})
                p.update(tok=0, cost=0.0, got=0.0)
    wins = [c for c in closed if c["pnl"] > 0]
    holds = sorted(c["hold"] for c in closed)
    pnl = sum(c["pnl"] for c in closed)
    span = max((swaps[-1][0] - swaps[0][0]) / DAY, 1 / 24) if len(swaps) > 1 else days
    out = {"wallet": wallet, "swaps": len(swaps), "closed": len(closed), "pnl": pnl,
           "win_rate": len(wins) / len(closed) if closed else None,
           "hold_s": holds[len(holds) // 2] if holds else None,
           "avg_pct": statistics.mean(c["pct"] for c in closed) if closed else None,
           "avg_buy": statistics.mean(buys) if buys else None,
           "per_day": len(swaps) / span if swaps else 0, "last": swaps[-1][0] if swaps else None,
           "ok_tx": ok_count, "fetched": fetched}
    invested = sum(c["cost"] for c in closed)
    out["roi"] = pnl / invested * 100 if invested > 0 else None
    by_day: dict[int, float] = {}
    for c in closed:
        by_day[int(c["ts"] // DAY)] = by_day.get(int(c["ts"] // DAY), 0) + c["pnl"]
    out["active_days"] = len(by_day)
    out["green_days"] = sum(1 for v in by_day.values() if v > 0)
    out["first"] = swaps[0][0] if swaps else None
    out["score"] = score(out)
    return out


def score(a: dict) -> float:
    """Wyżej = lepszy do kopiowania. Zysk i skuteczność na plus; flipperzy, brak danych i bezczynność na minus."""
    if not a["closed"]:
        return -100 + a["swaps"]
    s = a["pnl"] * 10 + (a["win_rate"] or 0) * 20 + min(a["closed"], 15) * 2
    if a["hold_s"] is not None and a["hold_s"] < 20:
        s -= 60  # flipper – kopia dochodzi za późno
    elif a["hold_s"] is not None and a["hold_s"] < 60:
        s -= 15
    if a["last"] and time.time() - a["last"] > DAY:
        s -= 20
    return round(s, 1)


def label(a: dict) -> str:
    if not a["swaps"]:
        return "😴 nie handluje"
    if a["hold_s"] is not None and a["hold_s"] < 20:
        return "⚡ flipper"
    # w memecoinach liczy się wynik: dobry trader często ma mało trafień, ale małe straty i duże wygrane
    if a["closed"] >= 3 and a["pnl"] > 0:
        return "🏆 dobry"
    if a["closed"] >= 3 and a["pnl"] < 0:
        return "📉 traci"
    return "🤔 mało danych"


async def analyze_periods(rpc, wallet: str, max_tx: int = 30, periods=(1, 7, 30)) -> dict:
    """Ranking: jedna porcja transakcji portfela, wynik osobno za 1 / 7 / 30 dni.
    „partial” = dane nie sięgają początku okresu (portfel handluje tak dużo, że ostatnie transakcje to krótszy czas)."""
    now = time.time()
    sigs = await rpc.call("getSignaturesForAddress", [wallet, {"limit": 1000, "commitment": "confirmed"}])
    ok = [x for x in sigs if x.get("err") is None and x.get("blockTime") and now - x["blockTime"] < max(periods) * DAY]
    swaps, fetched = [], 0
    for x in ok:
        if fetched >= max_tx:
            break
        tx = await rpc.transaction(x["signature"])
        fetched += 1
        keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys") or []
        payer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if payer != wallet:
            continue
        sw = parse_swap(tx, wallet)
        if sw and sw.quote_symbol == "SOL":
            swaps.append((x["blockTime"], sw))
    out = {}
    oldest = min((ts for ts, _ in swaps), default=now)
    for d in periods:
        part = [(ts, sw) for ts, sw in swaps if now - ts < d * DAY]
        a = summarize(wallet, part, d, len(ok), fetched)
        a["partial"] = fetched >= max_tx and now - oldest < d * DAY * 0.9
        a["lb_score"] = lb_score(a, d)
        out[d] = a
    return out


def lb_score(a: dict, days: int) -> float:
    """Ocena do rankingu: zysk i % zwrotu, ale też STABILNOŚĆ (dni na plusie) i liczba zamkniętych pozycji.
    Flipperzy (trzymają sekundy) w dół – nie da się ich skopiować z zyskiem."""
    if a["closed"] < 2 or a["pnl"] <= 0:
        return round(-100 + a["pnl"], 2)
    roi = max(min(a["roi"] or 0, 300), -100)
    green = a["green_days"] / a["active_days"] if a["active_days"] else 0
    s = roi * 0.3 + min(a["pnl"], 100) * 3 + green * 40 * (1 if days > 1 else 0.3) + min(a["closed"], 30) + \
        (a["win_rate"] or 0) * 15
    if a["hold_s"] is not None and a["hold_s"] < 20:
        s -= 80
    if a.get("partial"):
        s -= 5
    return round(s, 1)


async def analyze_many(rpc, wallets: list[str], days: float = 3, pause: float = 0.3, progress=None) -> list[dict]:
    out = []
    for i, w in enumerate(wallets):
        try:
            out.append(await asyncio.wait_for(analyze_wallet(rpc, w, days), 90))
        except Exception as e:  # noqa: BLE001
            log.debug("Analiza %s: %s", w, e)
        if progress:
            progress(i + 1)
        await asyncio.sleep(pause)
    out.sort(key=lambda a: -a["score"])
    return out
