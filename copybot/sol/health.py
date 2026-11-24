"""🩺 Stan kopiowanych traderów – JEDNO miejsce, które mówi, czy bot naprawdę kopiuje danego tradera i dlaczego nie.

Liczy dokładnie te rzeczy, które decydują o kopiowaniu (pauza po 3 stratach, próg flippera, brak handlu, straty,
brak tras wymiany, pominięcia z 24 h), żeby aplikacja pokazywała prawdziwe powody, a nie zgadywała."""
import re
from collections import Counter

from ..db import now_ms, ref_id

DAY_MS = 24 * 3600 * 1000


def _ago(ms: int) -> str:
    m = max(int(ms // 60_000), 0)
    return f"{m} min" if m < 60 else f"{m // 60} h" if m < 2880 else f"{m // 1440} dni"


def _reason(info: str | None) -> str:
    """Powód pominięcia bez liczb (żeby „kupiłeś już 3×” i „kupiłeś już 4×” liczyły się razem)."""
    t = (info or "nieznany powód").split(" – ")[0].split(" (")[0]
    return re.sub(r"\d+([.,]\d+)?", "X", t)[:70]


def overview(s, u, lids: list[int]) -> dict[int, dict]:
    """lid → {state, issues[{level, text}], me{pnl, sells, wins, buys}, skipped{n, top}, his_last}.
    state: copy (bot kopiuje) | paused (włączony, ale bot teraz nie kopiuje) | watch | None."""
    if not lids:
        return {}
    se, uid = s.sol, u["tg_id"]
    paper = not se.is_live(u)
    now = now_ms()
    q = ",".join("?" * len(lids))
    me = {r["leader_id"]: r for r in s.db._q(
        f"""SELECT leader_id, SUM(side = 'sell' AND pnl IS NOT NULL) sells, SUM(pnl > 0) wins,
                   COALESCE(SUM(pnl), 0) pnl, SUM(side = 'buy') buys
            FROM sol_trades WHERE tg_id = ? AND paper = ? AND status = 'filled' AND ts >= ? AND leader_id IN ({q})
            GROUP BY leader_id""", (uid, int(paper), now - 7 * DAY_MS, *lids))}
    skips: dict[int, Counter] = {}
    for r in s.db._q(f"""SELECT leader_id, info, status, COUNT(*) n FROM sol_trades WHERE tg_id = ? AND ts >= ?
                         AND status IN ('skipped', 'error') AND leader_id IN ({q}) GROUP BY leader_id, info, status""",
                     (uid, now - DAY_MS, *lids)):
        skips.setdefault(r["leader_id"], Counter())[_reason(r["info"])] += r["n"]
    attempts = {r["leader_id"]: (r["n"], r["bad"] or 0) for r in s.db._q(
        f"""SELECT leader_id, COUNT(*) n, SUM(info LIKE '%brak trasy%' OR info LIKE '%zamrożon%' OR status = 'error') bad
            FROM sol_trades WHERE tg_id = ? AND ts >= ? AND leader_id IN ({q}) GROUP BY leader_id""",
        (uid, now - 2 * DAY_MS, *lids))}
    his_last = {-r["tg_id"]: r["t"] for r in s.db._q(
        f"SELECT tg_id, MAX(ts) t FROM sol_trades WHERE status = 'filled' AND tg_id IN ({q}) GROUP BY tg_id",
        tuple(ref_id(lid) for lid in lids))}
    follows = {r["leader_id"]: r for r in s.db._q(
        f"SELECT leader_id, copy, created_at FROM follows WHERE tg_id = ? AND leader_id IN ({q})", (uid, *lids))}
    min_hold = (u["sol_min_hold"] if "sol_min_hold" in u.keys() else 0) or 0
    out = {}
    for lid in lids:
        f = follows.get(lid)
        m = me.get(lid)
        mine = {"pnl": round(m["pnl"], 4) if m else 0.0, "sells": (m["sells"] or 0) if m else 0,
                "wins": (m["wins"] or 0) if m else 0, "buys": (m["buys"] or 0) if m else 0}
        issues, blocked = [], False
        pause = se.trader_pause_left(u, lid) if f and f["copy"] else None
        if pause:
            issues.append({"level": "pause", "text": f"⏸ pauza jeszcze ~{pause} min – 3 ostatnie kopie na minusie"})
            blocked = True
        hs = se.hold_stats(lid)
        if min_hold and hs["n"] >= 5 and hs["median_s"] is not None and hs["median_s"] < min_hold:
            issues.append({"level": "block", "text": f"⚡ flipper – trzyma ~{hs['median_s']:.0f} s (Twój próg "
                                                     f"{min_hold:g} s), bot go nie kopiuje"})
            blocked = True
        last = his_last.get(lid)
        since = max(last or 0, (f["created_at"] if f else 0) or 0)
        if since and now - since > 2 * DAY_MS:
            issues.append({"level": "warn", "text": f"😴 nie handluje od {_ago(now - (last or since))}"})
        if mine["sells"] >= 3 and mine["pnl"] < 0:
            issues.append({"level": "warn", "text": f"📉 tracisz na nim {mine['pnl']:+.3f} SOL w 7 dni "
                                                    f"({mine['wins']}/{mine['sells']} na plus)"})
        n, bad = attempts.get(lid, (0, 0))
        if bad >= 5 and bad / n >= 0.5:
            issues.append({"level": "warn", "text": "🚫 większości jego zakupów nie da się skopiować "
                                                    "(brak trasy / zamrożone tokeny / odrzucone przez sieć)"})
        sk = skips.get(lid, Counter())
        n_sk = sum(sk.values())
        top = sk.most_common(1)[0][0] if sk else None
        if n_sk >= 3 and not blocked:
            issues.append({"level": "info", "text": f"ℹ️ pominięte w 24 h: {n_sk} (najczęściej: {top})"})
        state = None if not f else ("watch" if not f["copy"] else "paused" if blocked else "copy")
        out[lid] = {"state": state, "issues": issues, "me": mine, "skipped": {"n": n_sk, "top": sk.most_common(5)},
                    "his_last": last}
    return out


def unique_names(items: list[dict]) -> None:
    """Ta sama nazwa u kilku traderów (np. trzy „Top 5 ocena+aktywność…”) → dopisuje końcówkę adresu."""
    cnt = Counter((it.get("name") or "").strip() for it in items if it.get("name"))
    for it in items:
        n = (it.get("name") or "").strip()
        if n and cnt[n] > 1 and not n.endswith(it["wallet"][-4:]):
            it["name"] = f"{n} · …{it['wallet'][-4:]}"
