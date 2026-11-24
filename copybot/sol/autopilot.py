"""🚀 Autopilot zarobku – jeden przycisk, potem bot sam pilnuje, żeby kopiowanie zarabiało.

1. Skład: traderzy wysoko NARAZ w ocenie, aktywności i stabilności (ranking światowy 7 dni, sprawdzeni też
   w 30 dniach), bez botów, flipperów, nieaktywnych i tracących. Ilu – zależy od salda (mniej traderów przy
   małym saldzie = sensowne kwoty zamiast groszy).
2. Rotacja co 6 h: wyłącza tych, na których kopiowaniu TY tracisz (≥5 sprzedaży na minusie w 7 dni),
   tych, którzy przestali handlować (2 doby bez ruchu) i flipperów; zostawia zarabiających; wolne miejsca
   uzupełnia najlepszymi z rankingu. ⭐ priorytetowych nie rusza.
3. Ustawienia: kwoty pod saldo (🤖 Auto), wyjście z martwych pozycji po 90 min, ucieczka twórcy tokena,
   ×2 z ruchomym stopem, więcej pieniędzy na zarabiających (🧠).
Uczciwie: to zwiększa szanse, nie gwarantuje zysku – rynek memecoinów potrafi dać serię strat każdemu.
"""
import html
import logging
import time
from collections import Counter

from ..db import now_ms, ref_id
from . import autocfg, leaderboard as LB

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000
COMBO = "score,active,green"
PROFIT_SETTINGS = {"sol_time_exit": 90, "sol_dev_exit": 1, "sol_smart": 1, "sol_auto_rank": 0, "sol_autocfg": 1,
                   "sol_x2": 1, "sol_trail": 40, "sol_min_hold": 45, "sol_min_lead": 0, "sol_ladder": 1, "sol_chase": 10}  # 🛡 bezpieczników nie rusza


def target_count(balance: float) -> int:
    return 3 if balance < 0.5 else 4 if balance < 1 else 5 if balance < 3 else 6 if balance < 10 else 8


def _lab(s) -> dict[str, dict]:
    lab = getattr(getattr(s, "sol", None), "lab", None)
    try:
        return lab.by_wallet() if lab is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def candidates(s, why: Counter | None = None, use_lab: bool = True) -> list[dict]:
    """Najlepsi do kopiowania – świat (a gdy brak kluczy: ranking bota), od najlepszego.
    🧪 use_lab: najpierw sprawdzeni w laboratorium (kopia na nich zarabia), odrzuceni przez laboratorium odpadają.
    why – jeśli podany, zlicza powody odrzucenia (do logów)."""
    why = why if why is not None else Counter()
    for kind in ("world", "chain"):
        rows = LB.top(s, 7, COMBO, kind=kind)
        if rows:
            break
    else:
        why["pusty ranking"] += 1
        rows, kind = [], "chain"
    m30 = {r["wallet"]: r for r in LB.top(s, 30, "score", kind=kind)} if rows else {}
    now = time.time()
    seen = _bot_seen(s, [r["wallet"] for r in rows])
    out = []
    for r in rows:
        pnl = r["pnl"] if r["pnl"] is not None else r["pnl_usd"]
        r30 = m30.get(r["wallet"])
        if not pnl or pnl <= 0 or r.get("bot"):
            why["strata/bot"] += 1
        elif r.get("hold_s") is not None and r["hold_s"] < 30:
            why["flipper"] += 1  # kopia wchodzi za późno
        elif (r.get("activity") or 0) < 35:
            why["mało aktywny"] += 1  # rzadko handluje – mało okazji
        elif r.get("last") and now - r["last"] > 36 * 3600:
            why["cisza >36h"] += 1
        elif r["wallet"] in seen and seen[r["wallet"]][0] < now_ms() - 2 * DAY_MS and \
                (seen[r["wallet"]][1] or 0) < now_ms() - 2 * DAY_MS:
            why["bot nie widzi ruchów >48h"] += 1  # ranking mówi „aktywny”, a bot śledzi go od 2+ dni i nic
        elif r.get("win_rate") is not None and r["win_rate"] < 0.3:
            why["skuteczność <30%"] += 1
        elif r30 is not None and ((r30["pnl"] if r30["pnl"] is not None else r30["pnl_usd"]) or 0) <= 0:
            why["traci w 30d"] += 1  # zarabia 7 dni, ale w 30 dniach traci – szczęśliwy tydzień
        else:
            out.append(r)
    why[f"ok ({kind})"] += len(out)
    labw = _lab(s) if use_lab else {}
    if labw:
        kept = []
        for r in out:
            d = labw.get(r["wallet"])
            if d and d["verdict"] == "reject":
                why["🧪 odrzucony w laboratorium"] += 1
                continue
            kept.append({**r, "lab": d})
        have = {r["wallet"] for r in kept}
        # sprawdzeni w laboratorium, choć spoza dzisiejszej czołówki (np. kopiowani) – też kandydaci
        extra = [{"wallet": w, "name": d.get("name"), "lab": d} for w, d in labw.items()
                 if d["verdict"] == "best" and w not in have and d.get("active")]
        best = sorted([r for r in kept + extra if r.get("lab") and r["lab"]["verdict"] == "best"],
                      key=lambda r: -(r["lab"]["score"] or 0))
        rest = [r for r in kept if not (r.get("lab") and r["lab"]["verdict"] == "best")]
        why["🧪 sprawdzeni w laboratorium"] += len(best)
        out = best + rest
    return out


