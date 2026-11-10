"""Ranking najlepszych traderów Solany (1 / 7 / 30 dni) – liczony przez bota prosto z łańcucha.

Kandydaci: portfele, które właśnie kupowały popularne tokeny (Jupiter: trendy 1 h / 6 h / 24 h) za co najmniej
0,3 SOL, plus portfele z list wszystkich osób w bocie. Każdego bot bada (ostatnie transakcje, które sam podpisał)
i liczy zysk, % zwrotu, skuteczność, czas trzymania i dni na plusie. Odświeżanie co 12 h (zapytania do RPC
są limitowane – to nie jest pełna lista całej sieci, tylko aktywni traderzy z popularnych tokenów).
"""
import asyncio
import json
import logging

from ..db import now_ms
from .parser import parse_swap
from .research import analyze_periods

log = logging.getLogger(__name__)
STATE: dict = {"running": False, "done": 0, "total": 0, "started": None, "finished": None}


def is_memecoin(t: dict) -> bool:
    """Memecoin z platformy startowej albo mały token – tam są traderzy, których warto kopiować
    (duże tokeny jak JUP/ZEC/cbBTC obracają głównie boty i to w USDC)."""
    mid = t.get("id") or ""
    if t.get("launchpad") or mid.endswith(("pump", "bonk", "moon")):
        return True
    mc = t.get("mcap") or 0
    return 0 < mc < 30_000_000 and not t.get("isVerified")


async def discover(s, tokens: int = 18, per_token: int = 15, min_sol: float = 0.3) -> dict[str, str]:
    """{portfel: skąd} – aktywni kupujący popularnych tokenów + portfele z list w bocie."""
    se = s.sol
    found: dict[str, str] = {}
    hits: dict[str, int] = {}
    mints, seen = [], set()
    for interval in ("1h", "6h", "24h"):
        try:
            for t in await se.jup.trending(interval, 50):
                if t.get("id") and t["id"] not in seen and is_memecoin(t):
                    seen.add(t["id"])
                    mints.append((t["id"], t.get("symbol") or "?"))
        except Exception:  # noqa: BLE001
            log.debug("Ranking: trendy %s", interval, exc_info=True)
    for mint, sym in mints[:tokens]:
        try:
            sigs = await se.rpc.call("getSignaturesForAddress", [mint, {"limit": 100, "commitment": "confirmed"}])
        except Exception:  # noqa: BLE001
            continue
        for x in [x for x in sigs if x.get("err") is None][:per_token]:
            try:
                tx = await se.rpc.transaction(x["signature"])
            except Exception:  # noqa: BLE001
                continue
            keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys") or []
            payer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else None
            if not payer:
                continue
            if payer in found:
                hits[payer] = hits.get(payer, 1) + 1  # handluje kilkoma popularnymi tokenami – ciekawszy
                continue
            sw = parse_swap(tx, payer)
            if sw and sw.quote_symbol == "SOL" and sw.quote_amount >= min_sol:
                found[payer] = f"kupował {sym}"
                hits[payer] = 1
            await asyncio.sleep(0.15)
    # najpierw ci, których widać w kilku tokenach (aktywni traderzy, nie jednorazowi kupujący)
    found = dict(sorted(found.items(), key=lambda kv: -hits.get(kv[0], 1)))
    for r in s.db._q("SELECT DISTINCT wallet FROM sol_saved"):
        found.setdefault(r["wallet"], "z listy w bocie")
    return found


async def rebuild(s, max_wallets: int = 90):
    if STATE["running"]:
        return
    STATE.update(running=True, done=0, total=0, started=now_ms())
    try:
        cand = await discover(s)
        # najpierw odkryci z trendów, potem z list (pierwszeństwo dla świeżych aktywnych)
        order = sorted(cand.items(), key=lambda kv: kv[1] == "z listy w bocie")[:max_wallets]  # sort stabilny
        STATE["total"] = len(order)
        for wallet, src in order:
            try:
                res = await asyncio.wait_for(analyze_periods(s.sol.rpc, wallet, max_tx=60), 180)
                for d, a in res.items():
                    s.db._q("INSERT OR REPLACE INTO leaderboard VALUES (?, ?, ?, ?, ?, ?)",
                            (wallet, d, json.dumps(a), a["lb_score"], now_ms(), src))
            except Exception:  # noqa: BLE001
                log.debug("Ranking: %s", wallet, exc_info=True)
            STATE["done"] += 1
            await asyncio.sleep(0.4)  # oszczędzamy limit RPC dla kopiowania
        s.db._q("DELETE FROM leaderboard WHERE updated < ?", (now_ms() - 3 * 24 * 3600 * 1000,))
    finally:
        STATE.update(running=False, finished=now_ms())
        log.info("Ranking traderów odświeżony: %d portfeli", STATE["done"])


def top(s, period: int, sort: str = "score", limit: int = 100) -> list[dict]:
    rows = s.db._q("SELECT * FROM leaderboard WHERE period = ?", (period,))
    out = []
    for r in rows:
        a = json.loads(r["stats"])
        if not a.get("closed"):
            continue
        out.append({"wallet": r["wallet"], "source": r["source"], "updated": r["updated"], "score": r["score"],
                    **{k: a.get(k) for k in ("pnl", "roi", "win_rate", "closed", "swaps", "hold_s", "green_days",
                                             "active_days", "avg_buy", "partial", "last")}})
    key = {"score": lambda x: x["score"], "pnl": lambda x: x["pnl"] or 0, "roi": lambda x: x["roi"] or -1e9,
           "green": lambda x: (x["green_days"] or 0, x["pnl"] or 0)}.get(sort, lambda x: x["score"])
    out.sort(key=key, reverse=True)
    return out[:limit]


async def leaderboard_job(ctx):
    s = ctx.application.bot_data["svc"]
    if getattr(s, "sol", None):
        await rebuild(s)


def start_rebuild(s) -> bool:
    if STATE["running"]:
        return False
    asyncio.create_task(rebuild(s))
    return True


__all__ = ["rebuild", "top", "leaderboard_job", "start_rebuild", "STATE"]
