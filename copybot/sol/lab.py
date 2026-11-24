"""🧪 Laboratorium traderów – zanim ktoś trafi do Twoich traderów, bot sprawdza, czy KOPIA na nim zarabia.

Jak to działa:
- członkowie: czołówka rankingu (aktywni, na plusie) + wszyscy, których ktoś teraz kopiuje,
- każdy ich ruch bot kopiuje na niby na osobnym koncie testowym, DOKŁADNIE jak ćwiczenia: z opóźnieniem, po cenie
  z chwili kopii, z poślizgiem, opłatami i „nie goń ceny” 10% (stała kwota 0,1 SOL, żeby traderzy byli porównywalni),
- sprzedaje, gdy on sprzedaje (taka część pozycji, jaką on sprzedał),
- mierzy: wynik kopii, o ile drożej wchodzimy niż on, o ile taniej wychodzimy, jak często cena ucieka, ile jego
  tokenów kończy jako rug pull,
- werdykt: 🏆 najlepszy / 🧪 testuję / ❌ odrzucony (z powodem). Autopilot wybiera tylko sprawdzonych.

Prawdziwe kopie mają pierwszeństwo: laboratorium wycenia przez Raptora (osobny limit), a Jupitera używa tylko, gdy
ten ma wolne miejsce; ma też limit ruchów na tradera na godzinę."""
import asyncio
import json
import logging
import os
import statistics
import time
from collections import deque

from ..db import now_ms
from .parser import LAMPORTS, SOL_MINT, Swap

log = logging.getLogger(__name__)

LAB_BASE = 1_000_000_000          # konta testowe: tg_id = -(LAB_BASE + id) – nie mieszają się z ludźmi ani wzorcami
LAB_SIZE = int(os.getenv("LAB_SIZE", "25"))  # ilu z czołówki testujemy naraz
BUY = 100_000_000                 # 0,1 SOL na każdy jego zakup – traderzy porównywalni niezależnie od jego kwot
START = 1000 * LAMPORTS           # wirtualne saldo konta testowego (nigdy nie brakuje)
PER_HOUR = int(os.getenv("LAB_PER_HOUR", "30"))  # maks. kopii testowych jednego tradera na godzinę
WINDOW_H = 72                     # wyniki z ostatnich 3 dni
MIN_CLOSED = 5                    # od tylu zamkniętych pozycji wynik jest wiarygodny
DAY_MS = 24 * 3600 * 1000


def lab_uid(lab_id: int) -> int:
    return -(LAB_BASE + int(lab_id))