def _bot_seen(s, wallets: list[str]) -> dict[str, tuple[int, int | None]]:
    """wallet → (od kiedy bot go śledzi, ostatni ruch, który bot zobaczył) – własna obserwacja zamiast danych rankingu
    (Solana Tracker podaje „ostatni ruch” z opóźnieniem albo wcale)."""
    if not wallets:
        return {}
    q = ",".join("?" * len(wallets))
    rows = s.db._q(f"""SELECT l.wallet, MIN(f.created_at) since, MAX(t.ts) last FROM leaders l
                       JOIN follows f ON f.leader_id = l.id
                       LEFT JOIN sol_trades t ON t.tg_id = -l.id AND t.status = 'filled'
                       WHERE l.source = 'sol' AND l.wallet IN ({q}) GROUP BY l.wallet""", tuple(wallets))
    out = {r["wallet"]: (r["since"] or now_ms(), r["last"]) for r in rows}
    # 🧪 laboratorium też nasłuchuje (i to dłużej) – jego obserwacja się liczy
    for r in s.db._q(f"SELECT wallet, added_at, last_seen FROM lab WHERE wallet IN ({q})", tuple(wallets)):
        since, last = out.get(r["wallet"], (r["added_at"], None))
        out[r["wallet"]] = (min(since, r["added_at"]), max(last or 0, r["last_seen"] or 0) or None)
    return out


def _no_route_share(s, uid: int, lid: int) -> float:
    """Jaka część prób kopiowania tradera w 48 h skończyła się „brakiem trasy” (min. 5 takich prób)."""
    r = s.db._q("""SELECT COUNT(*) n, SUM(info LIKE '%brak trasy%' OR info LIKE '%zamrożon%' OR status = 'error') nr
                   FROM sol_trades WHERE tg_id = ? AND leader_id = ? AND ts >= ?""", (uid, lid, now_ms() - 2 * DAY_MS))[0]
    return (r["nr"] or 0) / r["n"] if r["n"] and (r["nr"] or 0) >= 5 else 0.0


def _his_last(s, lid: int) -> int | None:
    r = s.db._q("SELECT MAX(ts) t FROM sol_trades WHERE tg_id = ? AND status = 'filled'", (ref_id(lid),))
    return r[0]["t"] if r else None


