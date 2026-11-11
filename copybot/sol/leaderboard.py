"""Ranking najlepszych traderów Solany (1 / 7 / 30 dni).

Dwa rodzaje danych:
  🌍 ŚWIAT – gotowe rankingi z serwisów, które śledzą całą sieć (każdy portfel, każda transakcja):
     • Solana Tracker – top traderzy wg zrealizowanego zysku, bez botów arbitrażowych i „jednego strzału”,
       z liczbą dni na plusie / minusie (stabilność) i rankingiem KOL-i (znani traderzy z Twittera),
     • Vybe Network – top traderzy z wykresem zysku dzień po dniu z ostatnich 7 dni.
     Oba mają darmowe klucze (Solana Tracker: 2 500 zapytań / mies., Vybe: 25 000 kredytów / mies.) –
     ranking zużywa kilkanaście zapytań co 6 h, więc darmowy plan wystarcza z dużym zapasem.
  🔎 BOT – ranking liczony przez bota z łańcucha (gdy nie ma kluczy albo dla portfeli z Waszych list):
     aktywni kupujący popularne memecoiny + portfele z list, zysk policzony z ich własnych transakcji.

Do najlepszych światowych bot dolicza z łańcucha czas trzymania – flipperów (sekundy) nie da się
skopiować z zyskiem, więc idą w dół.
"""
import asyncio
import json
import logging
import math
import os
import re
import time

import aiohttp

from ..db import now_ms
from .parser import SOL_MINT, parse_swap
from .research import analyze_periods, analyze_wallet

log = logging.getLogger(__name__)
STATE: dict = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "step": "",
               "errors": {}, "world": None, "chain": None}
PERIODS = (1, 7, 30)
ST_URL = "https://data.solanatracker.io"
VYBE_URL = "https://api.vybenetwork.xyz"
CHAIN_EVERY_MS = 12 * 3600 * 1000
HOLD_TTL_MS = 24 * 3600 * 1000
BOT_TRADES_PER_DAY = 400  # więcej transakcji dziennie = bot / sniper, nie do skopiowania


class SourceError(Exception):
    pass


# ---------- klucze (zmienne na Railway albo wpisane przez admina w aplikacji, trzymane zaszyfrowane) ----------
ENV = {"st": "SOLANATRACKER_API_KEY", "vybe": "VYBE_API_KEY"}


def get_key(s, which: str) -> str:
    env = os.getenv(ENV[which], "").strip()
    if env:
        return env
    enc = s.db.get_state(f"lb_key:{which}")
    if not enc:
        return ""
    try:
        return s.vault.dec(enc) or ""
    except Exception:  # noqa: BLE001
        return ""


def set_key(s, which: str, value: str):
    value = (value or "").strip()
    s.db.set_state(f"lb_key:{which}", s.vault.enc(value) if value else "")


def key_status(s) -> dict:
    return {w: ("env" if os.getenv(ENV[w], "").strip() else "app" if get_key(s, w) else None) for w in ENV}


# ---------- pobieranie ----------
async def _get(session, url: str, params: dict, headers: dict) -> dict:
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
        if r.status in (401, 403):
            raise SourceError("zły albo nieaktywny klucz API")
        if r.status == 429:
            raise SourceError("wyczerpany limit zapytań (spróbuj później)")
        if r.status >= 400:
            raise SourceError(f"błąd serwisu (HTTP {r.status})")
        return await r.json(content_type=None)


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def norm_st(t: dict, days: int, kol: bool = False) -> dict:
    """Wiersz z Solana Tracker → wspólny format."""
    p = t.get("period") or {}
    dd = p.get("days") or {}
    green, red = dd.get("profitable"), dd.get("losing")
    active = p.get("tradingDays") or ((green or 0) + (red or 0)) or None
    counts, toks = t.get("counts") or {}, t.get("tokens") or {}
    ident = t.get("identity") or {}
    wr = _num(t.get("winRate"))
    last = (t.get("timing") or {}).get("lastTrade")
    return {"wallet": t.get("wallet"), "pnl_usd": _num(p.get("realized")), "roi": _num(p.get("roi")),
            "win_rate": wr / 100 if wr is not None else None, "green_days": green, "red_days": red,
            "active_days": active, "trades": counts.get("trades"), "tokens": counts.get("tokensTraded"),
            "closed": toks.get("closed"), "volume_usd": _num(p.get("volume")),
            "avg_buy_usd": _num((t.get("averages") or {}).get("buy")), "invested_usd": _num(t.get("invested")),
            "max_single_usd": _num(dd.get("maxSinglePnl")), "last": last / 1000 if last else None,
            "name": ident.get("name"), "twitter": ident.get("twitter"), "kol": kol or bool(ident.get("name")),
            "src": ["Solana Tracker" + (" · KOL" if kol else "")]}