class Lab:
    def __init__(self, s):
        self.s = s
        self._wallets: tuple[float, set[str]] = (0.0, set())
        self._rate: dict[str, deque] = {}
        self.sem = asyncio.Semaphore(2)
        self.stats_cache: dict = {"members": [], "updated": None}

    def paused(self) -> bool:
        """Główny węzeł RPC odmawia (bot jedzie na zapasowym) – laboratorium nie nasłuchuje: cały limit dla prawdziwych
        kopii."""
        se = self.s.sol
        w = getattr(se, "watcher", None)
        return bool(getattr(se.rpc, "degraded", False) or (w is not None and getattr(w, "mode", "") == "zapasowy"))

    # ---------- członkowie ----------
    def wallets(self) -> set[str]:
        if self.paused():
            return set()
        at, ws = self._wallets
        if time.monotonic() - at > 30:
            ws = {r["wallet"] for r in self.s.db._q("SELECT wallet FROM lab WHERE active = 1")}
            self._wallets = (time.monotonic(), ws)
        return ws

    def member(self, wallet: str):
        r = self.s.db._q("SELECT * FROM lab WHERE wallet = ?", (wallet,))
        return r[0] if r else None

    def account(self, lab_id: int) -> dict:
        """Konto testowe – zasady jak na ćwiczeniach z rozsądnymi ustawieniami (poślizg 15%, nie goń ceny 10%)."""
        uid = lab_uid(lab_id)
        if self.s.sol.paper(uid).get(SOL_MINT, 0) < START // 2:
            self.s.sol.paper_apply(uid, {SOL_MINT: START - self.s.sol.paper(uid).get(SOL_MINT, 0)})
        return {"tg_id": uid, "lab": True, "sol_live": 0, "sol_slippage_bps": 1500, "sol_chase": 10,
                "sol_priority": 100_000, "sol_min_buy": 0, "sol_turbo": 0}

    def sync(self, wanted: list[dict]) -> tuple[int, int]:
        """wanted: [{wallet, name, source}] → nowi dołączają, nieobecni od 3 dni odchodzą (dane zostają)."""
        now = now_ms()
        have = {r["wallet"]: r for r in self.s.db._q("SELECT * FROM lab")}
        added = 0
        for w in wanted:
            r = have.get(w["wallet"])
            if not r:
                self.s.db._q("INSERT INTO lab (wallet, name, source, added_at, active) VALUES (?, ?, ?, ?, 1)",
                             (w["wallet"], w.get("name"), w.get("source"), now))
                added += 1
            else:
                limited = self.s.db._q("SELECT 1 FROM lab_events WHERE lab_id = ? AND status = 'limit' AND ts >= ?",
                                       (r["id"], now - 7 * DAY_MS))
                if limited:  # bot / scalper – nie wraca do nasłuchu przez tydzień
                    continue
                self.s.db._q("UPDATE lab SET active = 1, left_at = NULL, name = COALESCE(?, name), source = ? "
                             "WHERE id = ?", (w.get("name"), w.get("source"), r["id"]))
        want = {w["wallet"] for w in wanted}
        left = 0
        for w, r in have.items():
            if r["active"] and w not in want:
                # poza czołówką i nikt go nie kopiuje – po 3 dniach przestajemy nasłuchiwać (wyniki zostają)
                if not r["left_at"]:
                    self.s.db._q("UPDATE lab SET left_at = ? WHERE id = ?", (now, r["id"]))
                elif now - r["left_at"] > 3 * DAY_MS:
                    self.s.db._q("UPDATE lab SET active = 0 WHERE id = ?", (r["id"],))
                    left += 1
        self._wallets = (0.0, set())
        return added, left

    # ---------- kopia testowa ----------
    def _event(self, lab_id: int, sw: Swap, status: str, gap: float | None = None, info: str | None = None):
        self.s.db._q("INSERT INTO lab_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (lab_id, now_ms(), sw.mint, sw.side, sw.quote_amount if sw.quote_symbol == "SOL" else None,
                      status, gap, (info or "")[:200]))

    async def on_swap(self, wallet: str, sw: Swap, t_recv: float | None = None):
        if self.paused():
            return
        m = self.member(wallet)
        if not m or not m["active"]:
            return
        self.s.db._q("UPDATE lab SET last_seen = ? WHERE id = ?", (now_ms(), m["id"]))
        q = self._rate.setdefault(wallet, deque())
        now = time.monotonic()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= PER_HOUR:
            # bot / scalper: kopia i tak nie nadąży, a każdy jego ruch to zapytanie do RPC – przestajemy nasłuchiwać
            self.s.db._q("UPDATE lab SET active = 0, left_at = ? WHERE id = ?", (now_ms(), m["id"]))
            self._wallets = (0.0, set())
            log.info("🧪 Laboratorium: %s – ponad %d ruchów/h, koniec nasłuchu (bot/scalper)", wallet[:6], PER_HOUR)
            return self._event(m["id"], sw, "limit")
        q.append(now)
        try:
            async with self.sem:
                await self._copy(m, sw, t_recv)
        except Exception as e:  # noqa: BLE001
            log.debug("Laboratorium %s: %s", wallet[:6], e)

    async def _copy(self, m, sw: Swap, t_recv: float | None):
        from .engine import Skip
        from .chain import ChainError
        se = self.s.sol
        u = self.account(m["id"])
        uid = u["tg_id"]
        his = await se._trader_price(sw)  # lamporty za jednostkę tokena u niego
        if sw.side == "buy":
            if sw.quote_symbol == "SOL" and sw.quote_amount < 0.02:
                return self._event(m["id"], sw, "tiny", info="zakup testowy za grosze – pomijamy")
            n = self.s.db._q("""SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND mint = ? AND side = 'buy'
                                AND status = 'filled' AND ts >= ?""", (uid, sw.mint, now_ms() - DAY_MS))[0]["n"]
            if n >= 3:
                return self._event(m["id"], sw, "adds", info="ten token już 3× w 24 h")
            try:
                f = await se.execute(u, "buy", sw.mint, BUY, None, decimals=sw.decimals, fallback=sw)
            except Skip as e:
                return self._event(m["id"], sw, "chase" if "cena uciekła" in str(e) else "skip", info=str(e))
            except (ChainError, Exception) as e:  # noqa: BLE001
                return self._event(m["id"], sw, "error", info=str(e))
            ours = f.sol * LAMPORTS / (f.tokens * 10 ** sw.decimals) if f.tokens else None
            gap = (ours / his - 1) if (ours and his) else None
            se.record(uid, None, sw.signature, "buy", sw.mint, f.sol, f.tokens, "filled", f.note or "lab", True)
            return self._event(m["id"], sw, "filled", gap)
        held = se.paper(uid).get(sw.mint, 0)
        if held <= 0:
            return
        amount = held if sw.fraction > 0.97 else int(held * sw.fraction)
        if amount <= 0:
            return
        basis = se.cost_basis(uid, sw.mint, True)
        try:
            f = await se.execute(u, "sell", sw.mint, amount, None, decimals=sw.decimals, fallback=sw)
        except Exception as e:  # noqa: BLE001
            return self._event(m["id"], sw, "error", info=str(e))
        se.sell_pnl(basis, f)
        ours = f.sol * LAMPORTS / (f.tokens * 10 ** sw.decimals) if f.tokens else None
        gap = (ours / his - 1) if (ours and his) else None
        se.record(uid, None, sw.signature, "sell", sw.mint, f.sol, f.tokens, "filled", f.note or "lab", True, pnl=f.pnl)
        self._event(m["id"], sw, "filled", gap)

    # ---------- wyniki ----------
    async def compute(self) -> dict:
        """Wyniki każdego członka z ostatnich 72 h + werdykt. Pozycje otwarte liczone po cenie rynkowej
        (💀 martwe = 0) – inaczej trader, którego tokeny się rugują, wyglądałby dobrze."""
        se, now = self.s.sol, now_ms()
        since = now - WINDOW_H * 3600_000
        out = []
        for m in self.s.db._q("SELECT * FROM lab WHERE active = 1 OR last_seen >= ?", (since,)):
            uid = lab_uid(m["id"])
            tr = self.s.db._q("""SELECT side, mint, sol, tokens, pnl, ts FROM sol_trades WHERE tg_id = ?
                                 AND status = 'filled' AND ts >= ?""", (uid, since))
            ev = self.s.db._q("SELECT side, status, gap FROM lab_events WHERE lab_id = ? AND ts >= ?", (m["id"], since))
            buys = [r for r in tr if r["side"] == "buy"]
            sells = [r for r in tr if r["side"] == "sell" and r["pnl"] is not None]
            spent = sum(r["sol"] or 0 for r in buys)
            realized = sum(r["pnl"] for r in sells)
            # otwarte pozycje kupione w oknie: wartość teraz vs koszt
            hold = {mint: raw for mint, raw in se.paper(uid).items() if mint != SOL_MINT and raw > 0}
            bought = {r["mint"] for r in buys}
            hold = {k: v for k, v in hold.items() if k in bought}
            try:
                vals = await asyncio.wait_for(se.values_sol(hold, max_quotes=0), 15) if hold else {}
            except Exception:  # noqa: BLE001
                vals = {}
            open_cost = sum((se.cost_basis(uid, k, True) or (0, 0))[0] for k in hold)
            open_val = sum(vals.get(k, 0.0) for k in hold)
            pnl = realized + open_val - open_cost
            his_buys = [e for e in ev if e["side"] == "buy" and e["status"] not in ("tiny", "limit")]
            chase = sum(1 for e in his_buys if e["status"] == "chase")
            g_in = [e["gap"] for e in ev if e["side"] == "buy" and e["status"] == "filled" and e["gap"] is not None]
            g_out = [e["gap"] for e in ev if e["side"] == "sell" and e["status"] == "filled" and e["gap"] is not None]
            dead = sum(1 for k in bought if se.is_dead(k))
            limited = sum(1 for e in ev if e["status"] == "limit")
            d = {"wallet": m["wallet"], "name": m["name"], "source": m["source"], "active": bool(m["active"]),
                 "hours": round((now - m["added_at"]) / 3600_000, 1), "last_seen": m["last_seen"],
                 "buys": len(buys), "closed": len(sells), "wins": sum(1 for r in sells if r["pnl"] > 0),
                 "spent": round(spent, 4), "pnl": round(pnl, 4), "roi": round(pnl / spent, 4) if spent else None,
                 "open": len(hold), "his_buys": len(his_buys),
                 "chase_rate": round(chase / len(his_buys), 3) if his_buys else None,
                 "gap_in": round(statistics.median(g_in), 4) if g_in else None,
                 "gap_out": round(statistics.median(g_out), 4) if g_out else None,
                 "tokens": len(bought), "rug_rate": round(dead / len(bought), 3) if bought else None,
                 "limited": limited}
            d["verdict"], d["reason"] = verdict(d)
            d["score"] = score(d)
            out.append(d)
        order = {"best": 0, "testing": 1, "reject": 2}
        out.sort(key=lambda d: (order[d["verdict"]], -(d["score"] if d["score"] is not None else -1e9)))
        self.stats_cache = {"members": out, "updated": now}
        self.s.db.set_state("lab_stats", json.dumps(self.stats_cache))
        return self.stats_cache

    def results(self) -> dict:
        if not self.stats_cache.get("updated"):
            try:
                self.stats_cache = json.loads(self.s.db.get_state("lab_stats") or "{}") or {"members": []}
            except ValueError:
                pass
        return self.stats_cache

    def by_wallet(self) -> dict[str, dict]:
        return {d["wallet"]: d for d in self.results().get("members", [])}