def rotate(s, u, balance: float, initial: bool = False) -> dict:
    """Poprawia skład kopiowanych traderów. initial=True (włączenie autopilota) – także ogranicza liczbę
    do celu, zostawiając najpierw tych, na których już zarabiasz."""
    from .api import ApiError, ranking, saved_list, switch
    uid = u["tg_id"]
    paper = not s.sol.is_live(u)
    mine = {r["wallet"]: r for r in ranking(s, uid, paper)}
    items = [it for it in saved_list(s, uid) if it["status"] == "copy"]
    target = target_count(balance)
    if (u["sol_risk"] if "sol_risk" in u.keys() else 0) > 0:
        target = min(target, 3)  # 🔥 duże wejścia = mało traderów naraz (inaczej pierwszy zjada całe saldo)
    off, kept, on, off_w = [], [], [], set()
    labw = _lab(s)
    follows = {r["leader_id"]: r["created_at"] for r in s.db._q("SELECT leader_id, created_at FROM follows WHERE tg_id = ?",
                                                                 (uid,))}
    for it in items:
        name = it["name"] or it["wallet"][:6]
        r = mine.get(it["wallet"]) or {}
        why = None
        if not it["priority"]:
            sells, wins, pnl = r.get("sells") or 0, r.get("wins") or 0, r.get("pnl_7d") or 0
            if pnl < 0 and (sells >= 5 or (sells >= 3 and wins * 3 <= sells)):
                # ≥5 sprzedaży na minusie albo już ≥3 i trafia najwyżej co trzecią – nie czekamy na większą stratę
                why = f"tracisz na nim {pnl:+.3f} SOL w 7 dni ({wins}/{sells} z zyskiem)"
            elif it["lid"] and _no_route_share(s, uid, it["lid"]) >= 0.5:
                why = "większości jego zakupów nie da się skopiować (brak trasy / zamrożone tokeny / odrzucone przez sieć)"
            elif it["lid"]:
                last = _his_last(s, it["lid"]) or 0
                since = follows.get(it["lid"]) or now_ms()
                if now_ms() - max(last, since) > 2 * DAY_MS:
                    why = "nie handluje od 2 dni"
                else:
                    h = it.get("hold") or {}
                    if h.get("n", 0) >= 3 and (h.get("median_s") or 999) < 20:
                        why = "flipper – kopia nie nadąża"
            ld = labw.get(it["wallet"])
            if not why and ld and ld["verdict"] == "reject":
                why = f"🧪 laboratorium: {ld['reason']}"
        if why:
            switch(s, uid, it["wallet"], False)
            off.append(f"{name} ({why})")
            off_w.add(it["wallet"])
        else:
            kept.append((it, r))
    if initial and len(kept) > target:
        # za dużo traderów = grosze na każdego; zostają najpierw zarabiający, potem najwyżej w rankingu
        rank = {r["wallet"]: i for i, r in enumerate(candidates(s))}
        kept.sort(key=lambda x: (not x[0]["priority"], -((x[1].get("pnl_7d") or 0) if (x[1].get("sells") or 0) >= 3 else 0),
                                 rank.get(x[0]["wallet"], 10_000)))
        for it, _ in kept[target:]:
            if it["priority"]:
                continue
            switch(s, uid, it["wallet"], False)
            off.append(f"{it['name'] or it['wallet'][:6]} (za dużo traderów naraz)")
            off_w.add(it["wallet"])
        kept = [k for k in kept[:target]] + [k for k in kept[target:] if k[0]["priority"]]
    if not initial and labw and len(kept) >= target:
        # 🔁 szybka wymiana: jest sprawdzony w laboratorium, a któryś z Twoich nie (i Tobie na nim nie idzie) –
        # najwyżej 1 wymiana na rotację, żeby skład nie skakał
        have_w = {it["wallet"] for it, _ in kept}
        best = [w for w, d in labw.items() if d["verdict"] == "best" and d.get("active") and w not in have_w
                and w not in off_w]
        if best:
            top = max(labw[w]["roi"] or 0 for w in best)
            weak = [(it, r) for it, r in kept if not it["priority"] and (r.get("pnl_7d") or 0) <= 0
                    and (labw.get(it["wallet"]) or {}).get("verdict") != "best"
                    and ((labw.get(it["wallet"]) or {}).get("roi") is None or labw[it["wallet"]]["roi"] < top - 0.10)]
            if weak:
                it, _ = min(weak, key=lambda x: (labw.get(x[0]["wallet"]) or {}).get("roi") or -1)
                switch(s, uid, it["wallet"], False)
                off.append(f"{it['name'] or it['wallet'][:6]} (🧪 w laboratorium są lepsi)")
                off_w.add(it["wallet"])
                kept = [k for k in kept if k[0]["wallet"] != it["wallet"]]
    need = max(target - len(kept), 0)
    have = {it["wallet"] for it in saved_list(s, uid) if it["status"] == "copy"}
    why = Counter()
    cands = candidates(s, why) if need else []
    for i, r in enumerate(cands):
        if need <= 0:
            break
        w = r["wallet"]
        if w in have or w in off_w:
            continue
        prev = mine.get(w) or {}
        if (prev.get("sells") or 0) >= 5 and (prev.get("pnl_7d") or 0) < 0:
            continue  # już go kopiowałeś i traciłeś – nie wracamy do niego
        ld = r.get("lab")
        if ld and ld["verdict"] == "best":  # 🧪 sprawdzony: krótko i z wynikiem kopii
            name = f"🧪 Lab #{i + 1} ({(ld['roi'] or 0) * 100:+.0f}% kopia)"
        else:
            name = LB.rank_name(COMBO, i + 1, 7, r.get("name"))
        old = s.db._q("SELECT name FROM sol_saved WHERE tg_id = ? AND wallet = ?", (uid, w))
        if old and not LB.is_generic(old[0]["name"], w):
            name = old[0]["name"]
        s.db.save_wallets(uid, [(w, name, None)], "autopilot")
        s.db._q("UPDATE sol_saved SET name = ? WHERE tg_id = ? AND wallet = ?", (name, uid, w))
        try:
            switch(s, uid, w, True)
        except ApiError:
            break
        for ld in s.db._q("SELECT id, name FROM leaders WHERE source = 'sol' AND wallet = ?", (w,)):
            if LB.is_generic(ld["name"], w):
                s.db.update_leader(ld["id"], name=name)
        on.append(name)
        need -= 1
    if need > 0:
        skip = Counter()
        for r in cands:
            w = r["wallet"]
            prev = mine.get(w) or {}
            skip["już kopiowany" if w in have else "właśnie wyłączony" if w in off_w else
                 "tracił na nim" if (prev.get("sells") or 0) >= 5 and (prev.get("pnl_7d") or 0) < 0 else "inne"] += 1
        log.info("Autopilot %s: brakuje %d traderów – ranking: %s; kandydaci pominięci: %s",
                 uid, need, dict(why), dict(skip))
    return {"off": off, "on": on, "kept": [k[0]["name"] for k in kept], "target": target}