def _series_value(pt):
    if isinstance(pt, (list, tuple)):
        return _num(pt[-1]) if pt else None
    return _num(pt)


def norm_vybe(t: dict) -> dict:
    m = t.get("metrics") or {}
    series = [v for v in (_series_value(x) for x in (m.get("sevenDayPnl") or [])) if v is not None]
    wr = _num(m.get("winRate"))
    labels = t.get("accountLabels") or []
    best = m.get("bestPerformingToken") or {}
    return {"wallet": t.get("accountAddress"), "pnl_usd": _num(m.get("realizedPnlUsd")),
            "unrealized_usd": _num(m.get("unrealizedPnlUsd")),
            "win_rate": (wr / 100 if wr > 1 else wr) if wr is not None else None, "trades": m.get("tradesCount"),
            "tokens": m.get("uniqueTokensTraded"), "volume_usd": _num(m.get("tradesVolumeUsd")),
            "series": series[-7:] if series else None,
            "green7": sum(1 for v in series if v > 0) if series else None,
            "active7": sum(1 for v in series if v != 0) if series else None,
            "best": best.get("tokenSymbol"), "name": t.get("accountName"),
            "twitter": t.get("accountTwitterUrl"), "kol": any(str(x).lower() == "kol" for x in labels),
            "labels": labels, "src": ["Vybe"]}


async def fetch_st(session, key: str, days: int) -> list[dict]:
    h = {"x-api-key": key}
    params = {"days": days, "sort": "realized", "direction": "desc", "limit": 100, "excludeArbitrage": "true",
              "pnlMode": "strict", "minTrades": 5 if days == 1 else 15,
              "minDays": 1 if days == 1 else 2 if days == 7 else 4}
    if days > 1:
        params["maxSingleTokenPct"] = 70  # bez „jednego szczęśliwego strzału”
    top = await _get(session, f"{ST_URL}/v2/pnl/leaderboard/top", params, h)
    out = [norm_st(t, days) for t in top.get("traders") or [] if t.get("wallet")]
    try:
        kols = await _get(session, f"{ST_URL}/v2/pnl/leaderboard/kols/period",
                          {"period": f"{days}d", "sort": "realized", "direction": "desc", "limit": 50}, h)
        out += [norm_st(t, days, kol=True) for t in kols.get("traders") or [] if t.get("wallet")]
    except SourceError:
        log.debug("Ranking: KOL-e Solana Tracker", exc_info=True)
    return out


async def fetch_vybe(session, key: str, days: int) -> list[dict]:
    data = await _get(session, f"{VYBE_URL}/v4/wallets/top-traders",
                      {"resolution": f"{days}d", "sortByDesc": "realizedPnlUsd", "limit": 100}, {"X-API-KEY": key})
    return [norm_vybe(t) for t in data.get("data") or [] if t.get("accountAddress")]


def merge(rows: list[dict]) -> dict[str, dict]:
    """Ten sam portfel z kilku źródeł → jeden wiersz (pierwsze źródło ma pierwszeństwo, reszta uzupełnia)."""
    out: dict[str, dict] = {}
    for r in rows:
        w = r["wallet"]
        if w not in out:
            out[w] = dict(r)
            continue
        cur = out[w]
        for k, v in r.items():
            if k == "src":
                cur["src"] = cur["src"] + [x for x in v if x not in cur["src"]]
            elif k == "kol":
                cur["kol"] = cur.get("kol") or v
            elif cur.get(k) is None and v is not None:
                cur[k] = v
    return out