def verdict(d: dict) -> tuple[str, str | None]:
    """🏆 best / 🧪 testing / ❌ reject + powód po ludzku. Odrzucamy typy, których kopia z zasady nie dogoni."""
    if d["limited"] >= 1:
        return "reject", "za dużo ruchów (bot / scalper) – kopia nie nadąża"
    if d["his_buys"] >= 6 and (d["chase_rate"] or 0) >= 0.5:
        return "reject", f"cena ucieka: {d['chase_rate'] * 100:.0f}% jego zakupów nie da się skopiować w limicie 10%"
    if d["buys"] >= 5 and (d["gap_in"] or 0) > 0.12:
        return "reject", f"wchodzimy średnio {d['gap_in'] * 100:.0f}% drożej niż on (snajper / wieloryb w małej puli)"
    if d["tokens"] >= 5 and (d["rug_rate"] or 0) >= 0.4:
        return "reject", f"{d['rug_rate'] * 100:.0f}% jego tokenów to rug pull"
    if d["closed"] >= MIN_CLOSED and (d["gap_out"] or 0) < -0.15:
        return "reject", f"wychodzimy średnio {-d['gap_out'] * 100:.0f}% taniej niż on (jego sprzedaż zbija cenę)"
    if d["closed"] >= MIN_CLOSED and d["roi"] is not None and d["roi"] < -0.10:
        return "reject", f"kopia traci {d['roi'] * 100:.0f}% w 3 dni"
    if d["closed"] >= MIN_CLOSED and d["hours"] >= 24 and d["roi"] is not None and d["roi"] > 0:
        return "best", None
    return "testing", None