async def enable(s, u, balance: float) -> dict:
    res = rotate(s, u, balance, initial=True)
    s.db.update_user(u["tg_id"], sol_autopilot=1, **PROFIT_SETTINGS)
    p = autocfg.apply(s, s.db.get_user(u["tg_id"]), balance, full=True)
    s.db.update_user(u["tg_id"], **PROFIT_SETTINGS)  # pełne ustawienia z autocfg nie mogą nadpisać trybu zarobku
    s.db.set_state(f"autopilot:{u['tg_id']}", str(now_ms()))
    res["text"] = p["text"]
    res["warn"] = p["warn"]
    return res


def summary(res: dict) -> str:
    lines = []
    if res["on"]:
        lines.append("🟢 Włączeni: " + ", ".join(res["on"]))
    if res["off"]:
        lines.append("⚪ Wyłączeni: " + "; ".join(res["off"]))
    if res.get("kept"):
        lines.append("✅ Zostają: " + ", ".join(n or "?" for n in res["kept"]))
    return "\n".join(lines) or "Skład bez zmian – obecni traderzy dalej się sprawdzają."


async def best_settings_once(ctx):
    """Jednorazowo (na prośbę admina): każde konto z portfelem Solany dostaje autopilota – kto go nie miał,
    ma od razu poprawiony skład traderów (nieaktywni / tracący → najlepsi z rankingu); kto miał – od razu
    przechodzi rotację z nowymi zasadami. 🛡 Bezpieczników nie zmienia. Każdy dostaje wiadomość, co się stało."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("best_settings_v1"):
        return
    s.db.set_state("best_settings_v1", str(now_ms()))
    from .axiom import portfolio
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        try:
            bal = (await portfolio(s, u))["total"]
            if not bal or bal <= 0:
                continue
            if u["sol_autopilot"]:
                res = rotate(s, u, bal)
                if res["on"] or res["off"]:
                    autocfg.apply(s, s.db.get_user(u["tg_id"]), bal)
                head = "🚀 <b>AUTOPILOT</b> – nowe, ostrzejsze zasady wycinania tracących traderów"
            else:
                res = await enable(s, u, bal)
                head = ("🚀 <b>AUTOPILOT ZAROBKU WŁĄCZONY</b> przez admina – Twoi traderzy prawie nie handlowali, "
                        "więc bot dobrał aktywnych z rankingu i ustawił kwoty pod saldo (🛡 bezpieczniki bez zmian). "
                        "Wyłączysz go w Opcjach.")
            log.info("Najlepsze ustawienia %s: +%d / -%d", u["tg_id"], len(res["on"]), len(res["off"]))
            await s.sol.say(u, "auto", head + "\n" + html.escape(summary({**res, "kept": []})))
        except Exception:  # noqa: BLE001
            log.exception("Najlepsze ustawienia %s", u["tg_id"])


async def autopilot_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    from .axiom import portfolio
    for u in s.db._q("SELECT * FROM users WHERE approved = 1 AND sol_autopilot = 1"):
        key = f"autopilot:{u['tg_id']}"
        if now_ms() - int(s.db.get_state(key) or 0) < 2.75 * 3600 * 1000:  # 🔁 co ~3 h (szybsza reakcja)
            continue
        s.db.set_state(key, str(now_ms()))
        try:
            bal = autocfg.planned_balance(s, u["tg_id"], (await portfolio(s, u))["total"])
            res = rotate(s, u, bal)
            if res["on"] or res["off"]:
                autocfg.apply(s, s.db.get_user(u["tg_id"]), bal)  # nowi traderzy → ich własne %
                await s.sol.say(u, "auto", "🚀 <b>AUTOPILOT ZAROBKU</b> – poprawiam skład traderów\n"
                                + html.escape(summary({**res, "kept": []})))
        except Exception:  # noqa: BLE001
            log.exception("Autopilot %s", u["tg_id"])


async def demo_unstick_once(ctx):
    """Jednorazowo: na ćwiczeniach zamyka pozycje w tokenach bez trasy wymiany (blokowały limit pozycji)."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("demo_unstick_v1"):
        return
    s.db.set_state("demo_unstick_v1", str(now_ms()))
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        try:
            closed = await s.sol.paper_close_unroutable(u)
            if closed:
                log.info("Ćwiczenia %s: zamknięto %d pozycji bez trasy wymiany", u["tg_id"], len(closed))
                await s.sol.say(u, "auto", "🧪 <b>ĆWICZENIA</b> – zamknąłem po cenie rynkowej tokeny, których nie da się "
                                "sprzedać przez giełdę (np. tokenizowane akcje): " + html.escape(", ".join(closed))
                                + "\nBlokowały miejsca na nowe pozycje. Od teraz ćwiczenia nie kupują tokenów, których "
                                "nie dałoby się kupić na prawdziwych SOL.")
        except Exception:  # noqa: BLE001
            log.exception("Ćwiczenia – zamykanie tokenów bez trasy %s", u["tg_id"])