def is_bot(a: dict) -> bool:
    days = a.get("active_days") or a.get("active7") or 1
    return bool(a.get("trades")) and a["trades"] / max(days, 1) > BOT_TRADES_PER_DAY


def activity(a: dict, days: int) -> tuple[float, float | None]:
    """(ocena aktywności 0–100, transakcji na dzień). Liczy się: handluje CODZIENNIE (dni z handlem / dni okresu),
    DUŻO (transakcji na dzień, skala logarytmiczna – 50/dzień to maks.) i NIEDAWNO (ostatni ruch).
    Boty z setkami transakcji dziennie dostają 0 – takich nie da się kopiować."""
    act = a.get("active_days") or a.get("active7")
    span = days if a.get("active_days") else (7 if a.get("active7") else days)
    trades = a.get("trades") or a.get("swaps")
    tpd = trades / max(act or 1, 1) if trades else a.get("per_day")
    if a.get("bot") or (tpd and tpd > BOT_TRADES_PER_DAY):
        return 0.0, tpd
    s = (min(act / max(span, 1), 1.0) * 50) if act else 0.0
    if tpd:
        s += min(math.log2(tpd + 1) / math.log2(51), 1.0) * 40
    last = a.get("last")
    if last:
        idle_h = (time.time() - last) / 3600
        s += 10 if idle_h < 6 else 5 if idle_h < 24 else 0
    return round(s, 1), tpd


def world_score(a: dict, days: int) -> float:
    """Ocena: zysk (skala logarytmiczna – $1k i $1M to nie 1000× lepszy trader), % zwrotu, STABILNOŚĆ
    (dni na plusie), skuteczność. Boty, flipperzy i „jeden strzał” w dół."""
    pnl = a.get("pnl_usd") or 0
    if pnl <= 0:
        return round(-100 + pnl / 1e4, 2)
    s = min(math.log10(pnl + 1), 6.5) * 14
    if a.get("roi") is not None:
        s += max(min(a["roi"], 400), -100) * 0.06
    green, active = a.get("green_days"), a.get("active_days")
    if green is None:
        green, active = a.get("green7"), a.get("active7")
    if green is not None and active and active >= 2:
        # dni na plusie względem okresu (min. połowa okresu) – 2 dobre dni z 7 to nie „stabilny” trader
        span = 7 if a.get("green_days") is None else days
        s += min(green / max(active, span * 0.5), 1.0) * 35 * (1 if days > 1 else 0.3)
    if a.get("win_rate") is not None:
        s += a["win_rate"] * 15
    if a.get("bot"):
        s -= 50
    hold = a.get("hold_s")
    if hold is not None and hold < 20:
        s -= 40
    elif hold is not None and hold < 60:
        s -= 10
    if days > 1 and a.get("max_single_usd") and a["max_single_usd"] > 0.7 * pnl:
        s -= 15  # prawie cały zysk z jednego tokena
    last = a.get("last")
    if last:
        idle_h = (time.time() - last) / 3600
        if idle_h > 5 * 24:
            s -= 40  # już nie handluje – kopiowanie nic nie da
        elif idle_h > 48:
            s -= 18
    elif a.get("per_day") is not None and a["per_day"] < 0.5:
        s -= 15  # z łańcucha: prawie nie handluje
    s += activity(a, days)[0] * 0.35  # aktywni (codziennie, dużo, niedawno) wyżej – maks. +35
    return round(s, 1)


async def sol_usd(s) -> float | None:
    try:
        return (await asyncio.wait_for(s.sol.jup.prices([SOL_MINT]), 8)).get(SOL_MINT)
    except Exception:  # noqa: BLE001
        return None


def _old_rows(s, period: int) -> dict[str, dict]:
    return {r["wallet"]: json.loads(r["stats"]) for r in
            s.db._q("SELECT wallet, stats FROM leaderboard WHERE period = ?", (period,))}


