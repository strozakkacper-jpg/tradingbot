"""API mini aplikacji (Telegram Mini App) dla kopiowania portfeli Solany.

Każde zapytanie jest podpisane przez Telegrama (nagłówek X-Init-Data) – sprawdza to `auth` z web.py.
Odpowiedzi to czyste dane (JSON); wygląd robi copybot/webapp/sol.html.
"""
import asyncio
import html
import json
import logging

from aiohttp import web

from ..bot import admin_ids
from ..db import now_ms
from . import admin as adm
from .engine import Skip, short
from .parser import LAMPORTS, SOL_MINT
from .risk import risk
from .ui import buy_txt, mode_txt, valid_address

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000
MAX_COPIED = 30  # bezpiecznik: każdy kopiowany portfel to Twoje zakupy – przy setkach saldo zniknęłoby w minuty

# pole -> (typ, min, max) – nic spoza tej listy nie da się zmienić z aplikacji
SETTINGS = {
    "sol_buy_mode": ("mode", None, None), "sol_buy": (float, 0.001, 100), "sol_pct": (float, 0.1, 1000),
    "sol_mult": (float, 0.01, 100), "sol_max": (float, 0, 1000), "sol_slippage_bps": (int, 50, 5000),
    "sol_tp": (float, 0, 10000), "sol_sl": (float, 0, 100), "sol_day_limit": (float, 0, 1000),
    "sol_safe": (bool, None, None), "sol_x2": (bool, None, None), "sol_no_add": (bool, None, None),
    "sol_min_lead": (float, 0, 100), "sol_min_hold": (float, 0, 3600), "sol_priority": (int, 10_000, 10_000_000),
    "sol_paper_start": (float, 0.01, 1000), "sol_notify": ("notify", None, None),
    "sol_consensus": (int, 0, 5), "sol_cons_window": (float, 1, 120), "sol_boost": (float, 1, 5),
    "sol_trail": (float, 0, 90), "sol_auto_rank": (bool, None, None), "chat_notify": (bool, None, None),
    "sol_turbo": (bool, None, None),
    "sol_min_buy": (float, 0, 10), "sol_max_pos": (int, 0, 100), "sol_streak": (int, 0, 20),
    "sol_max_mcap": (float, 0, 1e10), "sol_min_age": (float, 0, 100000), "sol_max_age": (float, 0, 100000),
    "sol_time_exit": (float, 0, 100000), "sol_dev_exit": (bool, None, None), "sol_smart": (bool, None, None), "sol_autocfg": (bool, None, None),
    "sol_report": (bool, None, None),
}


class ApiError(Exception):
    pass


def _trade(s, r) -> dict:
    return {"ts": r["ts"], "side": r["side"], "mint": r["mint"], "symbol": r["symbol"] or short(r["mint"]),
            "sol": r["sol"], "tokens": r["tokens"], "status": r["status"], "info": r["info"],
            "latency_ms": r["latency_ms"], "sig": r["sig"], "pnl": r["pnl"] if "pnl" in r.keys() else None}


def recommended(balance: float, typical_buy: float = 1.0, live: bool = False) -> dict:
    """Ustawienia dobrane do salda i do tego, za ile zwykle kupują Twoi traderzy.

    Zasady (wnioski z prawdziwych sesji):
    - % kwoty tradera zamiast stałej kwoty – mocne wejście tradera = większa pozycja, jego test za grosze = grosze
    - zwykła pozycja ok. 6% kapitału, mocne wejście maks. 12%, ok. 10–15 pozycji naraz
    - minimalna kwota zakupu, żeby opłaty (2 × sieć + napiwek) nie zjadały małych transakcji
    - filtr tylko prawdziwych pułapek (świeże tokeny z Pump.fun przepuszcza – to one robią ×10)
    - wygrane rosną: zwrot wkładu przy ×2, ruchomy stop 40% dopiero po ×2, stop-loss tylko awaryjny −60%
    - bezpieczniki: limit dnia 25% kapitału, przerwa po 5 stratach z rzędu, limit pozycji
    """
    usable = max(balance - 0.01, 0.01)  # 0,01 SOL bot zostawia na opłaty i depozyty

    def r(x):
        return float(f"{x:.2g}")
    tip = 200_000  # 0,0002 SOL – opłata za szybkość / napiwek Jito
    fees = 2 * (5_000 + tip) / LAMPORTS  # koszt kupna + sprzedaży
    target = usable * 0.06
    typical = max(typical_buy or 1.0, 0.05)
    pct = r(min(max(target / typical * 100, 0.1), 100))
    min_buy = r(min(max(0.005, fees * 20), target * 0.5))
    return {"sol_buy_mode": "pct", "sol_pct": pct, "sol_buy": r(target), "sol_max": r(usable * 0.12),
            "sol_min_buy": min_buy, "sol_max_pos": int(min(max(usable / max(target, min_buy), 4), 15)),
            "sol_day_limit": r(max(usable * 0.25, 0.005)), "sol_streak": 5,
            "sol_no_add": 0, "sol_min_lead": 0, "sol_min_hold": 20, "sol_safe": 1,
            "sol_x2": 1, "sol_tp": 0, "sol_sl": 60, "sol_trail": 40, "sol_slippage_bps": 2000,
            "sol_priority": tip, "sol_turbo": 1 if live else 0,
            "sol_boost": 2, "sol_consensus": 0, "sol_auto_rank": 1}