async def profit_v2_once(ctx):
    """Jednorazowo: konta z autopilotem dostają drabinkę zysku i „nie goń ceny” (strategia, nie bezpieczniki)."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("profit_v2"):
        return
    s.db.set_state("profit_v2", str(now_ms()))
    for u in s.db._q("SELECT * FROM users WHERE approved = 1 AND sol_autopilot = 1"):
        try:
            s.db.update_user(u["tg_id"], sol_ladder=1, sol_chase=30)
            await s.sol.say(u, "auto", "🚀 <b>AUTOPILOT – NOWE</b>\n"
                            "• <b>Drabinka zysku:</b> przy ×5, ×10 i ×25 bot sam sprzedaje po 25% pozycji – zysk z "
                            "rakiety ląduje w SOL, reszta leci dalej z ruchomym stopem.\n"
                            "• <b>Nie goń ceny:</b> gdy kopia byłaby o 30%+ droższa niż wejście tradera – pomija.\n"
                            "Zmienisz w ⚙️ Opcjach. 🛡 Bezpieczniki bez zmian.")
        except Exception:  # noqa: BLE001
            log.exception("Autopilot v2 %s", u["tg_id"])


WIKIX = 8576926628  # konto na prawdziwych SOL – ustawienia pod saldo ~200 zł na prośbę właściciela (20.11)
WIKIX_SETUP = {
    # 🛡 bezpieczniki pod ~0,75 SOL (200 zł)
    "sol_safe": 1, "sol_min_buy": 0.015, "sol_max": 0.12, "sol_max_pos": 8, "sol_day_limit": 0.15, "sol_streak": 5,
    "sol_max_adds": 3, "sol_reserve_pct": 20,
    # strategia
    "sol_risk": 0, "sol_buy_mode": "pct", "sol_sl": 50, "sol_tp": 0, "sol_turbo": 1, "sol_priority": 100_000,
    "sol_slippage_bps": 1500, "sol_no_add": 0,
}


async def wikix_setup_once(ctx):
    """Jednorazowo: najlepsze ustawienia dla Wikixa pod saldo ~200 zł (autopilot, 🤖 Auto, bezpieczniki)."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("wikix_setup_v1"):
        return
    u = s.db.get_user(WIKIX)
    if not u:
        return
    s.db.set_state("wikix_setup_v1", str(now_ms()))
    try:
        from .api import saved_list, switch
        from .axiom import portfolio
        # trader kupujący zamrożone tokeny (FIONA, XMR) – wyłączony
        dropped = []
        for it in saved_list(s, WIKIX):
            if it["status"] == "copy" and "DOBRY PO" in (it["name"] or ""):
                switch(s, WIKIX, it["wallet"], False)
                dropped.append(it["name"])
        s.db.update_user(WIKIX, **WIKIX_SETUP)
        bal = (await portfolio(s, s.db.get_user(WIKIX)))["total"] or 0
        res = await enable(s, s.db.get_user(WIKIX), max(bal, 0.05))  # kwoty pod prawdziwe saldo – 🤖 Auto przeliczy po dopłacie
        s.db.update_user(WIKIX, **WIKIX_SETUP)  # bezpieczniki i ryzyko zostają takie, jak wyżej
        log.info("Wikix: ustawienia pod 200 zł – saldo %.4f SOL, traderzy +%d / -%d, wyłączony: %s",
                 bal, len(res["on"]), len(res["off"]), dropped)
        await s.sol.say(s.db.get_user(WIKIX), "auto",
                        "⚙️ <b>NOWE USTAWIENIA</b> (ustawione przez admina pod saldo ok. 200 zł ≈ 0,75 SOL)\n"
                        "🛡 <b>Bezpieczniki:</b> filtr pułapek włączony · min. zakup 0,015 SOL · maks. 0,12 SOL na zakup · "
                        "do 8 pozycji · limit straty dnia 0,15 SOL · przerwa po 5 stratach · maks. 3 zakupy tokena · "
                        "rezerwa 20%\n"
                        "🚀 <b>Autopilot</b> dobiera 4 aktywnych traderów i co 6 h wycina tracących · 🤖 kwoty same "
                        "dopasowują się do salda (po dopłacie przeliczą się same)\n"
                        "📈 Drabinka zysku ×5/×10/×25 · zwrot wkładu ×2 · ruchomy stop 40% · stop-loss −50% · "
                        "nie goń ceny > 30% · wyjście z martwych pozycji po 90 min\n"
                        + (f"⚪ Wyłączony trader (kupował zamrożone tokeny): {html.escape(', '.join(dropped))}\n" if dropped else "")
                        + html.escape(summary({**res, "kept": []}))
                        + "\n\nWszystko zmienisz w ⚙️ Opcjach.")
    except Exception:  # noqa: BLE001
        log.exception("Wikix: ustawienia")