async def rebuild_world(s) -> int:
    keys = {w: get_key(s, w) for w in ENV}
    if not any(keys.values()):
        STATE["world"] = None
        return 0
    session = s.sol.jup.session
    px = await sol_usd(s)
    errors: dict[str, str] = {}
    written = 0
    best: dict[str, float] = {}
    for d in PERIODS:
        STATE["step"] = f"🌍 ranking światowy – {d} d"
        rows: list[dict] = []
        if keys["st"]:
            try:
                rows += await fetch_st(session, keys["st"], d)
            except Exception as e:  # noqa: BLE001
                errors["Solana Tracker"] = str(e)
        if keys["vybe"]:
            try:
                rows += await fetch_vybe(session, keys["vybe"], d)
            except Exception as e:  # noqa: BLE001
                errors["Vybe"] = str(e)
        if not rows:
            continue
        old = _old_rows(s, d)
        merged = merge(rows)
        s.db._q("DELETE FROM leaderboard WHERE period = ? AND source LIKE '🌍%'", (d,))
        for w, a in merged.items():
            prev = old.get(w) or {}
            if prev.get("hold_at") and now_ms() - prev["hold_at"] < HOLD_TTL_MS:
                a.update(hold_s=prev.get("hold_s"), hold_at=prev["hold_at"], per_day=prev.get("per_day"))
            a["kind"] = "world"
            a["bot"] = is_bot(a)
            a["pnl"] = a["pnl_usd"] / px if px and a.get("pnl_usd") is not None else None
            a["avg_buy"] = a["avg_buy_usd"] / px if px and a.get("avg_buy_usd") else None
            a["score"] = world_score(a, d)
            best[w] = max(best.get(w, -1e9), a["score"])
            s.db._q("INSERT OR REPLACE INTO leaderboard VALUES (?, ?, ?, ?, ?, ?)",
                    (w, d, json.dumps(a), a["score"], now_ms(), "🌍 " + " + ".join(a["src"])))
            written += 1
        await asyncio.sleep(0.5)
    STATE["errors"] = errors
    STATE["world"] = now_ms() if written else STATE.get("world")
    await check_holds(s, sorted(best, key=lambda w: -best[w])[:25])
    return written


async def check_holds(s, wallets: list[str]):
    """Najlepszym światowym dolicza z łańcucha, ile trzymają tokeny (flipperzy w dół)."""
    todo = []
    for w in wallets:
        r = s.db._q("SELECT stats FROM leaderboard WHERE wallet = ? AND period = 7", (w,))
        a = json.loads(r[0]["stats"]) if r else {}
        if not (a.get("hold_at") and now_ms() - a["hold_at"] < HOLD_TTL_MS):
            todo.append(w)
    for i, w in enumerate(todo):
        STATE["step"] = f"⏱ sprawdzam czas trzymania {i + 1}/{len(todo)}"
        try:
            a = await asyncio.wait_for(analyze_wallet(s.sol.rpc, w, days=3, max_tx=30), 90)
        except Exception:  # noqa: BLE001
            continue
        for r in s.db._q("SELECT period, stats FROM leaderboard WHERE wallet = ?", (w,)):
            st = json.loads(r["stats"])
            if st.get("kind") != "world":
                continue
            st.update(hold_s=a.get("hold_s"), hold_at=now_ms(), per_day=a.get("per_day"))
            st["score"] = world_score(st, r["period"])
            s.db._q("UPDATE leaderboard SET stats = ?, score = ? WHERE wallet = ? AND period = ?",
                    (json.dumps(st), st["score"], w, r["period"]))
        await asyncio.sleep(0.4)


# ---------- ranking liczony przez bota z łańcucha ----------
def is_memecoin(t: dict) -> bool:
    """Memecoin z platformy startowej albo mały token – tam są traderzy, których warto kopiować
    (duże tokeny jak JUP/ZEC/cbBTC obracają głównie boty i to w USDC)."""
    mid = t.get("id") or ""
    if t.get("launchpad") or mid.endswith(("pump", "bonk", "moon")):
        return True
    mc = t.get("mcap") or 0
    return 0 < mc < 30_000_000 and not t.get("isVerified")


async def discover(s, tokens: int = 18, per_token: int = 15, min_sol: float = 0.3, trending: bool = True) -> dict:
    """{portfel: skąd} – aktywni kupujący popularnych memecoinów + portfele z list w bocie."""
    se = s.sol
    found: dict[str, str] = {}
    hits: dict[str, int] = {}
    mints, seen = [], set()
    for interval in (("1h", "6h", "24h") if trending else ()):
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


