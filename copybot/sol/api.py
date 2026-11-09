"""API mini aplikacji (Telegram Mini App) dla kopiowania portfeli Solany.

Każde zapytanie jest podpisane przez Telegrama (nagłówek X-Init-Data) – sprawdza to `auth` z web.py.
Odpowiedzi to czyste dane (JSON); wygląd robi copybot/webapp/sol.html.
"""
import asyncio
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
    "sol_safe": (bool, None, None), "sol_x2": (bool, None, None),
}


class ApiError(Exception):
    pass


def _trade(s, r) -> dict:
    return {"ts": r["ts"], "side": r["side"], "mint": r["mint"], "symbol": r["symbol"] or short(r["mint"]),
            "sol": r["sol"], "tokens": r["tokens"], "status": r["status"], "info": r["info"],
            "latency_ms": r["latency_ms"], "sig": r["sig"]}


def settings_of(u) -> dict:
    return {k: (bool(u[k]) if t is bool else u[k]) for k, (t, *_r) in SETTINGS.items()} | {
        "summary": buy_txt(u), "presets": [float(x) for x in (u["sol_presets"] or "0.05,0.1,0.5,1").split(",") if x]}


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
        out.append({"wallet": r["wallet"], "name": r["name"] or (f["name"] if f else None), "emoji": r["emoji"],
                    "status": (("copy" if f["copy"] else "watch") if f else None), "lid": f["id"] if f else None,
                    "priority": bool(r["priority"] or (f and f["priority"])), "last_active": r["last_active"],
                    "checked_at": r["checked_at"], "order": i})
    for w, f in follows.items():
        if w not in seen:
            out.append({"wallet": w, "name": f["name"], "emoji": None, "status": "copy" if f["copy"] else "watch",
                        "lid": f["id"], "priority": bool(f["priority"]), "last_active": None, "checked_at": None,
                        "order": -1})
    out.sort(key=lambda x: (not x["priority"], x["status"] != "copy", -(x["last_active"] or 0), x["order"]))
    return out


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
                        "priority": bool(fr and fr["priority"]),
                        "result": rep["total"] * 100 if ok else None,
                        "spark": [v for _, v in rep["series"]][-30:] if ok else [],
                        "own": own_amount(fr)})
    wallets.sort(key=lambda w: (not w["priority"], not w["copy"]))
    start = s.cfg.sol_paper_start
    series = s.db.sol_equity_series(uid, not live, now_ms() - 7 * DAY_MS) + [(now_ms(), pf["total"])]
    last = s.db._q("SELECT latency_ms FROM sol_trades WHERE tg_id = ? AND latency_ms IS NOT NULL ORDER BY id DESC "
                   "LIMIT 1", (uid,))
    return {
        "user": {"id": uid, "admin": uid in admin_ids(s), "role": adm.role_of(s, uid)},
        "live": live, "active": bool(u["active"]), "paused": s.db.get_state("paused") == "1",
        "sol_usd": pf["sol_usd"], "sol_change": sol_ch, "sol": pf["sol"], "total": pf["total"],
        "demo_start": start, "result": (pf["total"] / start - 1) * 100 if not live else None,
        "session_start": u["session_start"] if not u["session_end"] else None, "session_sol0": u["session_sol0"],
        "today_pnl": se.day_pnl(uid, not live),
        "positions": [{k: p[k] for k in ("mint", "symbol", "tokens", "value", "pnl", "cost")} for p in pf["positions"]],
        "wallets": wallets, "feed": [_trade(s, r) for r in se.history(uid, 30)],
        "series": [[t, v] for t, v in series], "last_latency_ms": last[0]["latency_ms"] if last else None,
        "settings": settings_of(u),
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
    return {"id": lid, "name": lead["name"], "wallet": lead["wallet"],
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
    if act == "reset_demo":
        se.paper_reset(uid)
        return {"msg": f"Ćwiczenia od nowa: {s.cfg.sol_paper_start:g} wirtualnych SOL"}
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
        return {"address": pub, "balance": bal}

    async def h_action(req, u):
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            raise ApiError("Złe zapytanie") from None
        if not isinstance(body, dict):
            raise ApiError("Złe zapytanie")
        return await action(s, s.db.get_user(u["tg_id"]), body)

    async def h_saved(_, u):
        return {"items": saved_list(s, u["tg_id"]), "max_copied": MAX_COPIED,
                "copied": copied_count(s, u["tg_id"]), "scan": _SCANS.get(u["tg_id"]), "now": now_ms()}

    async def h_admin_users(_, u):
        if u["tg_id"] not in admin_ids(s):
            raise ApiError("Tylko dla administratora")
        return {"users": await adm.overview(s), "me": u["tg_id"], "owner": adm.role_of(s, u["tg_id"]) == "owner"}

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
    r.add_get("/api/sol/admin/users", handler(h_admin_users))
    r.add_get("/api/sol/admin/user/{uid:\\d+}", handler(h_admin_user))