WIKIX_PLAN_SOL = 0.75  # 200 zł – Wikix dopiero dopłaca


async def wikix_plan_once(ctx):
    """Jednorazowo: Wikix dopiero doładuje 200 zł – kwoty i skład traderów pod 0,75 SOL już teraz (saldo planowane
    na 7 dni albo do wpłaty), bezpieczniki jak w WIKIX_SETUP."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("wikix_plan_v2"):
        return
    u = s.db.get_user(WIKIX)
    if not u:
        return
    s.db.set_state("wikix_plan_v2", str(now_ms()))
    try:
        autocfg.set_planned_balance(s, WIKIX, WIKIX_PLAN_SOL)
        s.db.update_user(WIKIX, **WIKIX_SETUP)
        res = await enable(s, s.db.get_user(WIKIX), WIKIX_PLAN_SOL)
        s.db.update_user(WIKIX, **WIKIX_SETUP)
        u = s.db.get_user(WIKIX)
        log.info("Wikix: plan 0,75 SOL – traderzy +%d / -%d, kwota %s SOL / %s%%", len(res["on"]), len(res["off"]),
                 u["sol_buy"], u["sol_pct"])
        await s.sol.say(u, "auto",
                        "💰 <b>GOTOWE POD 200 ZŁ</b> – kwoty i traderzy ustawione już teraz pod ok. 0,75 SOL, "
                        "więc po doładowaniu bot od razu gra pełną stawką.\n"
                        + html.escape(summary(res))
                        + "\n\nDopóki saldo jest niższe, bot pomija zakupy („za mało SOL”) – nic nie tracisz. "
                        "Doładuj na adres portfela z 👛 Portfel.")
    except Exception:  # noqa: BLE001
        log.exception("Wikix: plan 200 zł")



async def chase_v2_once(ctx):
    """Jednorazowo (23.11): „nie goń ceny” 30% → 10% i próg flippera 30 → 45 s u wszystkich, którzy mieli wartości
    ustawione przez automat. Analiza Draqs: trader zarabiał 6–12%, a kopia wchodziła 12–29% drożej (jego zakup +
    szybsze boty podbijają cenę w ~3 s) – każda taka kopia to pewna strata. Bezpieczników nie zmienia."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("chase_v2"):
        return
    s.db.set_state("chase_v2", str(now_ms()))
    n = 0
    for u in s.db._q("SELECT tg_id, sol_chase, sol_min_hold FROM users WHERE approved = 1"):
        upd = {}
        if u["sol_chase"] in (0, 30):
            upd["sol_chase"] = 10
        if u["sol_min_hold"] in (0, 20, 30):
            upd["sol_min_hold"] = 45
        if upd:
            s.db.update_user(u["tg_id"], **upd)
            n += 1
    log.info("Nie goń ceny 10%% / flipper 45 s: zmienione konta %d", n)