def typical_trader_buy(s, uid: int) -> float:
    """Za ile zwykle kupują Twoi włączeni traderzy (mediana z badania), domyślnie 1 SOL."""
    vals = sorted(it["stats"]["avg_buy"] for it in saved_list(s, uid)
                  if it["status"] == "copy" and it.get("stats") and it["stats"].get("avg_buy"))
    return vals[len(vals) // 2] if vals else 1.0


def today_stats(s, uid: int, paper: bool) -> dict:
    """Podsumowanie dnia (od północy): kupna, sprzedaże, ile z zyskiem, pominięte."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    since = int(datetime.now(ZoneInfo(s.cfg.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp() * 1000)
    r = s.db._q("""SELECT SUM(side = 'buy' AND status = 'filled') buys, SUM(side = 'sell' AND status = 'filled') sells,
                   SUM(side = 'sell' AND status = 'filled' AND pnl > 0) wins, SUM(status != 'filled') skipped
                   FROM sol_trades WHERE tg_id = ? AND paper = ? AND ts >= ?""", (uid, int(paper), since))[0]
    return {k: r[k] or 0 for k in ("buys", "sells", "wins", "skipped")}


def last_leader_move(s, uid: int) -> int | None:
    """Kiedy któryś z kopiowanych traderów ostatnio coś kupił/sprzedał (wg portfeli wzorcowych)."""
    r = s.db._q("""SELECT MAX(t.ts) ts FROM sol_trades t JOIN follows f ON f.leader_id = -t.tg_id
                   WHERE f.tg_id = ? AND t.tg_id < 0""", (uid,))
    return r[0]["ts"] if r else None


def settings_of(u) -> dict:
    return {k: (bool(u[k]) if t is bool else u[k]) for k, (t, *_r) in SETTINGS.items()} | {
        "summary": buy_txt(u), "sol_autocfg_at": u["sol_autocfg_at"] if "sol_autocfg_at" in u.keys() else None,
        "presets": [float(x) for x in (u["sol_presets"] or "0.05,0.1,0.5,1").split(",") if x]}


def copied_count(s, uid: int) -> int:
    from .axiom import sol_leaders
    return sum(1 for _, c in sol_leaders(s, uid) if c)


def check_copy_limit(s, uid: int, adding: int = 1):
    if copied_count(s, uid) + adding > MAX_COPIED:
        raise ApiError(f"Możesz kopiować maks. {MAX_COPIED} portfeli naraz – każdy ich zakup to Twój zakup. "
                       "Resztę ustaw na 👁 obserwuj albo zostaw na liście Zapisane.")


def saved_list(s, uid: int) -> list[dict]:
    """Jedna lista traderów: zapisani (np. z Axiom) + śledzeni. Kolejność: ⭐ priorytet, włączeni, aktywni."""
    follows = {r["wallet"]: r for r in s.db._q(
        """SELECT l.id, l.wallet, l.name, f.copy, f.priority FROM follows f JOIN leaders l ON l.id = f.leader_id
           WHERE f.tg_id = ? AND l.source = 'sol' AND l.status = 'active'""", (uid,))}
    out, seen = [], set()
    for i, r in enumerate(s.db.saved_wallets(uid)):
        f = follows.get(r["wallet"])
        seen.add(r["wallet"])
        stats = json.loads(r["stats"]) if r["stats"] else None
        out.append({"wallet": r["wallet"], "name": r["name"] or (f["name"] if f else None), "emoji": r["emoji"],
                    "stats": stats and {k: stats.get(k) for k in ("swaps", "closed", "pnl", "win_rate", "hold_s",
                                                                  "per_day", "avg_buy")},
                    "score": r["score"], "label": research_label(stats) if stats else None,
                    "status": (("copy" if f["copy"] else "watch") if f else None), "lid": f["id"] if f else None,
                    "priority": bool(r["priority"] or (f and f["priority"])), "last_active": r["last_active"],
                    "checked_at": r["checked_at"], "order": i,
                    "hold": s.sol.hold_stats(f["id"]) if f else None})
    for w, f in follows.items():
        if w not in seen:
            out.append({"wallet": w, "name": f["name"], "emoji": None, "status": "copy" if f["copy"] else "watch",
                        "lid": f["id"], "priority": bool(f["priority"]), "last_active": None, "checked_at": None,
                        "order": -1, "hold": s.sol.hold_stats(f["id"])})
    for x in out:
        x.setdefault("stats", None)
        x.setdefault("score", None)
        x.setdefault("label", None)
    out.sort(key=lambda x: (not x["priority"], x["status"] != "copy", -(x["score"] if x["score"] is not None else -1e9),
                            -(x["last_active"] or 0), x["order"]))
    return out


def ranking(s, uid: int, paper: bool) -> list[dict]:
    """Tabela traderów: ile zarobiłeś na kopiowaniu każdego (7 dni / od początku) + wynik z badania."""
    week = now_ms() - 7 * DAY_MS
    mine = {r["wallet"]: r for r in s.db._q(
        """SELECT l.wallet, COUNT(*) n, SUM(t.side = 'sell') sells, SUM(t.pnl > 0) wins,
                  SUM(COALESCE(t.pnl, 0)) pnl_all, SUM(CASE WHEN t.ts >= ? THEN COALESCE(t.pnl, 0) ELSE 0 END) pnl_7d,
                  SUM(t.side = 'buy') buys
           FROM sol_trades t JOIN leaders l ON l.id = t.leader_id
           WHERE t.tg_id = ? AND t.paper = ? AND t.status = 'filled' GROUP BY l.wallet""", (week, uid, int(paper)))}
    out = []
    for it in saved_list(s, uid):
        m = mine.get(it["wallet"])
        out.append({**{k: it[k] for k in ("wallet", "name", "emoji", "status", "priority", "lid", "label", "score",
                                           "last_active")},
                    "research": it["stats"],
                    "copies": (m["buys"] or 0) if m else 0, "sells": (m["sells"] or 0) if m else 0,
                    "wins": (m["wins"] or 0) if m else 0,
                    "pnl_7d": m["pnl_7d"] if m else 0.0, "pnl_all": m["pnl_all"] if m else 0.0})
    return out


def research_label(stats: dict) -> str:
    from .research import label
    return label(stats)


_RESEARCH: dict[int, dict] = {}
_CHAT_LAST_SENT: dict[int, float] = {}  # antyspam: ostatnia wiadomość użytkownika
_CHAT_PING: dict[int, float] = {}  # kiedy ostatnio powiadomiliśmy daną osobę o czacie


def chat_msg(s, r, me: int) -> dict:
    name = r["nickname"] or (f"@{r['username']}" if r["username"] else str(r["tg_id"]))
    return {"id": r["id"], "uid": r["tg_id"], "name": name, "role": adm.role_of(s, r["tg_id"]), "text": r["text"],
            "ts": r["ts"], "mine": r["tg_id"] == me}


async def chat_notify_others(s, sender, text: str):
    """Krótkie powiadomienie w czacie z botem dla pozostałych (maks. raz na 2 min na osobę – bez spamu)."""
    import time as _t
    name = html.escape(adm.display_name(sender))
    url = f"{s.cfg.webapp_url}/app?tab=chat" if s.cfg.webapp_url.startswith("https://") else None
    for u in s.db.approved_users():
        if u["tg_id"] == sender["tg_id"] or not u["chat_notify"]:
            continue
        if _t.time() - _CHAT_PING.get(u["tg_id"], 0) < 120:
            continue
        _CHAT_PING[u["tg_id"]] = _t.time()
        try:
            await s.sol.notify(u["tg_id"], f"💬 <b>{name}</b> na czacie: {html.escape(text[:200])}", html=True,
                               buttons=[[("💬 Otwórz czat", "webapp:" + url)]] if url else None)
        except TypeError:
            await s.sol.notify(u["tg_id"], f"💬 {adm.display_name(sender)} na czacie: {text[:200]}")
        except Exception:  # noqa: BLE001
            log.debug("Powiadomienie o czacie", exc_info=True)


def start_research(s, uid: int) -> bool:
    st = _RESEARCH.get(uid)
    if (st and st["running"]) or not hasattr(s.sol.rpc, "call"):
        return False
    wallets = [it["wallet"] for it in saved_list(s, uid)]
    _RESEARCH[uid] = {"total": len(wallets), "done": 0, "running": True}

    async def run():
        try:
            await research_all(s, uid, wallets)
        finally:
            _RESEARCH[uid]["running"] = False
    asyncio.create_task(run())
    return True


async def research_all(s, uid: int, wallets: list[str] | None = None):
    from .research import analyze_wallet
    wallets = wallets if wallets is not None else [it["wallet"] for it in saved_list(s, uid)]
    st = _RESEARCH.setdefault(uid, {"total": len(wallets), "done": 0, "running": True})
    for w in wallets:
        try:
            s.db.set_research(w, await asyncio.wait_for(analyze_wallet(s.sol.rpc, w), 90))
        except Exception:  # noqa: BLE001
            log.debug("Badanie %s", w, exc_info=True)
        st["done"] = st.get("done", 0) + 1
        await asyncio.sleep(0.5)  # nie zabieramy limitu RPC kopiowaniu


BAD_LABELS = ("📉 traci", "😴 nie handluje", "⚡ flipper")


def apply_rank(s, uid: int) -> tuple[list[str], list[str]]:
    """Po badaniu: wyłącza kopiowanych, którzy tracą / nie handlują / flipują, i włącza najlepszych na ich miejsce
    (tylu, ilu było kopiowanych – min. 3). ⭐ priorytetowych nie wyłącza."""
    items = saved_list(s, uid)
    target = max(sum(1 for it in items if it["status"] == "copy"), 3)
    off = []
    for it in items:
        if it["status"] == "copy" and it["label"] in BAD_LABELS and not it["priority"]:
            switch(s, uid, it["wallet"], False)
            off.append(it["name"] or short(it["wallet"]))
    on = enable_best(s, uid, max(target - copied_count(s, uid), 0))
    return off, on


async def auto_rank(s, u) -> tuple[list[str], list[str]]:
    """Automatyczna ocena: zbadaj całą listę, popraw skład kopiowanych traderów i daj znać."""
    uid = u["tg_id"]
    if not saved_list(s, uid):
        return [], []
    _RESEARCH[uid] = {"total": len(saved_list(s, uid)), "done": 0, "running": True}
    try:
        await research_all(s, uid)
    finally:
        _RESEARCH[uid]["running"] = False
    off, on = apply_rank(s, uid)
    if off or on:
        await s.sol.say(u, "auto", "🤖 <b>AUTOMATYCZNA OCENA TRADERÓW</b>\n"
                        + (f"⚪ Wyłączeni (tracili / nie handlują / flipują): {html.escape(', '.join(off))}\n" if off else "")
                        + (f"🟢 Włączeni (najlepsze wyniki z 3 dni): {html.escape(', '.join(on))}" if on else
                           "Brak nowych traderów z oceną 🏆 – kopiujesz mniej osób."))
    return off, on


def remove_inactive(s, uid: int, days: float = 7) -> int:
    """Usuwa z listy sprawdzonych traderów, którzy od X dni nic nie kupili – tylko niewłączonych."""
    cutoff = now_ms() - int(days * DAY_MS)
    n = 0
    for it in saved_list(s, uid):
        if it["status"] or it["priority"] or not it["checked_at"]:
            continue  # kopiowanych, obserwowanych, ⭐ i niesprawdzonych nie ruszamy
        if not it["last_active"] or it["last_active"] < cutoff:
            s.db.unsave_wallet(uid, it["wallet"])
            n += 1
    return n


def enable_best(s, uid: int, n: int = 5) -> list[str]:
    """Włącza N najlepszych wg badania (🏆, bez flipperów), w ramach limitu kopiowanych."""
    best = [it for it in saved_list(s, uid) if it["status"] != "copy" and it["label"] == "🏆 dobry"]
    best.sort(key=lambda it: -(it["score"] or 0))
    room = max(MAX_COPIED - copied_count(s, uid), 0)
    on = []
    for it in best[:min(n, room)]:
        switch(s, uid, it["wallet"], True)
        on.append(it["name"] or short(it["wallet"]))
    return on


def ensure_saved(s, uid: int, wallet: str):
    """Trader wyłączony nie znika – zostaje na liście (z nazwą tradera)."""
    if not any(r["wallet"] == wallet for r in s.db.saved_wallets(uid)):
        rows = s.db._q("SELECT name FROM leaders WHERE source = 'sol' AND wallet = ? AND status = 'active'", (wallet,))
        s.db.save_wallets(uid, [(wallet, rows[0]["name"] if rows else None, None)], "manual")


def switch(s, uid: int, wallet: str, on: bool) -> str:
    if on:
        fr = s.db._q("""SELECT f.copy FROM follows f JOIN leaders l ON l.id = f.leader_id
                        WHERE f.tg_id = ? AND l.wallet = ? AND l.source = 'sol'""", (uid, wallet))
        if not (fr and fr[0]["copy"]):
            check_copy_limit(s, uid)
        follow_saved(s, uid, wallet, True)
        return "🟢 Włączony – bot kopiuje jego ruchy"
    ensure_saved(s, uid, wallet)
    for r in s.db._q("SELECT id FROM leaders WHERE source = 'sol' AND wallet = ?", (wallet,)):
        s.db.unfollow(uid, r["id"])
    return "⚪ Wyłączony – zostaje na liście"


_SCANS: dict[int, dict] = {}


async def last_own_trade(s, wallet: str) -> int | None:
    """Czas (ms) ostatniej udanej transakcji PODPISANEJ przez ten portfel – spam od innych się nie liczy."""
    sigs = await s.sol.rpc.call("getSignaturesForAddress", [wallet, {"limit": 100, "commitment": "confirmed"}])
    for x in [x for x in sigs if x.get("err") is None][:6]:
        tx = await s.sol.rpc.transaction(x["signature"])
        keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys") or []
        first = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if first == wallet:
            return int((x.get("blockTime") or tx.get("blockTime") or 0) * 1000) or None
    return None


def start_scan(s, uid: int, force: bool = False) -> bool:
    st = _SCANS.get(uid)
    if (st and st["running"]) or not hasattr(s.sol.rpc, "call"):
        return False
    asyncio.create_task(scan_activity(s, uid, 0 if force else 30 * 60 * 1000))
    return True


async def scan_activity(s, uid: int, only_stale_ms: int = 30 * 60 * 1000):
    """W tle: sprawdza aktywność portfeli z listy (powoli, żeby nie zabrać limitu RPC kopiowaniu)."""
    items = [it for it in saved_list(s, uid) if not it["checked_at"] or now_ms() - it["checked_at"] > only_stale_ms]
    st = _SCANS[uid] = {"total": len(items), "done": 0, "running": True}
    try:
        for it in items:
            try:
                s.db.set_activity(it["wallet"], await asyncio.wait_for(last_own_trade(s, it["wallet"]), 30))
            except Exception:  # noqa: BLE001
                log.debug("Aktywność %s", it["wallet"], exc_info=True)
            st["done"] += 1
            await asyncio.sleep(0.6)
    finally:
        st["running"] = False


def follow_saved(s, uid: int, wallet: str, copy: bool) -> int:
    """Z listy Zapisane do śledzonych – z nazwą i emoji z importu."""
    from .axiom import _ensure_leader
    row = next((r for r in s.db.saved_wallets(uid) if r["wallet"] == wallet), None)
    lid = _ensure_leader(s, uid, wallet)
    lead = s.db.get_leader(lid)
    if row and row["name"] and lead["name"].startswith("Portfel "):
        s.db.update_leader(lid, name=f"{row['emoji'] + ' ' if row['emoji'] else ''}{row['name']}"[:40])
    s.db.follow(uid, lid, copy)
    if row and row["priority"]:
        s.db.set_priority(uid, wallet, True)
    s.sol.wallet(uid)
    return lid


def own_amount(fr) -> dict:
    return {"sol": fr["sol_buy"] if fr else None, "pct": fr["sol_pct"] if fr else None}


async def state(s, u) -> dict:
    from .axiom import GLOSSARY, HELP, portfolio, sol_leaders
    se = s.sol
    uid = u["tg_id"]
    se.wallet(uid)
    live = se.is_live(u)
    pf = await portfolio(s, u)
    try:
        info = await se.jup.token_full(SOL_MINT)
        sol_ch = (info or {}).get("stats24h", {}).get("priceChange")
    except Exception:  # noqa: BLE001
        sol_ch = None
    day_ago = now_ms() - DAY_MS
    wallets = []
    leaders = sol_leaders(s, uid)
    reps = await asyncio.gather(*(asyncio.wait_for(se.report(ld["id"]), 5) for ld, _ in leaders),
                                return_exceptions=True)
    for (lead, copy), rep in zip(leaders, reps):
        n = s.db._q("SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND leader_id = ? AND status = 'filled' "
                    "AND ts >= ?", (uid, lead["id"], day_ago))[0]["n"]
        ok = isinstance(rep, dict)
        fr = s.db.follow_row(uid, lead["id"])
        wallets.append({"id": lead["id"], "name": lead["name"], "wallet": lead["wallet"], "copy": copy, "n24": n,
                        "hold": se.hold_stats(lead["id"]),
                        "priority": bool(fr and fr["priority"]),
                        "result": rep["total"] * 100 if ok else None,
                        "spark": [v for _, v in rep["series"]][-30:] if ok else [],
                        "own": own_amount(fr)})
    wallets.sort(key=lambda w: (not w["priority"], not w["copy"]))
    start = se.paper_start(uid)
    series = s.db.sol_equity_series(uid, not live, now_ms() - 7 * DAY_MS) + [(now_ms(), pf["total"])]
    last = s.db._q("SELECT latency_ms FROM sol_trades WHERE tg_id = ? AND latency_ms IS NOT NULL ORDER BY id DESC "
                   "LIMIT 1", (uid,))
    return {
        "has_pin": bool(u["pin_hash"]),
        "user": {"id": uid, "admin": uid in admin_ids(s), "role": adm.role_of(s, uid),
                 "nickname": u["nickname"] if "nickname" in u.keys() else None, "username": u["username"]},
        "live": live, "active": bool(u["active"]), "paused": s.db.get_state("paused") == "1",
        "sol_usd": pf["sol_usd"], "sol_change": sol_ch, "sol": pf["sol"], "total": pf["total"],
        "demo_start": start, "result": (pf["total"] / start - 1) * 100 if not live else None,
        "session_start": u["session_start"] if not u["session_end"] else None, "session_sol0": u["session_sol0"],
        "today_pnl": se.day_pnl(uid, not live),
        "today": today_stats(s, uid, not live),
        "positions": [{k: p[k] for k in ("mint", "symbol", "tokens", "value", "pnl", "cost")} for p in pf["positions"]],
        "wallets": wallets, "feed": [_trade(s, r) for r in se.history(uid, 30)],
        "series": [[t, v] for t, v in series], "last_latency_ms": last[0]["latency_ms"] if last else None,
        "settings": settings_of(u),
        "chat_last_id": s.db.chat_last_id(),
        "watcher": {k: v for k, v in adm.watcher_status(s).items() if k in ("connected", "watched", "last_swap")},
        "last_leader_move": last_leader_move(s, uid),
        "default_amount": mode_txt(u["sol_buy_mode"], u["sol_buy"], u["sol_pct"], u["sol_mult"]),
        "help": HELP, "glossary": GLOSSARY,
    }


async def wallet(s, u, lid: int) -> dict:
    lead = s.db.get_leader(lid)
    if not lead or lead["status"] != "active" or lead["source"] != "sol":
        raise ApiError("Ten portfel nie jest już śledzony")
    uid = u["tg_id"]
    fr = s.db.follow_row(uid, lid)
    try:
        rep = await asyncio.wait_for(s.sol.report(lid), 8)
        stats = {k: rep[k] for k in ("total", "d7", "win_rate", "wins", "closed", "max_dd", "since")}
        stats["series"] = [[t, v] for t, v in rep["series"]]
        stats["recent"] = rep["recent"]
    except Exception:  # noqa: BLE001
        stats = None
    mine = s.db._q("""SELECT side, COUNT(*) n, SUM(sol) sol FROM sol_trades WHERE tg_id = ? AND leader_id = ?
                      AND status = 'filled' GROUP BY side""", (uid, lid))
    return {"id": lid, "name": lead["name"], "wallet": lead["wallet"], "hold": s.sol.hold_stats(lid),
            "status": None if not fr else ("copy" if fr["copy"] else "watch"), "own": own_amount(fr),
            "stats": stats, "mine": {r["side"]: {"n": r["n"], "sol": r["sol"] or 0} for r in mine},
            "can_rename": lead["owner_tg_id"] == uid or uid in admin_ids(s)}


async def token(s, u, mint: str) -> dict:
    se = s.sol
    info = await se.jup.token_full(mint)
    if not info:
        raise ApiError("Nie znalazłem takiego tokena")
    dec = int(info.get("decimals") or 0)
    se.db._q("INSERT OR REPLACE INTO sol_tokens VALUES (?, ?, ?)", (mint, info.get("symbol") or short(mint), dec))
    level, notes = risk(info)
    held = await se.token_raw(u, mint)
    mine = None
    if held > 0:
        v = (await se.values_sol({mint: held})).get(mint)
        basis = se.cost_basis(u["tg_id"], mint, not se.is_live(u))
        mine = {"tokens": held / 10 ** dec, "value": v,
                "pnl": (v / basis[0] - 1) * 100 if (v and basis and basis[0] > 0) else None}
    return {"mint": mint, "symbol": info.get("symbol"), "name": info.get("name"), "icon": info.get("icon"),
            "price": info.get("usdPrice"), "mcap": info.get("mcap"), "liquidity": info.get("liquidity"),
            "holders": info.get("holderCount"),
            "change": {k: (info.get(f"stats{k}") or {}).get("priceChange") for k in ("5m", "1h", "24h")},
            "risk": {"level": level, "notes": notes}, "mine": mine}


async def trending(s) -> list[dict]:
    items = await s.sol.jup.trending("1h", 15)
    return [{"mint": t["id"], "symbol": t.get("symbol"), "name": t.get("name"), "icon": t.get("icon"),
             "mcap": t.get("mcap"), "change": (t.get("stats1h") or {}).get("priceChange")} for t in items]


# ---------------- akcje ----------------

def _num(body, key, lo, hi, cast=float):
    try:
        v = cast(body[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError("Nieprawidłowa wartość") from None
    if not lo <= v <= hi:
        raise ApiError(f"Wartość spoza zakresu {lo:g}–{hi:g}")
    return v


async def action(s, u, body: dict) -> dict:
    from . import session
    from .axiom import _ensure_leader, buy, sol_leaders, wallet_lines
    se = s.sol
    uid = u["tg_id"]
    act = body.get("action")
    if act == "toggle":
        if u["active"]:
            s.db.update_user(uid, active=0)
            await session.end(s, s.db.get_user(uid), getattr(s, "bot", None), "kopiowanie wyłączone")
            return {"msg": "Kopiowanie wyłączone – raport sesji wysłałem w czacie 📊"}
        if not any(c for _, c in sol_leaders(s, uid)) and not s.db.copied_leaders(uid):
            raise ApiError("Najpierw dodaj portfel i włącz przy nim ✅ Kopiuj")
        s.db.update_user(uid, active=1)
        await session.start(s, s.db.get_user(uid))
        return {"msg": "Kopiowanie włączone ▶️"}
    if act == "add_wallets":
        many = wallet_lines(str(body.get("text", "")))
        if not many:
            raise ApiError("Nie widzę adresu portfela Solany (32–44 znaki)")
        check_copy_limit(s, uid, len(many))
        for addr, name in many:
            lid = _ensure_leader(s, uid, addr)
            if name:
                s.db.update_leader(lid, name=name)
            s.db.follow(uid, lid, True)
        se.wallet(uid)
        return {"msg": f"Dodano: {len(many)} – kopiujesz" + ("" if u["active"] else ". Włącz kopiowanie ▶️"),
                "ids": [_ensure_leader(s, uid, a) for a, _ in many]}
    if act in ("follow", "rename", "amount"):
        lid = int(_num(body, "lid", 1, 10 ** 12, int))
        lead = s.db.get_leader(lid)
        if not lead or lead["status"] != "active" or lead["source"] != "sol":
            raise ApiError("Ten portfel nie jest już śledzony")
        if act == "follow":
            mode = body.get("mode")
            if mode == "remove":
                s.db.unfollow(uid, lid)
                return {"msg": "Usunięto z listy"}
            if mode not in ("copy", "watch"):
                raise ApiError("Nieznana opcja")
            fr = s.db.follow_row(uid, lid)
            if mode == "copy" and not (fr and fr["copy"]):
                check_copy_limit(s, uid)
            s.db.follow(uid, lid, mode == "copy")
            se.wallet(uid)
            return {"msg": "✅ Kopiujesz" if mode == "copy" else "👁 Tylko powiadomienia"}
        if act == "rename":
            if not (lead["owner_tg_id"] == uid or uid in admin_ids(s)):
                raise ApiError("Nazwę zmienia osoba, która dodała portfel")
            name = str(body.get("name", "")).strip()[:40]
            if not name:
                raise ApiError("Podaj nazwę")
            s.db.update_leader(lid, name=name)
            return {"msg": "Zapisano nazwę"}
        if not s.db.follow_row(uid, lid):
            s.db.follow(uid, lid, False)
        if body.get("sol"):
            s.db.set_follow_amount(uid, lid, sol=_num(body, "sol", 0.001, 100))
        elif body.get("pct"):
            s.db.set_follow_amount(uid, lid, pct=_num(body, "pct", 0.1, 1000))
        else:
            s.db.set_follow_amount(uid, lid)
        return {"msg": "Zapisano kwotę"}
    if act == "import":
        from .importer import parse_wallets
        items = parse_wallets(str(body.get("text", "")))
        if not items:
            raise ApiError("Nie znalazłem żadnego adresu portfela w tym pliku")
        new, upd = s.db.save_wallets(uid, items, "import")
        start_scan(s, uid)
        return {"msg": f"📥 Zapisano {len(items)} portfeli (nowe: {new}) – sprawdzam, kto jest aktywny. "
                       "Włącz 🟢 tych, których chcesz kopiować.", "count": len(items)}
    if act in ("saved_follow", "saved_remove"):
        wallet = valid_address(str(body.get("wallet", "")))
        if not wallet:
            raise ApiError("Zły adres portfela")
        if act == "saved_remove":
            s.db.unsave_wallet(uid, wallet)
            return {"msg": "Usunięto z listy Zapisane"}
        mode = body.get("mode")
        if mode not in ("copy", "watch"):
            raise ApiError("Nieznana opcja")
        if mode == "copy":
            check_copy_limit(s, uid)
        lid = follow_saved(s, uid, wallet, mode == "copy")
        return {"msg": ("🟢 Kopiujesz" if mode == "copy" else "👁 Obserwujesz") +
                ("" if u["active"] or mode != "copy" else " – włącz kopiowanie ▶️ na pulpicie"), "lid": lid}
    if act == "bulk":  # zaznaczeni traderzy: usuń / wyłącz / włącz / ⭐
        op = body.get("op")
        wallets = [w for w in (valid_address(str(x)) for x in (body.get("wallets") or [])[:500]) if w]
        if op not in ("delete", "off", "on", "star", "unstar") or not wallets:
            raise ApiError("Nic nie zaznaczono")
        done, errors = 0, []
        for w in wallets:
            try:
                if op == "delete":
                    for r in s.db._q("SELECT id FROM leaders WHERE source = 'sol' AND wallet = ?", (w,)):
                        s.db.unfollow(uid, r["id"])
                    s.db.unsave_wallet(uid, w)
                elif op in ("off", "on"):
                    switch(s, uid, w, op == "on")
                else:
                    ensure_saved(s, uid, w)
                    s.db.set_priority(uid, w, op == "star")
                done += 1
            except ApiError as e:
                errors.append(str(e))
                break  # np. limit kopiowanych – dalej nie ma sensu
        names = {"delete": "Usunięto", "off": "Wyłączono", "on": "Włączono", "star": "⭐ Priorytet dla",
                 "unstar": "Bez priorytetu"}
        return {"msg": f"{names[op]}: {done}" + (f" · {errors[0]}" if errors else ""), "done": done}
    if act in ("switch", "priority"):
        wallet = valid_address(str(body.get("wallet", "")))
        if not wallet:
            raise ApiError("Zły adres portfela")
        on = bool(body.get("on"))
        if act == "switch":
            return {"msg": switch(s, uid, wallet, on) + ("" if not on or u["active"] else ". Włącz kopiowanie ▶️")}
        ensure_saved(s, uid, wallet)
        s.db.set_priority(uid, wallet, on)
        return {"msg": "⭐ Priorytet – na samej górze listy" if on else "Bez priorytetu"}
    if act == "research":
        if not start_research(s, uid):
            st = _RESEARCH.get(uid)
            return {"msg": f"Już badam: {st['done']}/{st['total']}" if st else "Badanie niedostępne"}
        n = _RESEARCH[uid]["total"]
        return {"msg": f"🏆 Badam {n} traderów (zysk, skuteczność, czas trzymania) – ok. {max(n // 20, 1)}–{max(n // 8, 2)} "
                       "min, lista sama się odświeży"}
    if act == "remove_inactive":
        n = remove_inactive(s, uid, float(body.get("days") or 7))
        return {"msg": f"🧹 Usunięto {n} nieaktywnych z listy" if n else
                "Nie ma kogo usunąć – najpierw sprawdź aktywność albo zbadaj traderów"}
    if act == "enable_best":
        on = enable_best(s, uid, int(body.get("n") or 5))
        return {"msg": ("🟢 Włączono: " + ", ".join(on)) if on else
                "Brak traderów z oceną 🏆 – najpierw kliknij „Znajdź najlepszych”"}
    if act == "scan_activity":
        st = _SCANS.get(uid)
        if st and st["running"]:
            return {"msg": f"Już sprawdzam: {st['done']}/{st['total']}"}
        start_scan(s, uid, bool(body.get("all")))
        n = len(saved_list(s, uid))
        return {"msg": f"🔍 Sprawdzam aktywność {n} portfeli – potrwa ok. {max(n * 2 // 60, 1)} min, lista sama się "
                       "odświeży"}
    if act == "settings":
        field = body.get("field")
        if field not in SETTINGS:
            raise ApiError("Nieznane ustawienie")
        typ, lo, hi = SETTINGS[field]
        if typ == "mode":
            val = body.get("value")
            if val not in ("fixed", "pct", "ratio"):
                raise ApiError("Nieznany tryb")
        elif typ == "notify":
            val = body.get("value")
            if val not in ("all", "sells", "none"):
                raise ApiError("Nieznana opcja")
        elif typ is bool:
            val = int(bool(body.get("value")))
        else:
            val = _num(body, "value", lo, hi, typ)
        s.db.update_user(uid, **{field: val})
        return {"msg": "Zapisano"}
    if act == "mode":
        live = bool(body.get("live"))
        if live and not body.get("confirm"):
            raise ApiError("Potwierdź przejście na prawdziwe pieniądze")
        s.db.update_user(uid, sol_live=int(live))
        return {"msg": "💰 Grasz prawdziwymi SOL" if live else "🧪 Wróciłeś do ćwiczeń"}
    if act == "import_wallet":
        try:
            kp = se.parse_secret(str(body.get("secret", "")))
        except Skip as e:
            raise ApiError(str(e)) from None
        pub = str(kp.pubkey())
        if not body.get("confirm"):
            try:
                bal = await se.rpc.balance(pub) / LAMPORTS
            except Exception:  # noqa: BLE001
                bal = None
            return {"address": pub, "balance": bal, "confirm_needed": True}
        if body.get("address") != pub:
            raise ApiError("Adres się nie zgadza – wklej klucz jeszcze raz")
        se.import_wallet(uid, kp, "axiom")
        return {"msg": f"🔗 Podłączono portfel {short(pub)}. Bot handluje nim po przejściu na 💰 prawdziwe pieniądze.",
                "address": pub}
    if act == "restore_wallet":
        pub = se.restore_wallet(uid)
        if not pub:
            raise ApiError("Nie ma poprzedniego portfela do przywrócenia")
        return {"msg": f"↩️ Wrócono do portfela {short(pub)}", "address": pub}
    if act == "reclaim_rent":
        if not se.is_live(u):
            raise ApiError("Działa na prawdziwym portfelu (💰) – na ćwiczeniach depozyty wracają same")
        n = await se.reclaim_rent(u)
        return {"msg": f"🧹 Zamknięto {n} pustych kont tokenów – wróciło ok. {n * 0.00204:.4f} SOL" if n
                else "Nie ma pustych kont tokenów do zamknięcia"}
    if act in ("auto_settings", "autocfg_preview"):
        from . import autocfg
        from .axiom import portfolio
        pf = await portfolio(s, u)
        bal = float(body.get("balance") or pf["total"])
        if bal <= 0:
            raise ApiError("Saldo 0 SOL – najpierw wpłać SOL albo zacznij ćwiczenia")
        if act == "autocfg_preview":
            tr = autocfg.trader_profiles(s, uid)
            p = autocfg.plan(bal, tr, se.is_live(u), autocfg.performance(s, u))
            return {"text": autocfg.describe(p, tr), "values": p["values"], "warn": p["warn"]}
        p = autocfg.apply(s, u, bal, full=True)
        auto = body.get("auto", True)
        s.db.update_user(uid, sol_autocfg=int(bool(auto)))
        return {"msg": "✨ Ustawione pod Twoje saldo", "text": p["text"], "values": p["values"], "warn": p["warn"],
                "auto": bool(auto)}
    if act == "admin_copy_traders":
        if uid not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        src = int(_num(body, "uid", 1, 10 ** 15, int))
        if src == uid:
            raise ApiError("To Twoi traderzy 🙂")
        theirs = s.db._q("""SELECT l.wallet, l.name, f.copy FROM follows f JOIN leaders l ON l.id = f.leader_id
                            WHERE f.tg_id = ? AND l.source = 'sol' AND l.status = 'active'""", (src,))
        want = body.get("wallets")
        if want:
            want = {str(w) for w in want}
            theirs = [r for r in theirs if r["wallet"] in want]
        elif body.get("only_copied", True):
            theirs = [r for r in theirs if r["copy"]]
        if not theirs:
            raise ApiError("Ta osoba nie ma traderów do skopiowania")
        mine = {it["wallet"]: it["status"] for it in saved_list(s, uid)}
        new = [r for r in theirs if r["wallet"] not in mine]
        if new:
            s.db.save_wallets(uid, [(r["wallet"], r["name"], None) for r in new], "admin")
        turned, full = 0, False
        if body.get("copy"):
            for r in theirs:
                if mine.get(r["wallet"]) == "copy":
                    continue
                try:
                    switch(s, uid, r["wallet"], True)
                    turned += 1
                except ApiError:
                    full = True
                    break
        msg = f"📋 Skopiowano {len(theirs)} traderów (nowych na Twojej liście: {len(new)})"
        if body.get("copy"):
            msg += f" · włączono kopiowanie: {turned}"
            if full:
                msg += f" – reszta czeka na liście (limit {MAX_COPIED} kopiowanych naraz)"
        return {"msg": msg, "added": len(new), "turned": turned}
    if act == "auto_name":
        from . import leaderboard as LB
        n = LB.auto_name(s, uid, refresh=True, everyone=bool(body.get("everyone")))
        return {"msg": f"🏷 Nazwano {n} traderów wg rankingu" if n else
                "Nikogo nie trzeba nazywać (albo nie ma ich w rankingu)"}
    if act == "optimize":
        from .axiom import portfolio
        from .optimizer import optimize
        pf = await portfolio(s, u)
        lids = [it["lid"] for it in saved_list(s, uid) if it["status"] == "copy" and it["lid"]]
        if not lids:
            raise ApiError("Najpierw włącz traderów – optymalizator sprawdza ich ruchy z ostatnich 7 dni")
        days = float(body.get("days") or 7)
        return await asyncio.to_thread(optimize, s, u, pf["total"], lids, typical_trader_buy(s, uid), days)
    if act == "apply_settings":
        vals = body.get("settings") or {}
        clean = {}
        for k, v in vals.items():
            if k not in SETTINGS:
                continue
            typ, lo, hi = SETTINGS[k]
            if typ == "mode":
                if v in ("fixed", "pct", "ratio"):
                    clean[k] = v
            elif typ is bool:
                clean[k] = int(bool(v))
            elif isinstance(v, (int, float)) and lo <= v <= hi:
                clean[k] = typ(v)
        if not clean:
            raise ApiError("Brak ustawień do zapisania")
        s.db.update_user(uid, **clean)
        return {"msg": "✅ Zastosowano najlepsze ustawienia z optymalizatora"}
    if act == "reset_demo":
        se.paper_reset(uid)
        return {"msg": f"Ćwiczenia od nowa: {se.paper_start(uid):g} wirtualnych SOL"}
    if act == "buy":
        mint = valid_address(str(body.get("mint", "")))
        if not mint:
            raise ApiError("Zły adres tokena")
        try:
            return {"msg": await buy(s, u, mint, _num(body, "sol", 0.001, 1000))}
        except Skip as e:
            raise ApiError(str(e)) from None
    if act == "sell":
        mint = valid_address(str(body.get("mint", "")))
        pct = int(_num(body, "pct", 1, 100, int))
        try:
            fill = await se.sell_manual(u, mint, pct)
        except Skip as e:
            raise ApiError(str(e)) from None
        return {"msg": f"✅ Sprzedano za {fill.sol:.4f} SOL{await se.usd_txt(fill.sol)}"}
    if act in ("withdraw", "restore_wallet") or (act == "import_wallet" and body.get("confirm")):
        from .extras import pin_locked, pin_ok
        if not pin_ok(u, body.get("pin")):
            raise ApiError("Za dużo prób – PIN zablokowany na 15 min" if pin_locked(uid) else "❌ Zły PIN")
    if act == "set_pin":
        from .extras import hash_pin, pin_ok
        if u["pin_hash"] and not pin_ok(u, body.get("old_pin")):
            raise ApiError("❌ Zły obecny PIN")
        new = str(body.get("pin") or "")
        if not new:
            s.db.update_user(uid, pin_hash=None)
            return {"msg": "PIN usunięty"}
        if not (new.isdigit() and 4 <= len(new) <= 8):
            raise ApiError("PIN to 4–8 cyfr")
        s.db.update_user(uid, pin_hash=hash_pin(new))
        return {"msg": "🔐 PIN ustawiony – wypłata, podłączenie i zmiana portfela będą go wymagały"}
    if act == "export_csv":
        from .extras import send_csv
        if not getattr(s, "bot", None):
            raise ApiError("Bot niedostępny")
        await send_csv(s, s.bot, uid)
        return {"msg": "📄 Wysłałem plik CSV w czacie z botem"}
    if act == "backup":
        from .extras import send_backup
        if adm.role_of(s, uid) != "owner" or not getattr(s, "bot", None):
            raise ApiError("Kopię zapasową dostaje tylko właściciel 👑")
        await send_backup(s, s.bot, uid)
        return {"msg": "🗄 Wysłałem kopię zapasową w czacie z botem"}
    if act == "withdraw":
        dest = valid_address(str(body.get("address", "")))
        if not dest:
            raise ApiError("Zły adres portfela")
        if not body.get("confirm"):
            raise ApiError("Potwierdź wypłatę")
        pub, kp = se.wallet(uid)
        bal = await se.rpc.balance(pub)
        amt = str(body.get("amount", "")).strip().lower()
        try:
            lamports = bal - 5000 if amt in ("all", "max", "wszystko") else int(float(amt.replace(",", ".")) * LAMPORTS)
        except ValueError:
            raise ApiError("Nieprawidłowa kwota") from None
        if lamports <= 0 or lamports + 5000 > bal:
            raise ApiError(f"Za mało środków. Saldo: {bal / LAMPORTS:.4f} SOL")
        sig = await se.rpc.transfer_sol(kp, dest, lamports)
        return {"msg": f"✅ Wypłacono {lamports / LAMPORTS:.6f} SOL", "sig": sig}
    if act == "admin_pause":
        if uid not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        s.db.set_state("paused", "1" if body.get("on") else "0")
        return {"msg": "⏸ Kopiowanie wstrzymane wszystkim" if body.get("on") else "▶️ Kopiowanie wznowione"}
    if act == "lb_keys":
        from . import leaderboard as LB
        if uid not in admin_ids(s):
            raise ApiError("Klucze rankingu ustawia administrator")
        changed = []
        for which in ("st", "vybe"):
            if which in body:
                val = str(body.get(which) or "").strip()
                if val and (len(val) < 16 or len(val) > 200 or any(c.isspace() for c in val)):
                    raise ApiError("To nie wygląda na klucz API – skopiuj go w całości")
                LB.set_key(s, which, val)
                changed.append(which)
        if changed:
            LB.start_rebuild(s)
        return {"msg": "🔑 Zapisano – liczę ranking światowy (ok. 1–3 min)" if changed else "Nic nie zmieniono",
                "keys": LB.key_status(s)}
    if act in ("lb_add", "lb_refresh"):
        if act == "lb_refresh":
            from . import leaderboard as LB
            if uid not in admin_ids(s):
                raise ApiError("Odświeżyć ranking może administrator (i tak odświeża się sam co 6 h)")
            started = LB.start_rebuild(s, force_chain=bool(body.get("chain")))
            return {"msg": "🔄 Liczę ranking od nowa – świat ok. 1–3 min, portfele bota do 20 min" if started
                    else "Już liczę ranking…"}
        wallet = valid_address(str(body.get("wallet", "")))
        if not wallet:
            raise ApiError("Zły adres portfela")
        from . import leaderboard as LB
        name = None
        try:
            rank, period = int(body.get("rank") or 0), int(body.get("period") or 0)
        except (TypeError, ValueError):
            rank = period = 0
        if 1 <= rank <= 500 and period in (1, 7, 30):
            kol = str(body.get("kol") or "").strip()[:30] or None
            name = LB.rank_name(str(body.get("sort") or "score"), rank, period, kol)
        name = name or str(body.get("name") or "")[:48] or f"🌍 {short(wallet)}"
        old_name = s.db._q("SELECT name FROM sol_saved WHERE tg_id = ? AND wallet = ?", (uid, wallet))
        if old_name and not LB.is_generic(old_name[0]["name"], wallet):
            name = None  # własnej nazwy nie nadpisujemy
        s.db.save_wallets(uid, [(wallet, name, None)], "ranking")
        if name:
            s.db._q("UPDATE sol_saved SET name = ? WHERE tg_id = ? AND wallet = ?", (name, uid, wallet))
            for r in s.db._q("SELECT id, name FROM leaders WHERE source = 'sol' AND wallet = ?", (wallet,)):
                if LB.is_generic(r["name"], wallet):
                    s.db.update_leader(r["id"], name=name)
        if body.get("copy"):
            msg = switch(s, uid, wallet, True)
            if name:
                for r in s.db._q("SELECT id, name FROM leaders WHERE source = 'sol' AND wallet = ?", (wallet,)):
                    if LB.is_generic(r["name"], wallet):
                        s.db.update_leader(r["id"], name=name)
            return {"msg": msg + (f" · nazwa: {name}" if name else "")}
        return {"msg": "➕ Dodano do Twojej listy traderów (wyłączony – włączysz, kiedy chcesz)"}
    if act == "chat_send":
        import time as _t
        text = str(body.get("text", "")).strip()[:1000]
        if not text:
            raise ApiError("Pusta wiadomość")
        if _t.time() - _CHAT_LAST_SENT.get(uid, 0) < 0.7:
            raise ApiError("Za szybko – chwila przerwy")
        _CHAT_LAST_SENT[uid] = _t.time()
        mid = s.db.chat_add(uid, text)
        asyncio.create_task(chat_notify_others(s, u, text))
        return {"id": mid}
    if act == "chat_delete":
        mid = int(_num(body, "id", 1, 10 ** 12, int))
        r = s.db._q("SELECT tg_id FROM chat_messages WHERE id = ?", (mid,))
        if not r:
            raise ApiError("Nie ma takiej wiadomości")
        if r[0]["tg_id"] != uid and uid not in admin_ids(s):
            raise ApiError("Usunąć może autor albo administrator")
        s.db.chat_delete(mid)
        return {"msg": "Usunięto wiadomość"}
    if act == "nickname":  # admin: dowolnej osobie; każdy: sobie
        target = int(body.get("uid") or uid)
        if target != uid and uid not in admin_ids(s):
            raise ApiError("Nazwy innym nadaje tylko administrator")
        if not s.db.get_user(target):
            raise ApiError("Nie ma takiej osoby")
        name = adm.clean_nickname(body.get("name", ""))
        s.db.update_user(target, nickname=name)
        return {"msg": f"✏️ Nazwa: {name}" if name else "Usunięto nazwę"}
    if act in ("admin_stop", "admin_role"):
        if uid not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        target = int(_num(body, "uid", 1, 10 ** 15, int))
        if act == "admin_stop":
            bot = getattr(s, "bot", None)
            if bot is None:
                tu = s.db.get_user(target)
                if not tu or not tu["active"]:
                    return {"msg": "Już stoi"}
                s.db.update_user(target, active=0)
                await session.end(s, s.db.get_user(target), None, "zatrzymane przez admina")
                return {"msg": "Zatrzymano"}
            ok = await adm.stop_user(s, target, bot, adm.display_name(u))
            return {"msg": "Zatrzymano" if ok else "Już stoi"}
        new = adm.toggle_role(s, uid, target)
        if new is None:
            raise ApiError("Rangi nadaje tylko właściciel 👑")
        return {"msg": "Dano rangę admina ⭐" if new == "admin" else "Zabrano rangę admina"}
    raise ApiError("Nieznana akcja")


def routes(s, app: web.Application, auth):
    """Podpina /api/sol/* do serwera panelu."""
    def handler(fn):
        async def wrapped(request: web.Request):
            u = auth(request)
            try:
                return web.json_response(await asyncio.wait_for(fn(request, u), 40))
            except ApiError as e:
                return web.json_response({"error": str(e)}, status=400)
            except asyncio.TimeoutError:
                return web.json_response({"error": "Przekroczony czas – spróbuj jeszcze raz"}, status=504)
            except web.HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("API Solany")
                return web.json_response({"error": str(e)[:200] or "błąd"}, status=500)
        return wrapped

    async def h_state(_, u):
        return await state(s, u)

    async def h_wallet(req, u):
        return await wallet(s, u, int(req.match_info["lid"]))

    async def h_token(req, u):
        mint = valid_address(req.match_info["mint"])
        if not mint:
            raise ApiError("Zły adres tokena")
        return await token(s, u, mint)

    async def h_trending(_, u):
        return {"items": await trending(s)}

    async def h_report(_, u):
        from . import session
        return {"html": await session.build(s, s.db.get_user(u["tg_id"]))}

    async def h_deposit(_, u):
        pub, _ = s.sol.wallet(u["tg_id"])
        try:
            bal = await s.sol.rpc.balance(pub) / LAMPORTS
        except Exception:  # noqa: BLE001
            bal = None
        backups = s.db._q("SELECT pubkey, source FROM sol_wallet_backup WHERE tg_id = ? ORDER BY id DESC LIMIT 1",
                          (u["tg_id"],))
        return {"address": pub, "balance": bal, "source": s.sol.wallet_source(u["tg_id"]),
                "previous": dict(backups[0]) if backups else None}

    async def h_action(req, u):
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            raise ApiError("Złe zapytanie") from None
        if not isinstance(body, dict):
            raise ApiError("Złe zapytanie")
        return await action(s, s.db.get_user(u["tg_id"]), body)

    async def h_live(req, u):
        """Długie odpytywanie: odpowiada od razu, gdy bot coś kupi/sprzeda albo ktoś napisze na czacie."""
        from .events import BUS
        try:
            v, c = int(req.query.get("v", -1)), int(req.query.get("c", -1))
        except ValueError:
            v = c = -1
        if v < 0:
            return BUS.snapshot(u["tg_id"])
        return await BUS.wait(u["tg_id"], v, c, timeout=25)

    async def h_leaderboard(req, u):
        from . import leaderboard as LB
        try:
            period = int(req.query.get("period") or 7)
        except ValueError:
            period = 7
        period = period if period in (1, 7, 30) else 7
        mine = {it["wallet"]: it["status"] for it in saved_list(s, u["tg_id"])}
        cnt = LB.counts(s, period)
        kind = req.query.get("kind")
        kind = kind if kind in ("world", "chain") else ("world" if cnt["world"] else "chain")
        rows = LB.top(s, period, req.query.get("sort") or "score", kind=kind)
        for r in rows:
            r["in_list"] = r["wallet"] in mine
            r["status"] = mine.get(r["wallet"])
        admin = u["tg_id"] in admin_ids(s)
        return {"rows": rows, "period": period, "kind": kind, "counts": cnt, "admin": admin,
                "state": {k: LB.STATE.get(k) for k in ("running", "done", "total", "started", "finished", "step",
                                                       "errors", "world", "chain")},
                "keys": LB.key_status(s) if admin else None, "has_world": any(LB.key_status(s).values())}

    async def h_ranking(_, u):
        uu = s.db.get_user(u["tg_id"])
        return {"rows": ranking(s, u["tg_id"], not s.sol.is_live(uu)), "live": s.sol.is_live(uu),
                "copied": copied_count(s, u["tg_id"]), "max_copied": MAX_COPIED}

    async def h_chat(req, u):
        after = int(req.query.get("after") or 0)
        return {"messages": [chat_msg(s, r, u["tg_id"]) for r in s.db.chat_list(after)],
                "last_id": s.db.chat_last_id(), "me": u["tg_id"], "admin": u["tg_id"] in admin_ids(s)}

    async def h_saved(_, u):
        return {"items": saved_list(s, u["tg_id"]), "max_copied": MAX_COPIED,
                "copied": copied_count(s, u["tg_id"]), "scan": _SCANS.get(u["tg_id"]), "now": now_ms(),
                "research": _RESEARCH.get(u["tg_id"])}

    async def h_admin_users(_, u):
        if u["tg_id"] not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        return dict(await adm.dashboard(s), me=u["tg_id"], owner=adm.role_of(s, u["tg_id"]) == "owner")

    async def h_admin_user(req, u):
        if u["tg_id"] not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        d = await adm.user_detail(s, int(req.match_info["uid"]))
        if not d:
            raise ApiError("Nie ma takiej osoby")
        return d

    r = app.router
    r.add_get("/api/sol/state", handler(h_state))
    r.add_get("/api/sol/wallet/{lid:\\d+}", handler(h_wallet))
    r.add_get("/api/sol/token/{mint}", handler(h_token))
    r.add_get("/api/sol/trending", handler(h_trending))
    r.add_get("/api/sol/report", handler(h_report))
    r.add_get("/api/sol/deposit", handler(h_deposit))
    r.add_post("/api/sol/action", handler(h_action))
    r.add_get("/api/sol/saved", handler(h_saved))
    r.add_get("/api/sol/chat", handler(h_chat))
    r.add_get("/api/sol/ranking", handler(h_ranking))
    r.add_get("/api/sol/live", handler(h_live))
    r.add_get("/api/sol/leaderboard", handler(h_leaderboard))
    r.add_get("/api/sol/admin/users", handler(h_admin_users))
    r.add_get("/api/sol/admin/user/{uid:\\d+}", handler(h_admin_user))