async def rebuild_chain(s, max_wallets: int = 90, trending: bool = True) -> int:
    STATE["step"] = "🔎 szukam aktywnych traderów"
    cand = await discover(s, trending=trending)
    order = sorted(cand.items(), key=lambda kv: kv[1] == "z listy w bocie")[:max_wallets]  # sort stabilny
    STATE.update(total=len(order), done=0)
    world = {(r["wallet"], r["period"]) for r in s.db._q("SELECT wallet, period FROM leaderboard WHERE source LIKE '🌍%'")}
    for wallet, src in order:
        STATE["step"] = f"🔎 badam portfele {STATE['done'] + 1}/{len(order)}"
        try:
            res = await asyncio.wait_for(analyze_periods(s.sol.rpc, wallet, max_tx=60), 180)
            for d, a in res.items():
                if (wallet, d) in world:
                    continue  # światowe dane są pełniejsze
                a["kind"] = "chain"
                s.db._q("INSERT OR REPLACE INTO leaderboard VALUES (?, ?, ?, ?, ?, ?)",
                        (wallet, d, json.dumps(a), a["lb_score"], now_ms(), src))
        except Exception:  # noqa: BLE001
            log.debug("Ranking: %s", wallet, exc_info=True)
        STATE["done"] += 1
        await asyncio.sleep(0.4)  # oszczędzamy limit RPC dla kopiowania
    STATE["chain"] = now_ms()
    s.db.set_state("lb_chain_at", str(now_ms()))
    return len(order)


async def rebuild(s, force_chain: bool = False):
    if STATE["running"]:
        return
    STATE.update(running=True, done=0, total=0, started=now_ms(), step="")
    try:
        world = await rebuild_world(s)
        last_chain = int(s.db.get_state("lb_chain_at") or 0)
        if force_chain or now_ms() - last_chain > CHAIN_EVERY_MS:
            # z kluczami świat jest pełny – bot bada już tylko portfele z Waszych list (oszczędza RPC)
            await rebuild_chain(s, max_wallets=90 if not world else 60, trending=not world)
        s.db._q("DELETE FROM leaderboard WHERE updated < ?", (now_ms() - 3 * 24 * 3600 * 1000,))
        try:
            auto_name(s)  # traderzy z nic niemówiącymi nazwami dostają nazwę z rankingu
        except Exception:  # noqa: BLE001
            log.exception("Ranking: nazwy")
        log.info("Ranking traderów odświeżony: świat %d wierszy, bot %d portfeli", world, STATE["done"])
    except Exception:  # noqa: BLE001
        log.exception("Ranking traderów")
    finally:
        STATE.update(running=False, finished=now_ms(), step="")


# ---------- odczyt ----------
FIELDS = ("pnl", "pnl_usd", "roi", "win_rate", "closed", "swaps", "trades", "tokens", "hold_s", "green_days",
          "active_days", "red_days", "green7", "active7", "series", "avg_buy", "avg_buy_usd", "partial", "last",
          "name", "twitter", "kol", "bot", "best", "per_day", "volume_usd", "unrealized_usd")


