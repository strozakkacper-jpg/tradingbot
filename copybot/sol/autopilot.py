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

from ..db import now_ms, ref_id
from . import autocfg, leaderboard as LB

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000
COMBO = "score,active,green"
PROFIT_SETTINGS = {"sol_time_exit": 90, "sol_dev_exit": 1, "sol_smart": 1, "sol_auto_rank": 0, "sol_autocfg": 1,
                   "sol_x2": 1, "sol_trail": 40, "sol_min_hold": 30, "sol_min_lead": 0, "sol_safe": 1}


def target_count(balance: float) -> int:
    return 3 if balance < 0.5 else 4 if balance < 1 else 5 if balance < 3 else 6 if balance < 10 else 8


def candidates(s) -> list[dict]:
    """Najlepsi do kopiowania – świat (a gdy brak kluczy: ranking bota), od najlepszego."""
    for kind in ("world", "chain"):
        rows = LB.top(s, 7, COMBO, kind=kind)
        if rows:
            break
    else:
        return []
    m30 = {r["wallet"]: r for r in LB.top(s, 30, "score", kind=kind)}
    now = time.time()
    out = []
    for r in rows:
        pnl = r["pnl"] if r["pnl"] is not None else r["pnl_usd"]
        if not pnl or pnl <= 0 or r.get("bot"):
            continue
        if r.get("hold_s") is not None and r["hold_s"] < 30:
            continue  # flipper – kopia wchodzi za późno
        if (r.get("activity") or 0) < 35:
            continue  # rzadko handluje – mało okazji
        if r.get("last") and now - r["last"] > 36 * 3600:
            continue
        if r.get("win_rate") is not None and r["win_rate"] < 0.3:
            continue
        r30 = m30.get(r["wallet"])
        if r30 is not None and ((r30["pnl"] if r30["pnl"] is not None else r30["pnl_usd"]) or 0) <= 0:
            continue  # zarabia 7 dni, ale w 30 dniach traci – szczęśliwy tydzień
        out.append(r)
    return out


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
    off, kept, on, off_w = [], [], [], set()
    follows = {r["leader_id"]: r["created_at"] for r in s.db._q("SELECT leader_id, created_at FROM follows WHERE tg_id = ?",
                                                                 (uid,))}
    for it in items:
        name = it["name"] or it["wallet"][:6]
        r = mine.get(it["wallet"]) or {}
        why = None
        if not it["priority"]:
            if (r.get("sells") or 0) >= 5 and (r.get("pnl_7d") or 0) < 0:
                why = f"tracisz na nim {r['pnl_7d']:+.3f} SOL w 7 dni"
            elif it["lid"]:
                last = _his_last(s, it["lid"]) or 0
                since = follows.get(it["lid"]) or now_ms()
                if now_ms() - max(last, since) > 2 * DAY_MS:
                    why = "nie handluje od 2 dni"
                else:
                    h = it.get("hold") or {}
                    if h.get("n", 0) >= 3 and (h.get("median_s") or 999) < 20:
                        why = "flipper – kopia nie nadąża"
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
    need = max(target - len(kept), 0)
    have = {it["wallet"] for it in saved_list(s, uid) if it["status"] == "copy"}
    for i, r in enumerate(candidates(s)):
        if need <= 0:
            break
        w = r["wallet"]
        if w in have or w in off_w:
            continue
        prev = mine.get(w) or {}
        if (prev.get("sells") or 0) >= 5 and (prev.get("pnl_7d") or 0) < 0:
            continue  # już go kopiowałeś i traciłeś – nie wracamy do niego
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


async def autopilot_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    from .axiom import portfolio
    for u in s.db._q("SELECT * FROM users WHERE approved = 1 AND sol_autopilot = 1"):
        key = f"autopilot:{u['tg_id']}"
        if now_ms() - int(s.db.get_state(key) or 0) < 5.5 * 3600 * 1000:
            continue
        s.db.set_state(key, str(now_ms()))
        try:
            bal = (await portfolio(s, u))["total"]
            res = rotate(s, u, bal)
            if res["on"] or res["off"]:
                autocfg.apply(s, s.db.get_user(u["tg_id"]), bal)  # nowi traderzy → ich własne %
                await s.sol.say(u, "auto", "🚀 <b>AUTOPILOT ZAROBKU</b> – poprawiam skład traderów\n"
                                + html.escape(summary({**res, "kept": []})))
        except Exception:  # noqa: BLE001
            log.exception("Autopilot %s", u["tg_id"])
