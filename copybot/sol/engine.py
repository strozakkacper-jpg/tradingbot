"""Kopiowanie portfeli Solany (traderzy z Axiom, Pump.fun, Raydium…) – demo i LIVE przez Jupitera."""
import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass

from solders.keypair import Keypair

from ..db import now_ms, ref_id
from ..stats import downsample, max_drawdown, period_return, trade_stats
from .chain import ChainError, Jupiter, Rpc
from .parser import LAMPORTS, SOL_MINT, Swap

log = logging.getLogger(__name__)
PAPER_TX_FEE = 200_000  # ~0,0002 SOL opłat sieci na transakcję w demo
MIN_BUY = 1_000_000  # 0,001 SOL
DAY_MS = 24 * 3600 * 1000


class Skip(Exception):
    pass


@dataclass
class Fill:
    sol: float
    tokens: float
    sig: str | None
    latency_ms: int
    confirm_s: float | None = None


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}"


def fmt_tokens(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if x >= 1000 else f"{x:.6g}"


def tx_link(sig: str) -> str:
    return f'<a href="https://solscan.io/tx/{sig}">Solscan</a>'


class SolEngine:
    def __init__(self, cfg, db, vault, rpc: Rpc, jup: Jupiter, notify):
        self.cfg, self.db, self.vault, self.rpc, self.jup, self.notify = cfg, db, vault, rpc, jup, notify
        self.sem = asyncio.Semaphore(8)
        self._bal: dict[str, tuple[float, int]] = {}  # pubkey -> (czas, lamporty) – szybsze zakupy LIVE
        self._report_cache: dict[int, tuple[float, dict]] = {}
        self.watcher = None  # SolWatcher (ustawiany w main)

    # ---------- baza ----------
    def wallet(self, tg_id: int, create: bool = True) -> tuple[str, Keypair] | None:
        rows = self.db._q("SELECT * FROM sol_wallets WHERE tg_id = ?", (tg_id,))
        if rows:
            return rows[0]["pubkey"], Keypair.from_base58_string(self.vault.dec(rows[0]["secret"]))
        if not create:
            return None
        kp = Keypair()
        self.db._q("INSERT OR IGNORE INTO sol_wallets VALUES (?, ?, ?, ?)",
                   (tg_id, str(kp.pubkey()), self.vault.enc(str(kp)), now_ms()))
        self.paper_reset(tg_id)
        return self.wallet(tg_id, create=False)

    def has_wallet(self, tg_id: int) -> bool:
        return bool(self.db._q("SELECT 1 FROM sol_wallets WHERE tg_id = ?", (tg_id,)))

    def paper(self, tg_id: int) -> dict[str, int]:
        return {r["mint"]: r["raw"] for r in self.db._q("SELECT mint, raw FROM sol_paper WHERE tg_id = ? AND raw > 0",
                                                         (tg_id,))}

    def paper_apply(self, tg_id: int, deltas: dict[str, int]):
        with self.db.lock, self.db.conn:
            for mint, d in deltas.items():
                self.db.conn.execute("INSERT INTO sol_paper VALUES (?, ?, ?) ON CONFLICT(tg_id, mint) "
                                     "DO UPDATE SET raw = MAX(raw + excluded.raw, 0)", (tg_id, mint, int(d)))

    def paper_reset(self, tg_id: int):
        with self.db.lock, self.db.conn:
            self.db.conn.execute("DELETE FROM sol_paper WHERE tg_id = ?", (tg_id,))
            self.db.conn.execute("DELETE FROM sol_trades WHERE tg_id = ? AND paper = 1", (tg_id,))
            self.db.conn.execute("INSERT INTO sol_paper VALUES (?, ?, ?)",
                                 (tg_id, SOL_MINT, int(self.cfg.sol_paper_start * LAMPORTS)))

    def record(self, tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, paper, sig=None, lat=None):
        self.db._q("""INSERT INTO sol_trades (tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, paper,
                      sig, latency_ms, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, int(paper), sig, lat, now_ms()))

    def history(self, tg_id: int, limit: int = 12) -> list:
        return self.db._q("""SELECT t.*, k.symbol FROM sol_trades t LEFT JOIN sol_tokens k ON k.mint = t.mint
                             WHERE t.tg_id = ? ORDER BY t.id DESC LIMIT ?""", (tg_id, limit))

    async def token_info(self, mint: str, decimals_hint: int | None = None) -> tuple[str, int]:
        if mint == SOL_MINT:
            return "SOL", 9
        rows = self.db._q("SELECT * FROM sol_tokens WHERE mint = ?", (mint,))
        if rows:
            return rows[0]["symbol"], rows[0]["decimals"]
        info = None
        try:
            info = await asyncio.wait_for(self.jup.token(mint), 3)
        except Exception:  # noqa: BLE001
            pass
        if info:
            self.db._q("INSERT OR REPLACE INTO sol_tokens VALUES (?, ?, ?)", (mint, info["symbol"], info["decimals"]))
            return info["symbol"], info["decimals"]
        return short(mint), decimals_hint or 0

    def leaders_for(self, wallet: str) -> list:
        return self.db._q("SELECT * FROM leaders WHERE source = 'sol' AND status = 'active' AND wallet = ?", (wallet,))

    def watched_wallets(self) -> set[str]:
        return {r["wallet"] for r in self.db._q(
            "SELECT DISTINCT wallet FROM leaders WHERE source = 'sol' AND status = 'active' AND wallet IS NOT NULL")}

    # ---------- salda ----------
    def is_live(self, u) -> bool:
        return bool(u["sol_live"])

    async def sol_lamports(self, u, max_age: float = 15) -> int:
        if not self.is_live(u):
            return self.paper(u["tg_id"]).get(SOL_MINT, 0)
        pub, _ = self.wallet(u["tg_id"])
        hit = self._bal.get(pub)
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        lam = await self.rpc.balance(pub)
        self._bal[pub] = (time.monotonic(), lam)
        return lam

    async def token_raw(self, u, mint: str) -> int:
        if not self.is_live(u):
            return self.paper(u["tg_id"]).get(mint, 0)
        pub, _ = self.wallet(u["tg_id"])
        return await self.rpc.token_balance(pub, mint)

    async def holdings(self, u) -> dict[str, int]:
        if not self.is_live(u):
            return {m: r for m, r in self.paper(u["tg_id"]).items() if m != SOL_MINT}
        pub, _ = self.wallet(u["tg_id"])
        return {m: raw for m, (raw, _) in (await self.rpc.token_accounts(pub)).items() if m != SOL_MINT}

    async def value_in_sol(self, mint: str, raw: int) -> float | None:
        try:
            q = await asyncio.wait_for(self.jup.quote(mint, SOL_MINT, raw, 1000), 5)
            return int(q["outAmount"]) / LAMPORTS
        except Exception:  # noqa: BLE001
            return None

    async def equity_sol(self, tg_id: int, balances: dict[str, int] | None = None) -> float:
        bal = balances if balances is not None else self.paper(tg_id)
        vals = await asyncio.gather(*(self.value_in_sol(m, r) for m, r in bal.items() if m != SOL_MINT))
        return bal.get(SOL_MINT, 0) / LAMPORTS + sum(v or 0 for v in vals)

    # ---------- reakcja na wymianę tradera ----------
    async def on_swap(self, wallet: str, sw: Swap, t_recv: float) -> Counter:
        res = Counter()
        for lead in self.leaders_for(wallet):
            res += await self.copy_leader(lead, sw, t_recv)
        return res

    async def copy_leader(self, lead, sw: Swap, t_recv: float) -> Counter:
        symbol, decimals = await self.token_info(sw.mint, sw.decimals)
        followers = [u for u in self.db.active_followers(lead["id"]) if self.has_wallet(u["tg_id"])]

        async def run(u):
            async with self.sem:
                return await self._copy(u, lead, sw, symbol, decimals, t_recv)

        res = Counter(await asyncio.gather(*(run(u) for u in followers)))
        line = self._leader_line(lead["name"], sw, symbol, decimals)
        for u in self.db.watchers(lead["id"]):
            await self.notify(u["tg_id"], f"👁 {line}")
            res["alert"] += 1
        asyncio.create_task(self._reference(lead, sw))
        log.info("Solana %s: %s %s → %s", lead["name"], sw.side, symbol, dict(res))
        return res

    @staticmethod
    def _leader_line(name: str, sw: Swap, symbol: str, decimals: int) -> str:
        verb = "kupił" if sw.side == "buy" else "sprzedał"
        pct = f" ({sw.fraction * 100:.0f}% {sw.quote_symbol if sw.side == 'buy' else 'pozycji'})" if sw.fraction else ""
        return (f"{name} {verb} {fmt_tokens(sw.token_raw / 10 ** decimals)} {symbol} "
                f"za {sw.quote_amount:.4g} {sw.quote_symbol}{pct}")

    async def _copy(self, u, lead, sw: Swap, symbol: str, decimals: int, t_recv: float) -> str:
        live = self.is_live(u)
        head = "🟣 " + self._leader_line(lead["name"], sw, symbol, decimals)
        try:
            if sw.side == "buy":
                amount = await self.buy_amount(u, sw)
            else:
                held = await self.token_raw(u, sw.mint)
                if held <= 0:
                    return "none"
                amount = held if sw.fraction > 0.97 else int(held * sw.fraction)
            fill = await self.execute(u, sw.side, sw.mint, amount, t_recv)
        except Skip as e:
            self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, 0, 0, "skipped", str(e), not live)
            await self.notify(u["tg_id"], f"{head}\n⚠️ Pominięto: {e}")
            return "skipped"
        except Exception as e:  # noqa: BLE001
            log.exception("Solana: kopiowanie dla %s", u["tg_id"])
            self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, 0, 0, "error", str(e)[:300], not live)
            await self.notify(u["tg_id"], f"{head}\n❌ Nie udało się: {str(e)[:200]}")
            return "error"
        self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, fill.sol, fill.tokens, "filled",
                    lead["name"], not live, fill.sig, fill.latency_ms)
        verb = "Kupiono" if sw.side == "buy" else "Sprzedano"
        speed = f"⚡ {fill.latency_ms} ms od wykrycia" + (f" · potwierdzone po {fill.confirm_s:.1f} s"
                                                         if fill.confirm_s is not None else "")
        await self.notify(u["tg_id"], f"{head}\n✅ {verb} {fmt_tokens(fill.tokens)} {symbol} za {fill.sol:.4f} SOL · "
                                      f"{'💰 LIVE' if live else '🧪 demo'}\n{speed}"
                                      + (f"\nhttps://solscan.io/tx/{fill.sig}" if fill.sig else ""))
        return "filled"

    async def buy_amount(self, u, sw: Swap) -> int:
        bal = await self.sol_lamports(u)
        reserve = int(self.cfg.sol_reserve * LAMPORTS) if self.is_live(u) else 0
        want = int(bal * sw.fraction * u["sol_mult"]) if u["sol_buy_mode"] == "ratio" else int(u["sol_buy"] * LAMPORTS)
        if u["sol_max"] > 0:
            want = min(want, int(u["sol_max"] * LAMPORTS))
        want = min(want, bal - reserve)
        if want < MIN_BUY:
            raise Skip(f"za mało SOL (saldo {bal / LAMPORTS:.4f})")
        return want

    async def execute(self, u, side: str, mint: str, amount_raw: int, t_recv: float | None = None) -> Fill:
        """Kupno: amount_raw = lamporty SOL. Sprzedaż: jednostki tokena."""
        t0 = t_recv or time.monotonic()
        in_mint, out_mint = (SOL_MINT, mint) if side == "buy" else (mint, SOL_MINT)
        _, decimals = await self.token_info(mint)
        try:
            quote = await self.jup.quote(in_mint, out_mint, amount_raw, int(u["sol_slippage_bps"]))
        except ChainError as e:
            raise Skip(f"brak trasy wymiany ({e})") from e
        out = int(quote["outAmount"])
        sol_raw, tok_raw = (amount_raw, out) if side == "buy" else (out, amount_raw)
        if not self.is_live(u):
            lat = int((time.monotonic() - t0) * 1000)
            if side == "buy":
                self.paper_apply(u["tg_id"], {SOL_MINT: -(amount_raw + PAPER_TX_FEE), mint: out})
            else:
                self.paper_apply(u["tg_id"], {mint: -amount_raw, SOL_MINT: out - PAPER_TX_FEE})
            return Fill(sol_raw / LAMPORTS, tok_raw / 10 ** decimals, None, lat)
        pub, kp = self.wallet(u["tg_id"])
        tx = await self.jup.swap(quote, kp, self.cfg.sol_priority_max_lamports)
        lat = int((time.monotonic() - t0) * 1000)  # do momentu wysłania transakcji
        self._bal.pop(pub, None)
        sig, confirm_s = await self.rpc.send_and_confirm(tx)
        return Fill(sol_raw / LAMPORTS, tok_raw / 10 ** decimals, sig, lat, confirm_s)

    async def sell_manual(self, u, mint: str, pct: int) -> Fill:
        held = await self.token_raw(u, mint)
        if held <= 0:
            raise Skip("nie masz tego tokena")
        fill = await self.execute(u, "sell", mint, held if pct >= 100 else int(held * pct / 100))
        self.record(u["tg_id"], None, None, "sell", mint, fill.sol, fill.tokens, "filled", "ręcznie",
                    not self.is_live(u), fill.sig, fill.latency_ms)
        return fill

    # ---------- portfel wzorcowy i wyniki tradera (w SOL) ----------
    def ensure_reference(self, lid: int):
        if self.db.get_state(f"sol_ref_start:{lid}") is None:
            rid = ref_id(lid)
            with self.db.lock, self.db.conn:
                self.db.conn.execute("DELETE FROM sol_paper WHERE tg_id = ?", (rid,))
                self.db.conn.execute("INSERT INTO sol_paper VALUES (?, ?, ?)",
                                     (rid, SOL_MINT, int(self.cfg.sol_paper_start * LAMPORTS)))
            self.db.set_state(f"sol_ref_start:{lid}", str(now_ms()))

    async def _reference(self, lead, sw: Swap):
        """Portfel wzorcowy: każda wymiana tradera 1:1 (ten sam % SOL / pozycji) – z niego liczymy wyniki."""
        try:
            self.ensure_reference(lead["id"])
            rid = ref_id(lead["id"])
            bal = self.paper(rid)
            if sw.side == "buy":
                amount = int(bal.get(SOL_MINT, 0) * sw.fraction)
            else:
                held = bal.get(sw.mint, 0)
                amount = held if sw.fraction > 0.97 else int(held * sw.fraction)
            if amount <= 0:
                return
            ref = {"tg_id": rid, "sol_live": 0, "sol_slippage_bps": 1000}
            fill = await self.execute(ref, sw.side, sw.mint, amount)
            self.record(rid, lead["id"], sw.signature, sw.side, sw.mint, fill.sol, fill.tokens, "filled", None, True)
        except Exception:  # noqa: BLE001
            log.debug("Portfel wzorcowy Solany %s", lead["id"], exc_info=True)

    async def report(self, lid: int, max_age: float = 60) -> dict:
        hit = self._report_cache.get(lid)
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        self.ensure_reference(lid)
        rid = ref_id(lid)
        now = now_ms()
        since = int(self.db.get_state(f"sol_ref_start:{lid}") or now)
        equity = await self.equity_sol(rid)
        start = self.cfg.sol_paper_start
        series = [(since, start)] + self.db.equity_series(rid, True) + [(now, equity)]
        rows = self.db._q("""SELECT t.*, k.symbol FROM sol_trades t LEFT JOIN sol_tokens k ON k.mint = t.mint
                             WHERE t.tg_id = ? AND t.status = 'filled' ORDER BY t.id""", (rid,))
        fills = [{"symbol": r["symbol"] or short(r["mint"]), "side": r["side"], "amount": r["tokens"] or 0,
                  "cost": r["sol"] or 0, "price": (r["sol"] / r["tokens"]) if r["tokens"] else 0, "ts": r["ts"]}
                 for r in rows]
        rep = {"id": lid, "since": since, "equity": equity, "start": start, "total": equity / start - 1,
               "d7": period_return(series, now - 7 * DAY_MS, equity), "d30": period_return(series, now - 30 * DAY_MS,
                                                                                          equity),
               "max_dd": max_drawdown([v for _, v in series]), **trade_stats(fills), "series": downsample(series),
               "recent": list(reversed(fills[-8:])), "unit": "SOL"}
        self._report_cache[lid] = (time.monotonic(), rep)
        return rep