def score(d: dict) -> float | None:
    """Wynik kopii (zwrot na zainwestowanym) ważony liczbą zamkniętych pozycji – 2 szczęśliwe trafy nie wygrywają
    z 20 solidnymi."""
    if d["roi"] is None:
        return None
    return round(d["roi"] * 100 * min(d["closed"] / 10, 1.0), 2)


# ---------- zadania ----------
async def lab_job(ctx):
    """Co 3 h: skład laboratorium – czołówka rankingu + kopiowani przez kogokolwiek."""
    s = ctx.application.bot_data["svc"]
    lab = getattr(getattr(s, "sol", None), "lab", None)
    if lab is None:
        return
    from . import autopilot as AP
    try:
        cands = AP.candidates(s, use_lab=False)[:LAB_SIZE]
    except Exception:  # noqa: BLE001
        log.exception("Laboratorium: kandydaci")
        cands = []
    wanted = [{"wallet": c["wallet"], "name": c.get("name"), "source": "ranking"} for c in cands]
    seen = {w["wallet"] for w in wanted}
    for r in s.db._q("""SELECT DISTINCT l.wallet, l.name FROM leaders l JOIN follows f ON f.leader_id = l.id
                        WHERE l.source = 'sol' AND l.status = 'active' AND f.copy = 1 AND l.wallet IS NOT NULL"""):
        if r["wallet"] not in seen:
            wanted.append({"wallet": r["wallet"], "name": r["name"], "source": "kopiowany"})
            seen.add(r["wallet"])
    added, left = lab.sync(wanted)
    log.info("🧪 Laboratorium: %d traderów (z czołówki %d), nowi %d, odeszli %d", len(lab.wallets()), len(cands),
             added, left)


async def lab_stats_job(ctx):
    """Co 15 min: wyniki laboratorium (z wartością otwartych pozycji)."""
    s = ctx.application.bot_data["svc"]
    lab = getattr(getattr(s, "sol", None), "lab", None)
    if lab is None:
        return
    try:
        res = await lab.compute()
        ms = res["members"]
        log.info("🧪 Laboratorium: %d wyników – 🏆 %d, 🧪 %d, ❌ %d", len(ms), sum(d["verdict"] == "best" for d in ms),
                 sum(d["verdict"] == "testing" for d in ms), sum(d["verdict"] == "reject" for d in ms))
    except Exception:  # noqa: BLE001
        log.exception("Laboratorium: wyniki")