def top(s, period: int, sort: str = "score", limit: int = 100, kind: str | None = None) -> list[dict]:
    rows = s.db._q("SELECT * FROM leaderboard WHERE period = ?", (period,))
    out = []
    for r in rows:
        a = json.loads(r["stats"])
        k = a.get("kind") or "chain"
        if kind and k != kind:
            continue
        if k == "chain" and not a.get("closed"):
            continue
        act, tpd = activity(a, period)
        # ocena światowa liczona na bieżąco – „ostatni ruch” i aktywność starzeją się z godziny na godzinę
        score = world_score(a, period) if k == "world" else r["score"]
        out.append({"wallet": r["wallet"], "source": r["source"], "updated": r["updated"], "score": score,
                    "kind": k, "activity": act, "tpd": tpd, **{f: a.get(f) for f in FIELDS}})

    def green(x):
        g, act = (x["green_days"], x["active_days"]) if x["green_days"] is not None else (x["green7"], x["active7"])
        return ((g or 0) / act if act else 0, g or 0, x["pnl"] or 0)
    key = {"score": lambda x: x["score"], "pnl": lambda x: x["pnl"] if x["pnl"] is not None else (x["pnl_usd"] or 0) / 150,
           "roi": lambda x: x["roi"] if x["roi"] is not None else -1e9, "green": green,
           "win": lambda x: (x["win_rate"] or 0, x["pnl"] or 0),
           # najaktywniejsi na górze – ale tylko ci na plusie (aktywny i tracący to najgorszy wybór)
           "active": lambda x: ((x["pnl"] or x["pnl_usd"] or 0) > 0, x["activity"], x["score"])}
    sorts = [k for k in dict.fromkeys(str(sort or "score").split(",")) if k in key][:6] or ["score"]
    if len(sorts) == 1:
        out.sort(key=key[sorts[0]], reverse=True)
        return out[:limit]
    # kilka filtrów naraz: miejsce w każdym z nich → średnia (0 = pierwszy, 1 = ostatni); wygrywa ten, kto jest
    # wysoko we WSZYSTKICH wybranych naraz, a nie tylko w jednym
    n = max(len(out) - 1, 1)
    pos = {id(x): 0.0 for x in out}
    for k in sorts:
        for i, x in enumerate(sorted(out, key=key[k], reverse=True)):
            pos[id(x)] += i / n
    for x in out:
        x["combo"] = round((1 - pos[id(x)] / len(sorts)) * 100, 1)
    out.sort(key=lambda x: (x["combo"], x["score"]), reverse=True)
    return out[:limit]


def counts(s, period: int) -> dict:
    c = {"world": 0, "chain": 0}
    for r in s.db._q("SELECT source FROM leaderboard WHERE period = ?", (period,)):
        c["world" if (r["source"] or "").startswith("🌍") else "chain"] += 1
    return c


async def leaderboard_job(ctx):
    s = ctx.application.bot_data["svc"]
    if getattr(s, "sol", None):
        await rebuild(s)


def start_rebuild(s, force_chain: bool = False) -> bool:
    if STATE["running"]:
        return False
    asyncio.create_task(rebuild(s, force_chain))
    return True


__all__ = ["rebuild", "top", "leaderboard_job", "start_rebuild", "STATE", "get_key", "set_key", "key_status",
           "auto_name", "rank_name", "is_generic"]


# ---------- nazwy traderów z rankingu („🎯 Top 5 skuteczność 7d”) ----------
SORT_NAMES = {"score": ("🏆", "ocena"), "win": ("🎯", "skuteczność"), "green": ("📅", "stabilność"),
              "pnl": ("💰", "zysk"), "roi": ("📈", "zwrot"), "active": ("🔥", "aktywność")}
_W = r"(ocena|skuteczność|stabilność|zysk|zwrot|aktywność)"
_AUTO = re.compile(r"Top \d+ " + _W + r"(\+" + _W + r")* \d+d")
_SHORT = re.compile(r"^(🌍 |Portfel )?[1-9A-HJ-NP-Za-km-z]{3,6}(…|\.\.\.)[1-9A-HJ-NP-Za-km-z]{3,6}$")


def rank_name(sort: str, rank: int, period: int, name: str | None = None) -> str:
    keys = [k for k in dict.fromkeys(str(sort or "score").split(",")) if k in SORT_NAMES] or ["score"]
    emo = "".join(SORT_NAMES[k][0] for k in keys)
    word = "+".join(SORT_NAMES[k][1] for k in keys)  # np. „🏆🔥 Top 3 ocena+aktywność 7d”
    tag = f"{emo} Top {rank} {word} {period}d"
    return (f"⭐ {name[:14]} · " + tag) if name else tag


def is_generic(name: str | None, wallet: str = "") -> bool:
    """Nazwa nic nie mówi: pusta, sam adres („9rXz…B2jJ”) albo nasza automatyczna z rankingu."""
    if not name:
        return True
    n = name.strip()
    return bool(_SHORT.match(n)) or n in (wallet, wallet[:4] + "…" + wallet[-4:]) or bool(_AUTO.search(n))