async def safety_v3_once(ctx):
    """Jednorazowo (23.11, na prośbę właściciela – zmiana bezpieczników dla wszystkich): dzienny limit straty 18%
    salda i przerwa po 5 stratach z rzędu. Ćwiczenia: limit od kapitału startowego (saldo demo było zawyżone przez
    fikcyjne zakupy po cenie snajpera). Każdy dostaje wiadomość."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("safety_v3"):
        return
    s.db.set_state("safety_v3", str(now_ms()))
    from .axiom import portfolio
    done = 0
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        try:
            live = s.sol.is_live(u)
            total = (await portfolio(s, u))["total"] or 0
            start = u["sol_paper_start"] if "sol_paper_start" in u.keys() and u["sol_paper_start"] else 5.0
            base = total if live else min(total or start, start)
            if base <= 0:
                continue
            limit = round(max(base * 0.18, 0.02), 3)
            s.db.update_user(u["tg_id"], sol_day_limit=limit, sol_streak=5)
            done += 1
            await s.sol.say(s.db.get_user(u["tg_id"]), "auto",
                            "🛡 <b>NOWE BEZPIECZNIKI</b> (ustawione przez admina)\n"
                            f"• dzienny limit straty: <b>{limit:g} SOL</b> (18% salda"
                            + ("" if live else " startowego ćwiczeń") + ") – po nim bot nie kupuje do jutra\n"
                            "• przerwa 30 min po <b>5 stratach z rzędu</b>\n"
                            "• nie goń ceny: pomija zakup droższy o > 10% niż u tradera\n"
                            "• pauza tradera na 2 h po 3 stratach z rzędu na jego kopiach\n"
                            "• flipperzy (trzymają &lt; 45 s) nie są kopiowani\n"
                            "Zmienisz w ⚙️ Opcje → 🛡 Bezpieczniki.")
        except Exception:  # noqa: BLE001
            log.exception("Bezpieczniki v3 %s", u["tg_id"])
    log.info("Bezpieczniki v3 (limit dnia 18%%, przerwa po 5 stratach): kont %d", done)


async def reset_demo_2sol_once(ctx):
    """Jednorazowo (24.11, na prośbę właściciela): ćwiczenia od nowa – każdy 2 wirtualne SOL (stare wyniki były
    zawyżone przez zakupy po cenie snajpera). Na kontach grających na ćwiczeniach: autopilot dobiera najlepszych
    aktywnych traderów i ustawia kwoty pod 2 SOL, bezpieczniki pod 2 SOL. Prawdziwych SOL nie rusza."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("reset_demo_2sol"):
        return
    s.db.set_state("reset_demo_2sol", str(now_ms()))
    done = []
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        uid = u["tg_id"]
        try:
            s.db.update_user(uid, sol_paper_start=2.0)
            s.sol.paper_reset(uid)
            s.db._q("DELETE FROM sol_equity WHERE tg_id = ? AND paper = 1", (uid,))
            if s.sol.is_live(u):
                continue  # prawdziwe SOL – tylko czyste ćwiczenia w tle, traderzy bez zmian
            s.db.update_user(uid, session_sol0=2.0, session_start=now_ms() if u["active"] else u["session_start"])
            res = await enable(s, s.db.get_user(uid), 2.0)
            s.db.update_user(uid, sol_day_limit=0.36, sol_streak=5, sol_safe=1)
            u2 = s.db.get_user(uid)
            done.append((uid, len(res["on"]), len(res["off"])))
            await s.sol.say(u2, "auto",
                            "🧪 <b>ĆWICZENIA OD NOWA – 2 SOL</b>\n"
                            "Stare wyniki ćwiczeń były zawyżone (zakupy świeżych tokenów po cenie snajpera, niemożliwe na "
                            "prawdziwych SOL) – od teraz ćwiczenia liczą dokładnie jak prawdziwe pieniądze.\n"
                            "🚀 Autopilot dobrał najlepszych aktywnych traderów i ustawił kwoty pod 2 SOL "
                            f"(ok. {u2['sol_buy']:g} SOL na zakup).\n🛡 Limit straty dnia 0,36 SOL · przerwa po 5 stratach.\n"
                            + html.escape(summary({**res, "kept": []})))
        except Exception:  # noqa: BLE001
            log.exception("Ćwiczenia od nowa %s", uid)
    log.info("Ćwiczenia od nowa (2 SOL): konta z traderami %s", done)