def rank_table(s) -> dict[str, tuple]:
    """{portfel: (miejsce, sortowanie, okres, nazwa KOL)} – najlepsze miejsce w dowolnym rankingu.
    Pierwszeństwo: świat przed rankingiem bota, potem niższe miejsce, ocena i 7 dni przy remisie."""
    best: dict[str, tuple] = {}
    for ki, kind in enumerate(("world", "chain")):
        for pi, period in enumerate((7, 30, 1)):
            for si, sort in enumerate(SORT_NAMES):
                need = {"roi": "roi", "win": "win_rate", "pnl": "pnl"}.get(sort)
                rows = [r for r in top(s, period, sort, kind=kind)
                        if not (need and r.get(need) is None)
                        and not (sort == "green" and r.get("green_days") is None and r.get("green7") is None)
                        and not (sort == "active" and not r.get("activity"))]
                for i, r in enumerate(rows):  # miejsce liczone tylko wśród tych, którzy mają tę miarę
                    if r.get("bot") or (r.get("score") or 0) <= -100:
                        continue
                    key = (ki, i, si, pi)
                    w = r["wallet"]
                    if w not in best or key < best[w][0]:
                        best[w] = (key, i + 1, sort, period, r.get("name"))
    return {w: v[1:] for w, v in best.items()}


def custom_part(name: str | None, wallet: str = "") -> str | None:
    """Własna część nazwy (np. „Cupsey” z „🏆 Top 3 ocena 7d · Cupsey”) – bez naszego dopisku i adresu."""
    if not name:
        return None
    parts = [x.strip() for x in name.split(" · ")]
    keep = [x for x in parts if not _AUTO.search(x) and not (x.startswith("⭐ ") and len(parts) > 1 and parts.index(x) == 0)]
    out = " · ".join(keep).strip()
    return None if not out or is_generic(out, wallet) else out


def auto_name(s, uid: int | None = None, refresh: bool = False, table: dict | None = None,
              everyone: bool = False) -> int:
    """Nadaje nazwy z rankingu. Domyślnie tylko nic niemówiącym („9rXz…B2jJ”); refresh=True – także odświeża
    nasze wcześniejsze nazwy na aktualne miejsce; everyone=True – każdemu z rankingu, a własną nazwę
    zostawia na końcu („🏆 Top 3 ocena 7d · Cupsey”). Zwraca, ile zmieniono."""
    table = table if table is not None else rank_table(s)
    if not table:
        return 0

    def new_name(w: str, cur: str | None) -> str | None:
        if w not in table:
            return None
        auto = bool(_AUTO.search(cur or ""))
        if not everyone and not (is_generic(cur, w) and (refresh or not auto)):
            return None
        rank, sort, period, kol = table[w]
        tag = rank_name(sort, rank, period, kol)
        own = custom_part(cur, w)
        if own and kol and own.lower().startswith(kol.lower()[:10]):
            own = None
        return f"{tag} · {own[:16]}" if own else tag

    n = 0
    users = [uid] if uid else [r["tg_id"] for r in s.db._q("SELECT DISTINCT tg_id FROM sol_saved")]
    for u in users:
        for r in s.db._q("SELECT wallet, name FROM sol_saved WHERE tg_id = ?", (u,)):
            new = new_name(r["wallet"], r["name"])
            if new and new != r["name"]:
                s.db._q("UPDATE sol_saved SET name = ? WHERE tg_id = ? AND wallet = ?", (new, u, r["wallet"]))
                n += 1
    for r in s.db._q("SELECT id, wallet, name FROM leaders WHERE source = 'sol' AND status = 'active'"):
        new = new_name(r["wallet"], r["name"])
        if new and new != r["name"]:
            s.db.update_leader(r["id"], name=new)
            n += 0 if uid else 1
    return n


async def name_everyone_once(ctx):
    """Jednorazowo po wdrożeniu: każdy trader z rankingu u każdej osoby dostaje nazwę z rankingu."""
    s = ctx.application.bot_data["svc"]
    if s.db.get_state("auto_name_all_v1"):
        return
    try:
        n = auto_name(s, everyone=True)
        s.db.set_state("auto_name_all_v1", str(now_ms()))
        log.info("Ranking: nazwano traderów wg rankingu u wszystkich – zmian: %d", n)
    except Exception:  # noqa: BLE001
        log.exception("Ranking: nazwy dla wszystkich")