async def demo_traders_check_once(ctx):
    """Jednorazowo po resecie ćwiczeń: skład traderów każdego konta do logów (czas od ostatniego ruchu) i
    uzupełnienie do celu, jeśli ktoś ma za mało aktywnych traderów."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("demo_traders_check"):
        return
    s.db.set_state("demo_traders_check", str(now_ms()))
    from .api import saved_list
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        try:
            live = s.sol.is_live(u)
            if not live:
                res = rotate(s, u, 2.0)
                if res["on"] or res["off"]:
                    autocfg.apply(s, s.db.get_user(u["tg_id"]), 2.0)
                    log.info("Ćwiczenia %s: uzupełniono traderów +%s / -%s", u["tg_id"], res["on"], res["off"])
            rows = []
            for it in saved_list(s, u["tg_id"]):
                if it["status"] != "copy" or not it["lid"]:
                    continue
                last = _his_last(s, it["lid"])
                rows.append(f"{(it['name'] or '')[:28]} ({'%.0f h' % ((now_ms() - last) / 3600_000) if last else 'brak ruchu'})")
            log.info("Skład traderów %s (%s): %s", u["tg_id"], "prawdziwe" if live else "ćwiczenia", "; ".join(rows) or "BRAK")
        except Exception:  # noqa: BLE001
            log.exception("Sprawdzenie traderów %s", u["tg_id"])


async def rebuild_demo_traders_once(ctx):
    """Jednorazowo (24.11, prośba właściciela „ustaw dobrych traderów”): na ćwiczeniach skład od zera z aktualnej
    czołówki (aktywni ≤ 36 h, na plusie 7 i 30 dni, nie flipperzy). Wyłącza traderów spoza czołówki – także z ⭐,
    bo zostawały nieaktywne od kilku dni. Prawdziwych SOL nie rusza."""
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None) or s.db.get_state("rebuild_demo_traders_v2"):
        return
    s.db.set_state("rebuild_demo_traders_v2", str(now_ms()))
    from .api import saved_list, switch
    why = Counter()
    cands = candidates(s, why)
    log.info("Czołówka do kopiowania: %d kandydatów (%s): %s", len(cands), dict(why),
             "; ".join(f"{(c.get('name') or c['wallet'][:6])[:20]} ({c.get('activity')})" for c in cands[:10]))
    if not cands:
        return
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id WHERE u.approved = 1"):
        try:
            if s.sol.is_live(u):
                continue
            uid = u["tg_id"]
            target = target_count(2.0)
            if (u["sol_risk"] if "sol_risk" in u.keys() else 0) > 0:
                target = min(target, 3)
            want = {c["wallet"] for c in cands[:target]}
            off = []
            for it in saved_list(s, uid):
                if it["status"] == "copy" and it["wallet"] not in want:
                    switch(s, uid, it["wallet"], False)
                    s.db._q("UPDATE sol_saved SET priority = 0 WHERE tg_id = ? AND wallet = ?", (uid, it["wallet"]))
                    off.append(it["name"] or it["wallet"][:6])
            res = rotate(s, s.db.get_user(uid), 2.0)
            autocfg.apply(s, s.db.get_user(uid), 2.0)
            now_copy = [f"{(it['name'] or '')[:26]} ({'%.0f h' % ((now_ms() - x) / 3600_000) if (x := _his_last(s, it['lid'])) else 'nowy'})"
                        for it in saved_list(s, uid) if it["status"] == "copy" and it["lid"]]
            log.info("Ćwiczenia %s: nowy skład %s (dodani %d, wyłączeni %d)", uid, "; ".join(now_copy), len(res["on"]), len(off))
            if res["on"]:
                await s.sol.say(s.db.get_user(uid), "auto",
                                "🚀 <b>NOWY SKŁAD TRADERÓW</b> (ćwiczenia)\nTylko aktywni z czołówki rankingu – handlują "
                                "codziennie, są na plusie w 7 i 30 dniach, trzymają tokeny dłużej niż sekundy.\n"
                                "🟢 " + html.escape(", ".join(res["on"]))
                                + (f"\n⚪ Wyłączeni (nieaktywni albo spoza czołówki): {len(off)}" if off else ""))
        except Exception:  # noqa: BLE001
            log.exception("Nowy skład traderów %s", u["tg_id"])
