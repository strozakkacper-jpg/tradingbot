"""Kopiowanie portfeli Solany (traderzy z Axiom, Pump.fun, Raydium…) – demo i LIVE przez Jupitera."""
import asyncio
import html
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass

from solders.keypair import Keypair

from ..db import now_ms, ref_id
from ..stats import downsample, max_drawdown, period_return, trade_stats
from .chain import ChainError, Jupiter, Rpc
from .parser import LAMPORTS, SOL_MINT, Swap

log = logging.getLogger(__name__)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def token_age_min(info: dict) -> float | None:
    """Wiek tokena w minutach (od pierwszej puli wymiany)."""
    from datetime import datetime, timezone
    ts = (info.get("firstPool") or {}).get("createdAt") or info.get("createdAt")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def is_frozen_error(s: str) -> bool:
    """Program tokenów: błąd 17 = AccountFrozen (konto tokena zamrożone przez twórcę)."""
    return "'Custom': 17}" in s or "AccountFrozen" in s or "account is frozen" in s.lower()


def friendly_error(e: Exception, u=None) -> str:
    """Błąd sieci / Jupitera / Pump.fun zwykłymi słowami."""
    s = str(e)
    slip = f" ({u['sol_slippage_bps'] / 100:g}%)" if u is not None and "sol_slippage_bps" in u.keys() else ""
    if "'Custom': 6001" in s or "Slippage" in s or "'Custom': 6002" in s or "'Custom': 6003" in s:
        return (f"cena uciekła poza tolerancję ceny{slip} w trakcie transakcji – nie weszła, pobrano tylko opłatę "
                "sieci. Przy szybkich memecoinach pomaga wyższa tolerancja (20–30%).")
    if "'Custom': 6005" in s:
        return "token właśnie przeszedł z Pump.fun na giełdę (PumpSwap/Raydium) – nie zdążyliśmy w tej sekundzie"
    if "429" in s or "Too many requests" in s:
        return "limit zapytań Jupitera – za dużo transakcji naraz (pomaga płatny klucz Jupitera albo mniej traderów)"
    if "brak potwierdzenia" in s:
        return "sieć nie potwierdziła transakcji w 45 s (przeciążenie) – sprawdź saldo, mogła nie wejść"
    if "insufficient" in s.lower() or "'Custom': 1}" in s:
        return "za mało SOL na transakcję i opłaty"
    if is_frozen_error(s):
        return ("🧊 token zamrożony – twórca tokena blokuje konta (pułapka albo token tylko dla wybranych, np. "
                "tokenizowane akcje). Nie da się go kupić ani sprzedać – bot więcej go nie próbuje.")
    if "transakcja odrzucona przez sieć" in s:
        return f"sieć odrzuciła transakcję ({s.split(': ', 1)[-1][:80]})"
    return s[:200]


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + full
PAPER_TX_FEE = 200_000  # ~0,0002 SOL opłat sieci na transakcję (portfele wzorcowe traderów)
ATA_RENT = 2_039_280  # depozyt za konto tokena w sieci Solana – wraca po zamknięciu pustego konta
BASE_FEE = 5_000  # podstawowa opłata sieci za transakcję
MIN_BUY = 1_000_000  # 0,001 SOL
LIVE_MIN_BUY = 10_000_000  # 0,01 SOL – mniejsze zakupy na prawdziwych SOL zjadają opłaty i depozyt konta
DUST_SOL = 0.0005  # resztka tokena warta mniej – spalamy i odzyskujemy depozyt konta (0,002 SOL)
DAY_MS = 24 * 3600 * 1000


def clamp_sell(amount_raw: int, out_raw: int, held: int) -> tuple[int, int]:
    """Sprzedaż najwyżej tego, co jest; przychód proporcjonalnie mniejszy. Nic nie zostało → Skip."""
    if amount_raw > held:
        out_raw = int(out_raw * held / amount_raw) if amount_raw else 0
        amount_raw = held
    if amount_raw <= 0:
        raise Skip("już sprzedane (inna sprzedaż tego tokena była chwilę wcześniej)")
    return amount_raw, out_raw


def is_launchpad(mint: str) -> bool:
    """Token z platformy startowej (Pump.fun, Bonk, …) – da się go kupić także bez trasy Jupitera."""
    return mint.endswith(("pump", "bonk", "moon", "bags", "boop"))


class Skip(Exception):
    pass


@dataclass
class Fill:
    sol: float
    tokens: float
    sig: str | None
    latency_ms: int
    confirm_s: float | None = None
    note: str | None = None  # np. „po cenie tradera” (ćwiczenia, gdy Jupiter nie ma trasy)
    pnl: float | None = None  # przy sprzedaży: zysk/strata w SOL względem kosztu zakupu
    pnl_pct: float | None = None


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}"


def fmt_tokens(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if x >= 1000 else f"{x:.6g}"


class SolEngine:
    def __init__(self, cfg, db, vault, rpc: Rpc, jup: Jupiter, notify):
        self.cfg, self.db, self.vault, self.rpc, self.jup, self.notify = cfg, db, vault, rpc, jup, notify
        self.sem = asyncio.Semaphore(8)
        self._bal: dict[str, tuple[float, int]] = {}  # pubkey -> (czas, lamporty) – szybsze zakupy LIVE
        self._report_cache: dict[int, tuple[float, dict]] = {}
        self._hold_cache: dict[int, tuple[float, dict]] = {}
        self._dec: dict[str, int] = {}  # mint -> miejsca po przecinku (z kont tokenów portfela)
        self._recent_buys: dict[str, dict[int, float]] = {}  # mint -> {trader: czas zakupu} – sygnał zbiorowy
        self.watcher = None  # SolWatcher (ustawiany w main)
        self.pump = None  # PumpPortal – zapasowa droga dla świeżych tokenów z Pump.fun (ustawiana w main)
        self.raptor = None  # Raptor (Solana Tracker) – druga droga wymiany, gdy Jupiter nie ma trasy / limit (main)
        self.jito = None  # Jito Block Engine – wysyłka w trybie turbo (ustawiana w main)
        self.timings: list[dict] = []  # ostatnie pomiary etapów transakcji LIVE (do panelu admina)

    # ---------- baza ----------
    def wallet(self, tg_id: int, create: bool = True) -> tuple[str, Keypair] | None:
        rows = self.db._q("SELECT * FROM sol_wallets WHERE tg_id = ?", (tg_id,))
        if rows:
            return rows[0]["pubkey"], Keypair.from_base58_string(self.vault.dec(rows[0]["secret"]))
        if not create:
            return None
        kp = Keypair()
        self.db._q("INSERT OR IGNORE INTO sol_wallets (tg_id, pubkey, secret, created_at) VALUES (?, ?, ?, ?)",
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

    def paper_start(self, tg_id: int) -> float:
        """Kwota startowa ćwiczeń: ustawiona przez użytkownika (np. tyle, ile naprawdę wpłacił) albo domyślna."""
        r = self.db._q("SELECT sol_paper_start FROM users WHERE tg_id = ?", (tg_id,)) if tg_id > 0 else []
        return r[0]["sol_paper_start"] if r and r[0]["sol_paper_start"] else self.cfg.sol_paper_start

    def paper_reset(self, tg_id: int):
        with self.db.lock, self.db.conn:
            self.db.conn.execute("DELETE FROM sol_paper WHERE tg_id = ?", (tg_id,))
            self.db.conn.execute("DELETE FROM sol_trades WHERE tg_id = ? AND paper = 1", (tg_id,))
        self.db._q("INSERT INTO sol_paper VALUES (?, ?, ?)", (tg_id, SOL_MINT, int(self.paper_start(tg_id) * LAMPORTS)))

    def paper_fee(self, u, lamports: int | None = None) -> int:
        """Ćwiczenia z prawdziwymi kosztami: opłata sieci + napiwek liczony jak na prawdziwej transakcji."""
        return BASE_FEE + self.trade_tip(u, lamports)

    def paper_trade(self, u, side: str, mint: str, amount_raw: int, out_raw: int) -> tuple[int, int]:
        """Transakcja na ćwiczeniach z kosztami jak na prawdziwych SOL: opłaty + depozyt za nowe konto tokena
        (wraca po sprzedaży całości, bo bot zamyka puste konta). Zwraca (ilość, wynik) NAPRAWDĘ wykonane:
        sprzedaż nie może przekroczyć tego, co masz – dwie sprzedaże tego samego tokena naraz (trader sprzedaje
        w dwóch transakcjach) liczyły się wcześniej obie od pełnego stanu i dopisywały SOL za więcej tokenów."""
        uid, fee = u["tg_id"], self.paper_fee(u, amount_raw if side == "buy" else out_raw)
        held = self.paper(uid).get(mint, 0)
        if side == "buy":
            rent = ATA_RENT if held <= 0 else 0
            self.paper_apply(uid, {SOL_MINT: -(amount_raw + fee + rent), mint: out_raw})
            return amount_raw, out_raw
        amount_raw, out_raw = clamp_sell(amount_raw, out_raw, held)
        refund = ATA_RENT - BASE_FEE if amount_raw >= held else 0
        self.paper_apply(uid, {mint: -amount_raw, SOL_MINT: out_raw - fee + refund})
        return amount_raw, out_raw

    def record(self, tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, paper, sig=None, lat=None,
               pnl=None):
        self.db._q("""INSERT INTO sol_trades (tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, paper,
                      sig, latency_ms, ts, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (tg_id, leader_id, leader_sig, side, mint, sol, tokens, status, info, int(paper), sig, lat, now_ms(),
                    pnl))
        from .events import BUS
        BUS.bump_user(tg_id)  # aplikacja odświeży się od razu

    @staticmethod
    def sell_pnl(basis, fill: "Fill"):
        """Zysk/strata ze sprzedaży: przychód minus średni koszt sprzedanej części (basis = przed sprzedażą)."""
        if basis and basis[1] > 0 and fill.tokens:
            cost = basis[0] * min(fill.tokens / basis[1], 1.0)
            if cost > 0:
                fill.pnl, fill.pnl_pct = fill.sol - cost, (fill.sol / cost - 1) * 100
        return fill

    async def say(self, u, kind: str, text: str, buttons: list | None = None):
        """Powiadomienie w czacie wg ustawienia: all = wszystko, sells = tylko sprzedaże z wynikiem, none = nic."""
        pref = u["sol_notify"] if "sol_notify" in u.keys() and u["sol_notify"] else "all"
        if pref == "none" or (pref == "sells" and kind not in ("sell", "auto")):
            return
        if kind in ("skip", "error") and buttons:
            # ten sam token, ten sam rodzaj wiadomości – najwyżej raz na 15 min (wcześniej: 148× „nie weszło” w dobę)
            tok = next((cb for row in buttons for _, cb in row if str(cb).startswith("tkn:")), None)
            if tok:
                seen = self.__dict__.setdefault("_said", {})
                reason = re.sub(r"[\d.,]+", "", text.split("Powód:", 1)[-1])[:60]  # ten sam powód (bez liczb)
                k = (u["tg_id"], kind, tok, reason)
                if time.time() - seen.get(k, 0) < 15 * 60:
                    return
                seen[k] = time.time()
                if len(seen) > 5000:
                    seen.clear()
        try:
            await self.notify(u["tg_id"], text, html=True, buttons=buttons)
        except TypeError:  # prosty notify (np. w testach) – sam tekst
            await self.notify(u["tg_id"], re.sub(r"<[^>]+>", "", text))

    @staticmethod
    def pnl_line(fill: "Fill") -> str:
        if fill.pnl is None:
            return ""
        if fill.pnl >= 0:
            return f"\n💰 <b>Zysk +{fill.pnl:.4f} SOL ({fill.pnl_pct:+.0f}%)</b>"
        return f"\n📉 <b>Strata {fill.pnl:.4f} SOL ({fill.pnl_pct:+.0f}%)</b>"

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
        # tylko portfele, które ktoś kopiuje albo obserwuje – każdy nasłuchiwany portfel to zapytania do RPC i Jupitera
        return {r["wallet"] for r in self.db._q(
            """SELECT DISTINCT l.wallet FROM leaders l JOIN follows f ON f.leader_id = l.id
               WHERE l.source = 'sol' AND l.status = 'active' AND l.wallet IS NOT NULL""")}

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
        accs = await self.rpc.token_accounts(pub)
        self._dec.update({m: dec for m, (_, dec) in accs.items()})  # miejsca po przecinku bez pytania Jupitera
        return {m: raw for m, (raw, _) in accs.items() if m != SOL_MINT}

    # ---------- 💀 martwe tokeny (rug pull: płynność wyciągnięta, nie da się sprzedać) ----------
    DEAD_RECHECK_MS = 30 * 60_000

    def _dead_reg(self) -> dict:
        """mint -> {"d": martwy?, "t": ostatnie sprawdzenie (ms), "since": od kiedy martwy (ms)} – w bazie,
        żeby przetrwało restart."""
        reg = self.__dict__.get("_dead")
        if reg is None:
            try:
                reg = json.loads(self.db.get_state("dead_tokens") or "{}")
            except ValueError:
                reg = {}
            self._dead = reg
        return reg

    def _dead_save(self):
        reg = self._dead_reg()
        old = now_ms() - 14 * DAY_MS
        for m in [m for m, v in reg.items() if v.get("t", 0) < old]:
            reg.pop(m, None)
        self.db.set_state("dead_tokens", json.dumps(reg))

    def is_dead(self, mint: str) -> bool:
        """Martwy = brak trasy w 2 kolejnych sprawdzeniach (≥ 5 min odstępu) – świeży token bez trasy przez
        chwilę nie trafi od razu do martwych."""
        v = self._dead_reg().get(mint) or {}
        return bool(v.get("d")) and v.get("n", 0) >= 2

    def dead_mints(self) -> set[str]:
        return {m for m, v in self._dead_reg().items() if v.get("d") and v.get("n", 0) >= 2}

    async def sell_route(self, mint: str, raw: int) -> bool | None:
        """Czy ten token da się sprzedać za SOL? Najpierw Raptor (poza limitem Jupitera), potem Jupiter.
        True = jest trasa, False = żadna droga nie ma trasy (martwy), None = nie wiadomo (błąd sieci)."""
        no_route = ("No routes", "no route", "not tradable", "ROUTE", "TRADABLE", "brak trasy", "multi-hop")
        raptor_none = False
        if self.raptor is not None:
            try:
                await asyncio.wait_for(self.raptor.quote(mint, SOL_MINT, raw, 3000), 6)
                return True
            except Exception as e:  # noqa: BLE001
                raptor_none = any(x in str(e) for x in no_route)
        if not raptor_none and self._jup_busy():
            return None  # Raptor nie odpowiedział, Jupiter zajęty – sprawdzimy następnym razem
        try:  # zadanie w tle – może poczekać na miejsce w limicie Jupitera
            await asyncio.wait_for(self.jup.quote(mint, SOL_MINT, raw, 3000, restrict=False), 15)
            return True
        except Exception as e:  # noqa: BLE001
            jup_none = any(x in str(e) for x in no_route)
        return False if jup_none and (raptor_none or self.raptor is None) else None

    async def check_dead(self, holdings: dict[str, int], limit: int = 8, first: set | None = None) -> list[str]:
        """Sprawdza trasy sprzedaży tokenów nie sprawdzanych od 30 min. Kolejność: podejrzane (1. raz bez trasy),
        potem `first` (np. tokeny na prawdziwych SOL), potem reszta. Zwraca nowo wykryte martwe."""
        reg, now, new, checked = self._dead_reg(), now_ms(), [], 0
        first = first or set()
        order = sorted(holdings.items(), key=lambda kv: (not (reg.get(kv[0]) or {}).get("d"), kv[0] not in first))
        for m, raw in order:
            was = reg.get(m) or {}
            every = 5 * 60_000 if was.get("d") and was.get("n", 0) < 2 else self.DEAD_RECHECK_MS  # podejrzany – szybciej
            if m == SOL_MINT or raw <= 0 or now - was.get("t", 0) < every:
                continue
            if checked >= limit:
                break
            checked += 1
            ok = await self.sell_route(m, raw)
            if ok is None:
                continue
            n = (was.get("n", 0) + 1) if not ok else 0
            reg[m] = {"d": not ok, "t": now, "n": n, "since": (was.get("since") or now) if not ok else None}
            if n == 2:
                new.append(m)
        if checked:
            self._dead_save()
            sus = sum(1 for v in reg.values() if v.get("d") and v.get("n", 0) < 2)
            log.info("💀 martwe tokeny: sprawdzono %d z %d, podejrzane %d, martwe razem %d, nowe %d",
                     checked, len(holdings), sus, len(self.dead_mints()), len(new))
        if new:
            log.info("💀 martwe tokeny (brak trasy sprzedaży): %s", ", ".join(m[:8] for m in new))
        return new

    async def value_in_sol(self, mint: str, raw: int) -> float | None:
        dead = self.__dict__.setdefault("_unvaluable", {})
        if time.monotonic() - dead.get(mint, -1e9) < 600:
            return None  # martwy / niewymienialny token – nie pytamy Jupitera co odświeżenie
        try:
            q = await asyncio.wait_for(self.jup.quote(mint, SOL_MINT, raw, 1000), 5)
            return int(q["outAmount"]) / LAMPORTS
        except Exception as e:  # noqa: BLE001
            if any(x in str(e) for x in ("not tradable", "No routes", "ROUTE", "TRADABLE")):
                dead[mint] = time.monotonic()
            return None

    async def values_sol(self, holdings: dict[str, int], max_quotes: int = 3) -> dict[str, float]:
        """Wartość pozycji w SOL – ceny paczkami (Jupiter price v3) zamiast wyceny każdego tokena osobno.
        Tokeny bez ceny (np. spam wrzucany na portfele) wyceniamy trasą wymiany tylko w kilku sztukach –
        inaczej portfel z setką śmieciowych tokenów zjadałby cały limit Jupitera."""
        dead = self.dead_mints()
        mints = [m for m in holdings if m != SOL_MINT and m not in dead]  # 💀 rug pull – cena z API to fikcja
        if not mints:
            return {}
        px: dict[str, float] = {}
        for i in range(0, len(mints), 49):
            try:
                px.update(await self.jup.prices(mints[i:i + 49] + [SOL_MINT]))
            except Exception:  # noqa: BLE001
                pass
        sol_usd = px.get(SOL_MINT)
        out = {}
        quotes = 0
        for m in mints:
            if sol_usd and m in px:
                dec = self._dec.get(m)
                if dec is None:
                    _, dec = self.cached_token(m) or await self.token_info(m)
                out[m] = holdings[m] / 10 ** dec * px[m] / sol_usd
            elif quotes < max_quotes:  # brak ceny w API – awaryjnie wycena przez trasę wymiany
                quotes += 1
                v = await self.value_in_sol(m, holdings[m])
                if v is not None:
                    out[m] = v
        return out

    async def equity_sol(self, tg_id: int, balances: dict[str, int] | None = None) -> float:
        bal = balances if balances is not None else self.paper(tg_id)
        vals = await self.values_sol(bal)
        return bal.get(SOL_MINT, 0) / LAMPORTS + sum(vals.values())

    def cost_basis(self, tg_id: int, mint: str, paper: bool) -> tuple[float, float] | None:
        """(koszt pozostałych tokenów w SOL, liczba tokenów) metodą średniej ceny – do PnL pozycji."""
        rows = self.db._q("""SELECT side, SUM(sol) sol, SUM(tokens) tok FROM sol_trades WHERE tg_id = ? AND mint = ?
                             AND status = 'filled' AND paper = ? GROUP BY side""", (tg_id, mint, int(paper)))
        d = {r["side"]: (r["sol"] or 0, r["tok"] or 0) for r in rows}
        spent, bought = d.get("buy", (0, 0))
        sold = d.get("sell", (0, 0))[1]
        left = bought - sold
        if bought <= 0 or left <= 0:
            return None
        return spent * left / bought, left

    # ---------- reakcja na wymianę tradera ----------
    async def on_swap(self, wallet: str, sw: Swap, t_recv: float) -> Counter:
        res = Counter()
        for lead in self.leaders_for(wallet):
            res += await self.copy_leader(lead, sw, t_recv)
        return res

    def cached_token(self, mint: str) -> tuple[str, int] | None:
        rows = self.db._q("SELECT * FROM sol_tokens WHERE mint = ?", (mint,))
        return (rows[0]["symbol"], rows[0]["decimals"]) if rows else None

    async def copy_leader(self, lead, sw: Swap, t_recv: float) -> Counter:
        # nazwa tokena tylko do powiadomień – pobieramy ją RÓWNOLEGLE, kopiowanie rusza od razu
        # (miejsca po przecinku znamy z samej transakcji tradera)
        info_task = asyncio.create_task(self.token_info(sw.mint, sw.decimals))
        if sw.side == "buy":
            self.note_buy(lead["id"], sw.mint)
        followers = []
        paused = self.db.get_state("paused") == "1"  # admin wstrzymał kopiowanie wszystkim
        for u in ([] if paused else self.db.active_followers(lead["id"])):
            if self.has_wallet(u["tg_id"]):
                fr = self.db.follow_row(u["tg_id"], lead["id"])
                followers.append(dict(u, follow_sol_buy=fr["sol_buy"] if fr else None,
                                      follow_sol_pct=fr["sol_pct"] if fr else None))

        async def run(u):
            async with self.sem:
                return await self._copy(u, lead, sw, info_task, t_recv)

        res = Counter(await asyncio.gather(*(run(u) for u in followers)))
        symbol, decimals = await info_task
        alert = (f"👁 <b>{html.escape(lead['name'])}</b> {'kupił' if sw.side == 'buy' else 'sprzedał'} "
                 f"<b>{html.escape(symbol)}</b> za {sw.quote_amount:.3g} {sw.quote_symbol}"
                 + (f" ({sw.fraction * 100:.0f}% swoich)" if sw.side == "sell" else ""))
        for u in self.db.watchers(lead["id"]):
            await self.say(u, "alert", alert, [[("📊 Token", f"tkn:{sw.mint}")]])
            res["alert"] += 1
        asyncio.create_task(self._reference(lead, sw))
        log.info("Solana %s: %s %s → %s", lead["name"], sw.side, symbol, dict(res))
        return res

    async def _copy(self, u, lead, sw: Swap, info_task, t_recv: float) -> str:
        live = self.is_live(u)
        err: Exception | None = None
        try:
            self._fresh(t_recv, sw.side)
            boost_note = None
            prequote = None
            if sw.side == "buy":
                keys = u.keys()
                need = u["sol_consensus"] if "sol_consensus" in keys else 0
                boost = u["sol_boost"] if "sol_boost" in keys and u["sol_boost"] else 1
                cons = self.consensus(u, sw.mint)
                if need >= 2 and cons < need:
                    raise Skip(f"⏳ czekam na potwierdzenie – kupił {cons} z {need} Twoich traderów (sygnał zbiorowy)")
                key = f"boost:{u['tg_id']}:{sw.mint}"
                already = self.db.get_state(key)
                holds = await self.token_raw(u, sw.mint) > 0
                if need >= 2 and already and holds:
                    raise Skip("już kupione po sygnale zbiorowym – kolejny trader tylko to potwierdza")
                boosted = cons >= 2 and boost > 1 and not already
                topup = boosted and holds  # dokładamy do pozycji kupionej za 1. traderem
                if self.turbo(u):
                    # turbo: najpierw tanie filtry z bazy (bez sieci) – odrzucony zakup nie zużywa limitu Jupitera;
                    # potem kwota i wycena RÓWNOLEGLE z resztą filtrów (sieć)
                    self.guard_db(u, sw, lead)
                    amount = await self.buy_amount(u, sw)
                    if not boosted:
                        prequote = asyncio.create_task(self.fast_quote(SOL_MINT, sw.mint, amount,
                                                                       int(u["sol_slippage_bps"])))
                        prequote.add_done_callback(lambda f: f.cancelled() or f.exception())
                    try:
                        await self.guard_buy(u, sw, lead, allow_add=topup)
                    except Exception:
                        if prequote:
                            prequote.cancel()
                        raise
                else:
                    await self.guard_buy(u, sw, lead, allow_add=topup)
                    amount = await self.buy_amount(u, sw)
                if boosted:
                    amount = int(amount * ((boost - 1) if topup else boost))
                    bal = await self.sol_lamports(u)
                    reserve = int(self.cfg.sol_reserve * LAMPORTS) if live else 0
                    amount = max(min(amount, bal - reserve - ATA_RENT), 0)
                    if amount < MIN_BUY:
                        raise Skip("za mało SOL na podwójną stawkę z sygnału zbiorowego")
                    boost_note = f"🔥 sygnał zbiorowy ({cons} traderów) ×{boost:g}"
                elif need >= 2:
                    boost_note = f"🔥 sygnał zbiorowy ({cons} traderów)"
                if boosted or need >= 2:
                    self.db.set_state(key, "1")
            else:
                # sprzedajemy tylko tokeny kupione za tym traderem – nie ruszamy kupionych za innym
                # traderem (ten sam token u dwóch traderów) ani kupionych ręcznie
                held = min(await self.token_raw(u, sw.mint),
                           self.attributed_raw(u["tg_id"], lead["id"], sw.mint, not live, sw.decimals))
                if held <= 0:
                    return "none"
                if sw.fraction > 0.97:
                    amount = held
                    self.__dict__.setdefault("_pend", {}).pop((u["tg_id"], lead["id"], sw.mint), None)
                else:
                    amount = await self._batched_sell(u, lead["id"], sw, held)
                    if not amount:
                        return "batched"  # drobny kawałek – zbieramy, sprzedamy razem z kolejnymi
            basis = self.cost_basis(u["tg_id"], sw.mint, not live) if sw.side == "sell" else None
            if sw.side == "sell":
                fill = await self.sell_with_retry(u, sw.mint, amount, t_recv, sw.decimals, sw)
            else:
                fill = await self.execute(u, sw.side, sw.mint, amount, t_recv, decimals=sw.decimals, fallback=sw,
                                          prequote=prequote)
            if boost_note:
                fill.note = boost_note + (f" · {fill.note}" if fill.note else "")
            if sw.side == "sell":
                self.sell_pnl(basis, fill)
                self.after_sell(u, sw.mint, await self.token_raw(u, sw.mint) <= 0 if live else False)
        except Exception as e:  # noqa: BLE001
            err = e
        symbol, decimals = await info_task
        sym = html.escape(symbol)
        name = html.escape(lead["name"])
        mode = "💰 prawdziwe SOL" if live else "🧪 ćwiczenia"
        trader = (f"👤 {name} {'kupił' if sw.side == 'buy' else 'sprzedał'} "
                  + (f"za {sw.quote_amount:.3g} {sw.quote_symbol}" if sw.side == "buy"
                     else f"{sw.fraction * 100:.0f}% swoich (za {sw.quote_amount:.3g} {sw.quote_symbol})"))
        token_btn = [("📊 Token", f"tkn:{sw.mint}")]
        try:
            if err is not None:
                raise err
        except Skip as e:
            log.info("Solana %s → %s: pominięto – %s", lead["name"], u["tg_id"], str(e)[:160])
            self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, 0, 0, "skipped", str(e), not live)
            await self.say(u, "skip", f"⚠️ <b>POMINIĘTO · {sym}</b>\n{trader}\nPowód: {html.escape(str(e))}",
                           [token_btn])
            return "skipped"
        except Exception as e:  # noqa: BLE001
            why = friendly_error(e, u)
            if is_frozen_error(str(e)):
                self.__dict__.setdefault("_frozen", {})[sw.mint] = time.time()
            log.warning("Solana %s → %s: nie weszło – %s (%s)", lead["name"], u["tg_id"], why, str(e)[:160])
            self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, 0, 0, "error", why[:300], not live)
            await self.say(u, "error", f"❌ <b>NIE WESZŁO · {sym}</b>\n{trader}\nPowód: {html.escape(why)}", [token_btn])
            return "error"
        self.record(u["tg_id"], lead["id"], sw.signature, sw.side, sw.mint, fill.sol, fill.tokens, "filled",
                    lead["name"] + (f" · {fill.note}" if fill.note else ""), not live, fill.sig, fill.latency_ms,
                    fill.pnl)
        usd = await self.usd_txt(fill.sol)
        speed = f"⚡ {fill.latency_ms / 1000:.2f} s" + (f" (+{fill.confirm_s:.1f} s sieć)" if fill.confirm_s else "")
        extra = f" · {fill.note}" if fill.note else ""
        links = [("🔍 Solscan", f"https://solscan.io/tx/{fill.sig}")] if fill.sig else []
        if sw.side == "buy":
            text = (f"🟢 <b>KUPNO · {sym}</b>\n{trader}\n"
                    f"💵 Ty: <b>{fill.sol:.4f} SOL</b>{usd} → {fmt_tokens(fill.tokens)} {sym}\n{speed} · {mode}{extra}")
            buttons = [token_btn + [("🔴 Sprzedaj", f"tsn:100:{sw.mint}")]] + ([links] if links else [])
        else:
            text = (f"🔴 <b>SPRZEDAŻ · {sym}</b>\n{trader}\n"
                    f"💵 Ty: dostałeś <b>{fill.sol:.4f} SOL</b>{usd}{self.pnl_line(fill)}\n{speed} · {mode}{extra}")
            buttons = [token_btn + links]
        await self.say(u, sw.side, text, buttons)
        return "filled"

    async def _batched_sell(self, u, lid: int, sw: Swap, held: int) -> int:
        """Trader wychodzi małymi kawałkami (np. 30 sprzedaży po 3%) – każda kopia to osobna opłata sieci i napiwek.
        Kawałki warte mniej niż ~15× opłata (0,002–0,02 SOL) zbieramy i sprzedajemy razem (gdy uzbiera się sensowna kwota albo
        połowa pozycji). Zwraca ile sprzedać teraz (0 = jeszcze czekamy)."""
        pend = self.__dict__.setdefault("_pend", {})
        key = (u["tg_id"], lid, sw.mint)
        frac = 1 - (1 - pend.get(key, 0.0)) * (1 - sw.fraction)  # łączny ułamek pozycji do sprzedania
        per = await self._trader_price(sw)
        fee = BASE_FEE + (self.priority(u) if self.turbo(u) else self.priority(u) // 2)  # realny koszt jednej transakcji
        value = held * frac * per if per else None
        if frac < 0.5 and value is not None and value < min(max(15 * fee, 2_000_000), 20_000_000):
            pend[key] = frac
            return 0
        pend.pop(key, None)
        return max(int(held * frac), 1)

    def _fresh(self, t_recv: float | None, side: str = "buy"):
        """Spóźniony ZAKUP (> SOL_MAX_DELAY_S) nie ma sensu – cena memecoina mogła już uciec.
        Spóźnioną SPRZEDAŻ robimy zawsze: trader wyszedł, więc trzymanie tokena to czyste ryzyko."""
        if t_recv is None or side == "sell":
            return
        late = time.monotonic() - t_recv
        if late > self.cfg.sol_max_delay_s:
            raise Skip(f"za późno – {late:.0f} s od ruchu tradera (limit {self.cfg.sol_max_delay_s:g} s), "
                       "cena mogła uciec")

    def day_pnl(self, tg_id: int, paper: bool) -> float:
        """Zarobek/strata (SOL) na tokenach sprzedanych dzisiaj (od północy czasu użytkownika)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from .session import closed_trades
        midnight = datetime.now(ZoneInfo(self.cfg.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        since = int(midnight.timestamp() * 1000)
        rows = self.db._q("""SELECT t.*, NULL symbol FROM sol_trades t WHERE tg_id = ? AND status = 'filled'
                             AND paper = ? ORDER BY id""", (tg_id, int(paper)))
        return sum(c["pnl"] for c in closed_trades(rows) if c["ts"] >= since)

    def open_positions(self, tg_id: int, paper: bool) -> int:
        """Ile tokenów kupionych przez bota (kopie i ręczne) wciąż trzymasz – spam z portfela się nie liczy."""
        r = self.db._q("""SELECT COUNT(*) n FROM (SELECT mint, SUM(CASE WHEN side = 'buy' THEN tokens ELSE -tokens END) t
                          FROM sol_trades WHERE tg_id = ? AND paper = ? AND status = 'filled' GROUP BY mint)
                          WHERE t > 1e-9""", (tg_id, int(paper)))
        return r[0]["n"] if r else 0

    async def open_positions_now(self, u) -> int:
        """Otwarte pozycje NAPRAWDĘ trzymane teraz: kupione przez bota I wciąż w portfelu. Tokeny sprzedane
        ręcznie (np. w Axiom), resztki po sprzedaży i martwe tokeny bez trasy wymiany nie zajmują miejsca
        z limitu – inaczej bot przestawał kupować, choć pozycji już nie było."""
        paper = not self.is_live(u)
        ledger = {r["mint"] for r in self.db._q(
            """SELECT mint FROM (SELECT mint, SUM(CASE WHEN side = 'buy' THEN tokens ELSE -tokens END) t
               FROM sol_trades WHERE tg_id = ? AND paper = ? AND status = 'filled' GROUP BY mint) WHERE t > 1e-9""",
            (u["tg_id"], int(paper)))}
        if not ledger:
            return 0
        cache = self.__dict__.setdefault("_pos_cache", {})
        hit = cache.get(u["tg_id"])
        if hit and time.monotonic() - hit[0] < 5:
            hold = hit[1]
        else:
            try:
                hold = await asyncio.wait_for(self.holdings(u), 4)
            except Exception:  # noqa: BLE001
                return self.open_positions(u["tg_id"], paper)  # RPC nie odpowiada – liczymy po staremu
            cache[u["tg_id"]] = (time.monotonic(), hold)
        dead = self.__dict__.get("_unvaluable", {})
        now = time.monotonic()
        # 🧊 zamrożonych tokenów nie da się sprzedać – nie mogą na zawsze blokować miejsc z limitu
        frozen = {m for m, t in self.__dict__.get("_frozen", {}).items() if time.time() - t < 24 * 3600}
        frozen |= {r["mint"] for r in self.db._q(
            """SELECT DISTINCT mint FROM sol_trades WHERE tg_id = ? AND status = 'error' AND info LIKE '%zamrożon%'
               AND ts >= ?""", (u["tg_id"], now_ms() - 7 * DAY_MS))}
        frozen |= self.dead_mints()  # 💀 rug pull – też nie do sprzedania
        return sum(1 for m in ledger if hold.get(m, 0) > 0 and now - dead.get(m, -1e9) >= 600 and m not in frozen)

    def note_buy(self, lid: int, mint: str):
        now = time.time()
        self._recent_buys.setdefault(mint, {})[lid] = now
        if len(self._recent_buys) > 2000:  # sprzątanie starych wpisów
            for m in [m for m, d in self._recent_buys.items() if now - max(d.values()) > 3600]:
                self._recent_buys.pop(m, None)

    def consensus(self, u, mint: str) -> int:
        """Ilu RÓŻNYCH kopiowanych przez Ciebie traderów kupiło ten token w oknie czasu (sygnał zbiorowy)."""
        keys = u.keys()
        window = (u["sol_cons_window"] if "sol_cons_window" in keys and u["sol_cons_window"] else 10) * 60
        mine = {r["leader_id"] for r in self.db._q("SELECT leader_id FROM follows WHERE tg_id = ? AND copy = 1",
                                                   (u["tg_id"],))}
        now = time.time()
        return sum(1 for lid, ts in self._recent_buys.get(mint, {}).items() if lid in mine and now - ts <= window)

    def attributed_raw(self, tg_id: int, lid: int, mint: str, paper: bool, decimals: int) -> int:
        """Ile tokenów (jednostki bazowe) kupiono za tym traderem i jeszcze nie sprzedano."""
        r = self.db._q("""SELECT SUM(CASE WHEN side = 'buy' THEN tokens ELSE -tokens END) t FROM sol_trades
                          WHERE tg_id = ? AND leader_id = ? AND mint = ? AND paper = ? AND status = 'filled'""",
                       (tg_id, lid, mint, int(paper)))[0]["t"] or 0
        return max(int(round(r * 10 ** decimals)), 0)

    def hold_stats(self, lid: int) -> dict:
        """Jak długo trader trzyma tokeny (z portfela wzorcowego): mediana w sekundach i liczba zamkniętych pozycji."""
        hit = self._hold_cache.get(lid)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        rows = self.db._q("SELECT side, mint, tokens, ts FROM sol_trades WHERE tg_id = ? AND status = 'filled' "
                          "ORDER BY id", (ref_id(lid),))
        qty: dict[str, float] = {}
        start: dict[str, int] = {}
        holds = []
        for r in rows:
            q = qty.get(r["mint"], 0.0)
            if r["side"] == "buy":
                if q <= 0:
                    start[r["mint"]] = r["ts"]
                qty[r["mint"]] = q + (r["tokens"] or 0)
            elif q > 0:
                q -= r["tokens"] or 0
                qty[r["mint"]] = q
                if q <= 1e-9 * max(r["tokens"] or 1, 1) or q <= 0:
                    holds.append((r["ts"] - start.pop(r["mint"], r["ts"])) / 1000)
                    qty[r["mint"]] = 0.0
        holds.sort()
        out = {"median_s": holds[len(holds) // 2] if holds else None, "n": len(holds),
               "fast_share": sum(h < 10 for h in holds) / len(holds) if holds else None}
        self._hold_cache[lid] = (time.monotonic(), out)
        return out

    def guard_db(self, u, sw: Swap, lead=None):
        """Tanie bezpieczniki – tylko baza i pamięć, bez zapytań do sieci (te same co w guard_buy). Turbo
        sprawdza je PRZED wyceną, żeby odrzucone zakupy nie zjadały limitu zapytań Jupitera."""
        keys = u.keys()
        if lead is not None and "sol_min_hold" in keys and u["sol_min_hold"] > 0:
            hs = self.hold_stats(lead["id"])
            if hs["n"] >= 5 and hs["median_s"] is not None and hs["median_s"] < u["sol_min_hold"]:
                raise Skip(f"⚡ flipper – trzyma tokeny zwykle ok. {hs['median_s']:.0f} s (Twój próg "
                           f"{u['sol_min_hold']:g} s), kopiowanie nieopłacalne")
        adds = u["sol_max_adds"] if "sol_max_adds" in keys else 0
        if adds and adds > 0:
            n = self.db._q("""SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND mint = ? AND side = 'buy'
                              AND status = 'filled' AND paper = ? AND ts >= ?""",
                           (u["tg_id"], sw.mint, int(not self.is_live(u)), now_ms() - 24 * 3600 * 1000))[0]["n"]
            if n >= adds:
                raise Skip(f"🛡 ten token kupiłeś już {n}× w 24 h (Twój limit {adds}) – trader dokupuje, "
                           "a Ty nie pakujesz w jeden token coraz więcej")
        limit = u["sol_day_limit"] if "sol_day_limit" in keys else 0
        if limit and limit > 0:
            pnl = self.day_pnl(u["tg_id"], not self.is_live(u))
            if pnl <= -limit:
                raise Skip(f"🛑 dzienny limit straty – dziś już −{-pnl:.3f} SOL (limit {limit:g} SOL), "
                           "bot wraca do kupowania jutro")
        streak = u["sol_streak"] if "sol_streak" in keys else 0
        if streak and streak > 0:
            last = self.db._q("""SELECT pnl, ts FROM sol_trades WHERE tg_id = ? AND paper = ? AND side = 'sell'
                                 AND status = 'filled' AND pnl IS NOT NULL ORDER BY id DESC LIMIT ?""",
                              (u["tg_id"], int(not self.is_live(u)), int(streak)))
            if len(last) >= streak and all(r["pnl"] < 0 for r in last) and now_ms() - last[0]["ts"] < 30 * 60_000:
                left = 30 - (now_ms() - last[0]["ts"]) // 60_000
                raise Skip(f"☕ przerwa po {streak} stratach z rzędu – bot wraca do kupowania za ok. {left} min")
        frozen = self.__dict__.setdefault("_frozen", {})
        if time.time() - frozen.get(sw.mint, 0) < 24 * 3600:
            raise Skip("🧊 token zamrożony (twórca blokuje konta) – sieć odrzuca każdy zakup, nie próbuję")
        bad = self.db._q("""SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND mint = ? AND status = 'error'
                            AND ts >= ?""", (u["tg_id"], sw.mint, now_ms() - 30 * 60_000))[0]["n"]
        if bad >= 2:
            raise Skip(f"sieć odrzuciła ten token już {bad}× w 30 min – nie próbuję dalej (każda próba to opłata)")

    async def guard_buy(self, u, sw: Swap, lead=None, allow_add: bool = False):
        """Bezpieczniki przed skopiowaniem zakupu: limit straty, filtry tradera, „nie dokupuj”, 🛡 ryzyko."""
        keys = u.keys()
        if lead is not None and "sol_min_hold" in keys and u["sol_min_hold"] > 0:
            hs = self.hold_stats(lead["id"])
            if hs["n"] >= 5 and hs["median_s"] is not None and hs["median_s"] < u["sol_min_hold"]:
                raise Skip(f"⚡ flipper – trzyma tokeny zwykle ok. {hs['median_s']:.0f} s (Twój próg "
                           f"{u['sol_min_hold']:g} s), kopiowanie nieopłacalne")
        if "sol_min_lead" in keys and u["sol_min_lead"] > 0:
            his = await self.trader_sol(sw)
            if his is not None and his < u["sol_min_lead"]:
                raise Skip(f"mały zakup tradera ({his:.3g} SOL, Twój próg {u['sol_min_lead']:g} SOL)")
        if not allow_add and "sol_no_add" in keys and u["sol_no_add"] and await self.token_raw(u, sw.mint) > 0:
            raise Skip("masz już ten token – nie dokupuję (opcja „Nie dokupuj”)")
        adds = u["sol_max_adds"] if "sol_max_adds" in keys else 0
        if adds and adds > 0:
            n = self.db._q("""SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND mint = ? AND side = 'buy'
                              AND status = 'filled' AND paper = ? AND ts >= ?""",
                           (u["tg_id"], sw.mint, int(not self.is_live(u)), now_ms() - 24 * 3600 * 1000))[0]["n"]
            if n >= adds:
                raise Skip(f"🛡 ten token kupiłeś już {n}× w 24 h (Twój limit {adds}) – trader dokupuje, "
                           "a Ty nie pakujesz w jeden token coraz więcej")
        limit = u["sol_day_limit"] if "sol_day_limit" in keys else 0
        if limit and limit > 0:
            pnl = self.day_pnl(u["tg_id"], not self.is_live(u))
            if pnl <= -limit:
                raise Skip(f"🛑 dzienny limit straty – dziś już −{-pnl:.3f} SOL (limit {limit:g} SOL), "
                           "bot wraca do kupowania jutro")
        paper = not self.is_live(u)
        max_pos = u["sol_max_pos"] if "sol_max_pos" in keys else 0
        if max_pos and max_pos > 0 and not allow_add:
            n = await self.open_positions_now(u)
            if n >= max_pos and await self.token_raw(u, sw.mint) <= 0:
                raise Skip(f"masz już {n} otwartych pozycji (Twój limit {max_pos}) – czekam, aż coś się sprzeda")
        streak = u["sol_streak"] if "sol_streak" in keys else 0
        if streak and streak > 0:
            last = self.db._q("""SELECT pnl, ts FROM sol_trades WHERE tg_id = ? AND paper = ? AND side = 'sell'
                                 AND status = 'filled' AND pnl IS NOT NULL ORDER BY id DESC LIMIT ?""",
                              (u["tg_id"], int(paper), int(streak)))
            if len(last) >= streak and all(r["pnl"] < 0 for r in last) and now_ms() - last[0]["ts"] < 30 * 60_000:
                left = 30 - (now_ms() - last[0]["ts"]) // 60_000
                raise Skip(f"☕ przerwa po {streak} stratach z rzędu – bot wraca do kupowania za ok. {left} min")
        frozen = self.__dict__.setdefault("_frozen", {})
        if time.time() - frozen.get(sw.mint, 0) < 24 * 3600:
            raise Skip("🧊 token zamrożony (twórca blokuje konta) – sieć odrzuca każdy zakup, nie próbuję")
        bad = self.db._q("""SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND mint = ? AND status = 'error'
                            AND ts >= ?""", (u["tg_id"], sw.mint, now_ms() - 30 * 60_000))[0]["n"]
        if bad >= 2:
            raise Skip(f"sieć odrzuciła ten token już {bad}× w 30 min – nie próbuję dalej (każda próba to opłata)")
        g = lambda k: u[k] if k in keys and u[k] else 0  # noqa: E731
        live_guard = self.is_live(u)  # na prawdziwych SOL token z prawem zamrażania odrzucamy zawsze
        if g("sol_safe") or g("sol_max_mcap") or g("sol_min_age") or g("sol_max_age") or g("sol_dev_exit") or live_guard:
            try:  # jedno zapytanie (z pamięci 15 s) dla wszystkich filtrów tokena
                info = await asyncio.wait_for(self.jup.token_full(sw.mint), 3)
            except Exception:  # noqa: BLE001
                info = None
            if g("sol_safe"):
                from .risk import trap_reason
                why = trap_reason(info)
                if why:
                    raise Skip(f"🛡 pułapka – {why}")
            elif live_guard and ((info or {}).get("audit") or {}).get("freezeAuthorityDisabled") is False:
                # nawet z wyłączonym filtrem pułapek: twórca może zamrozić Twoje tokeny – na prawdziwych SOL to
                # pewna strata całej kwoty (kupisz, a sprzedać już nie dasz rady)
                raise Skip("🧊 twórca może zamrozić Twoje tokeny – na prawdziwych SOL takich nie kupuję "
                           "(nawet przy wyłączonym filtrze pułapek)")
            if info:
                mc = info.get("mcap")
                if g("sol_max_mcap") and mc and mc > g("sol_max_mcap"):
                    raise Skip(f"token wart już ${mc / 1e3:,.0f}K (Twój limit ${g('sol_max_mcap') / 1e3:,.0f}K) – "
                               "mało miejsca na duży wzrost".replace(",", " "))
                age = token_age_min(info)
                if age is not None and g("sol_min_age") and age < g("sol_min_age"):
                    raise Skip(f"token ma dopiero {age:.0f} min (Twoje minimum {g('sol_min_age'):g} min) – "
                               "pierwsze minuty to najwięcej pułapek")
                if age is not None and g("sol_max_age") and age > g("sol_max_age") * 60:
                    raise Skip(f"token ma już {age / 60:.0f} h (Twój limit {g('sol_max_age'):g} h)")
                dev = (info.get("audit") or {}).get("devBalancePercentage")
                if g("sol_dev_exit") and dev is not None:  # zapamiętujemy, ile trzymał twórca przy zakupie
                    self.db.set_state(f"devpct:{u['tg_id']}:{sw.mint}", f"{dev:.4f}")

    async def usd_txt(self, sol: float) -> str:
        """„ (≈ $12.30)” – kurs SOL z pamięci podręcznej (10 s), bez czekania dłużej niż 2 s."""
        try:
            px = (await asyncio.wait_for(self.jup.prices([SOL_MINT]), 2)).get(SOL_MINT)
        except Exception:  # noqa: BLE001
            return ""
        return f" (≈ ${sol * px:,.2f})".replace(",", " ") if px else ""

    async def trader_sol(self, sw: Swap) -> float | None:
        """Za ile SOL kupił trader (przy zakupie za USDC/USDT – przeliczone po kursie SOL)."""
        if sw.quote_symbol == "SOL":
            return sw.quote_amount
        try:
            sol_usd = (await self.jup.prices([SOL_MINT])).get(SOL_MINT)
        except Exception:  # noqa: BLE001
            sol_usd = None
        return sw.quote_amount / sol_usd if sol_usd else None

    async def buy_amount(self, u, sw: Swap) -> int:
        """Ile lamportów wydać na kopię zakupu. Tryby:
        fixed – stała kwota SOL; pct – X% kwoty tradera (on 10 SOL, Ty przy 5% → 0,5 SOL);
        ratio – ten sam % portfela co trader (× mnożnik). Ustawienie portfela (follows) ma pierwszeństwo."""
        bal = await self.sol_lamports(u)
        reserve = int(self.cfg.sol_reserve * LAMPORTS) if self.is_live(u) else 0
        keys = u.keys()
        f_buy = u["follow_sol_buy"] if "follow_sol_buy" in keys else None
        f_pct = u["follow_sol_pct"] if "follow_sol_pct" in keys else None
        mode = "fixed" if f_buy else "pct" if f_pct else u["sol_buy_mode"]
        if mode == "pct":
            share = f_pct or u["sol_pct"]
            his = await self.trader_sol(sw)
            if his is None:
                raise Skip("nie znam kursu SOL – nie da się policzyć % kwoty tradera")
            want = int(his * share / 100 * LAMPORTS)
            why = (f"trader kupił za {his:.4g} SOL – {share:g}% z tego to {want / LAMPORTS:.5f} SOL, "
                   "poniżej minimum 0,001 SOL (zwiększ % albo ustaw stałą kwotę)")
        elif mode == "ratio":
            want = int(bal * sw.fraction * u["sol_mult"])
            why = (f"trader użył {sw.fraction * 100:.2g}% swoich SOL – u Ciebie to {want / LAMPORTS:.5f} SOL, "
                   "poniżej minimum 0,001 SOL (ustaw stałą kwotę w ⚙️ Ustawieniach Solany)")
        else:
            want, why = int((f_buy or u["sol_buy"]) * LAMPORTS), None
        if u["sol_max"] > 0:
            want = min(want, int(u["sol_max"] * LAMPORTS))
        floor0 = (u["sol_min_buy"] if "sol_min_buy" in keys else 0) or 0
        if why and want < MIN_BUY and not (floor0 and want >= floor0 * LAMPORTS * 0.3):
            raise Skip(why)
        want = min(want, bal - reserve)
        rpct = u["sol_reserve_pct"] if "sol_reserve_pct" in keys else 0
        if rpct and rpct > 0:
            # 🛡 rezerwa: tyle % kapitału (wolny SOL + koszt otwartych pozycji) zawsze zostaje wolne
            paper = not self.is_live(u)
            locked = self.db._q("""SELECT COALESCE(SUM(c), 0) c FROM (SELECT SUM(CASE WHEN side = 'buy' THEN sol ELSE 0 END)
                                   * MAX(1 - SUM(CASE WHEN side = 'sell' THEN tokens ELSE 0 END)
                                   / NULLIF(SUM(CASE WHEN side = 'buy' THEN tokens ELSE 0 END), 0), 0) c
                                   FROM sol_trades WHERE tg_id = ? AND paper = ? AND status = 'filled' GROUP BY mint)""",
                                (u["tg_id"], int(paper)))[0]["c"] or 0
            keep = int((bal / LAMPORTS + locked) * rpct / 100 * LAMPORTS)
            want = min(want, bal - keep)
            if want < MIN_BUY:
                raise Skip(f"🛡 rezerwa {rpct:g}% kapitału zostaje wolna – na ten zakup już nie starcza")
        if bal - reserve < MIN_BUY:
            raise Skip(f"za mało SOL na koncie (masz {bal / LAMPORTS:.4f} SOL)")
        floor = u["sol_min_buy"] if "sol_min_buy" in u.keys() else 0
        if u["tg_id"] > 0:
            # groszowe pozycje to pewna strata – depozyt konta tokena (0,002 SOL) i opłaty tam i z powrotem
            # zjadały 15–30% kwoty (dane z łańcucha: mediana kupna 0,0065 SOL). Ćwiczenia liczą tak samo.
            floor = max(floor or 0, LIVE_MIN_BUY / LAMPORTS)
        if floor and want < floor * LAMPORTS:
            # tylko trochę poniżej minimum (zwykłe wejście tradera) → kupujemy za minimum, jeśli starcza SOL;
            # dużo poniżej (jego zakup testowy za grosze) → pomijamy, bo opłaty zjadłyby zysk
            if why and want >= floor * LAMPORTS * 0.3 and floor * LAMPORTS <= bal - reserve:
                return int(floor * LAMPORTS)
            raise Skip(f"za mała kwota ({want / LAMPORTS:.4f} SOL, minimum {floor:g} SOL) – opłaty zjadłyby zysk")
        if want < MIN_BUY:
            raise Skip(f"za mała kwota ({want / LAMPORTS:.4f} SOL) – opłaty zjadłyby zysk")
        return want

    def _jup_busy(self) -> bool:
        busy = getattr(self.jup, "busy", None)
        return bool(busy and busy())

    async def fast_quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int) -> dict:
        """Wycena bez stania w kolejce: gdy okno zapytań Jupitera jest pełne (transakcja czekałaby do ~10 s),
        od razu pytamy Raptora; gdy Raptor nie ma trasy – i tak Jupiter."""
        if self.raptor is not None and self._jup_busy():
            try:
                rq = await self.raptor.quote(in_mint, out_mint, amount, slippage_bps)
                return {"outAmount": rq["amountOut"], "_raptor": rq}
            except ChainError:
                pass
        return await self.jup.quote(in_mint, out_mint, amount, slippage_bps)

    async def execute(self, u, side: str, mint: str, amount_raw: int, t_recv: float | None = None,
                      decimals: int | None = None, fallback: Swap | None = None, prequote=None) -> Fill:
        """Kupno: amount_raw = lamporty SOL. Sprzedaż: jednostki tokena."""
        t0 = t_recv or time.monotonic()
        if u["tg_id"] > 0:  # portfel (i saldo demo) musi istnieć PRZED transakcją – inaczej utworzenie go
            self.wallet(u["tg_id"])  # później wyzerowałoby kupione tokeny
        in_mint, out_mint = (SOL_MINT, mint) if side == "buy" else (mint, SOL_MINT)
        if decimals is None:  # bez zapytania do sieci, jeśli znamy token
            cached = self.cached_token(mint)
            decimals = cached[1] if cached else (await self.token_info(mint))[1]
        t_q = time.monotonic()
        try:
            quote = await (prequote if prequote is not None else
                           self.fast_quote(in_mint, out_mint, amount_raw, int(u["sol_slippage_bps"])))
        except ChainError as e:
            limited = "Too many requests" in str(e) or "429" in str(e)
            quote = None
            if not limited:
                try:  # druga próba: szersze trasy (przez tokeny pośrednie)
                    quote = await self.jup.quote(in_mint, out_mint, amount_raw, int(u["sol_slippage_bps"]), restrict=False)
                except ChainError:
                    quote = None
            if quote is None and self.raptor is not None:
                # druga droga: Raptor (71 giełd) – i na prawdziwych, i na ćwiczeniach (ta sama cena, te same zasady)
                try:
                    rq = await self.raptor.quote(in_mint, out_mint, amount_raw, int(u["sol_slippage_bps"]))
                    quote = {"outAmount": rq["amountOut"], "_raptor": rq}
                except ChainError:
                    quote = None
            if quote is None and limited:
                raise Skip("limit zapytań Jupitera – za dużo transakcji naraz (wyższy plan Jupitera to zniesie)") from e
            if quote is None:
                # ćwiczenia jak prawdziwe: zakup bez trasy tylko dla tokenów z platform startowych (na prawdziwych
                # SOL kupimy je przez PumpPortal); np. tokenizowanych akcji bez trasy nie „kupujemy” na niby.
                # Sprzedaż – zawsze (pozycja już jest, trader wyszedł), także gdy on sprzedał za USDC.
                if not self.is_live(u) and fallback and fallback.token_raw > 0 and (side == "sell" or is_launchpad(mint)):
                    per = await self._trader_price(fallback)
                    if per:
                        return self._paper_at_trader_price(u, side, mint, amount_raw, fallback, t0, decimals, per)
                if self.is_live(u) and self.pump is not None:  # świeży token z Pump.fun – zapasowa droga
                    return await self._live_pumpportal(u, side, mint, amount_raw, t0, t_recv, decimals)
                raise Skip(f"brak trasy wymiany – Jupiter jeszcze nie obsługuje tego tokena ({e})") from e
        out = int(quote["outAmount"])
        if not self.is_live(u) and u["tg_id"] > 0:
            # ćwiczenia: poślizg taki, jak wychodzi na prawdziwych transakcjach (cena rusza się, zanim wejdziemy)
            out = int(out * (1 - self.paper_slip(side)))
        chase = u["sol_chase"] if "sol_chase" in u.keys() else 0
        if side == "buy" and chase and fallback is not None and out > 0 and u["tg_id"] > 0:
            his = await self._trader_price(fallback)  # lamporty za jednostkę tokena u tradera
            mine = amount_raw / out
            if his and mine > his * (1 + chase / 100):
                raise Skip(f"🏃 cena uciekła: płaciłbyś o {(mine / his - 1) * 100:.0f}% drożej niż trader "
                           f"(Twój limit {chase:g}%) – nie gonię ceny")
        sol_raw, tok_raw = (amount_raw, out) if side == "buy" else (out, amount_raw)
        if not self.is_live(u):
            lat = int((time.monotonic() - t0) * 1000)
            if u["tg_id"] > 0:
                amount_raw, out = self.paper_trade(u, side, mint, amount_raw, out)
            elif side == "buy":
                self.paper_apply(u["tg_id"], {SOL_MINT: -(amount_raw + PAPER_TX_FEE), mint: out})
            else:
                amount_raw, out = clamp_sell(amount_raw, out, self.paper(u["tg_id"]).get(mint, 0))
                self.paper_apply(u["tg_id"], {mint: -amount_raw, SOL_MINT: out - PAPER_TX_FEE})
            sol_raw, tok_raw = (amount_raw, out) if side == "buy" else (out, amount_raw)
            return Fill(sol_raw / LAMPORTS, tok_raw / 10 ** decimals, None, lat)
        pub, kp = self.wallet(u["tg_id"])
        self._fresh(t_recv, side)  # wycena mogła czekać w kolejce limitu – sprawdzamy jeszcze raz przed wysłaniem
        turbo = self.turbo(u) and self.jito is not None
        t_s = time.monotonic()
        tip = self.trade_tip(u, sol_raw)  # kwota transakcji w SOL (kupno: wydane, sprzedaż: spodziewany wpływ)
        if "_raptor" not in quote and self.raptor is not None and self._jup_busy():
            # budowa transakcji u Jupitera stałaby w kolejce limitu – budujemy przez Raptora (świeża wycena)
            try:
                rq = await self.raptor.quote(in_mint, out_mint, amount_raw, int(u["sol_slippage_bps"]))
                quote = {"outAmount": rq["amountOut"], "_raptor": rq}
            except ChainError:
                pass
        if "_raptor" in quote:
            tx = await self.raptor.swap(quote["_raptor"], kp, tip, jito=turbo)
        else:
            tx = await self.jup.swap(quote, kp, tip, jito_tip=tip if turbo else None)
        t_b = time.monotonic()
        lat = int((t_b - t0) * 1000)  # do momentu wysłania transakcji
        self._bal.pop(pub, None)
        sig, confirm_s = await self.rpc.send_and_confirm(tx, also=[self.jito.send] if turbo else None)
        self.note_timing(side, turbo, t0, t_q, t_s, t_b, confirm_s)
        fill = Fill(sol_raw / LAMPORTS, tok_raw / 10 ** decimals, sig, lat, confirm_s,
                    note=("przez Raptor" if "_raptor" in quote else None) or ("⚡ turbo" if turbo else None))
        return await self._actual(fill, pub, mint, side, decimals, sol_raw, tok_raw)

    async def _actual(self, fill: Fill, pub: str, mint: str, side: str, decimals: int, q_sol: int, q_tok: int) -> Fill:
        """Prawdziwe SOL: wynik z TEGO, co naprawdę weszło (saldo przed/po w transakcji), a nie z wyceny sprzed
        wysłania. Kupno = ile SOL wyszło (z opłatą i napiwkiem, bez zwrotnego depozytu konta), sprzedaż = ile
        SOL wróciło po opłatach. Poślizg względem wyceny trafia do statystyk – ćwiczenia liczą z nim tak samo."""
        try:
            ch = await self.rpc.balance_change(fill.sig, pub, mint)
        except Exception:  # noqa: BLE001
            ch = None
        if not ch:
            return fill
        sol_d, tok_d, new_acc = ch
        if side == "buy":
            spent = -sol_d - (ATA_RENT if new_acc else 0)
            got = max(tok_d, 0)
            if spent > 0 and got > 0:
                self.note_slip("buy", got / q_tok - 1 if q_tok else 0)
                fill.sol, fill.tokens = spent / LAMPORTS, got / 10 ** decimals
        else:
            back = max(sol_d, 0)
            sold = max(-tok_d, 0)
            if sold > 0:
                self.note_slip("sell", back / q_sol - 1 if q_sol else 0)
                fill.sol, fill.tokens = back / LAMPORTS, sold / 10 ** decimals
        return fill

    def note_slip(self, side: str, pct: float):
        """Ile gorzej (ułamek) wyszła prawdziwa transakcja niż wycena – ostatnie 60 na stronę."""
        s = self.__dict__.setdefault("slips", {"buy": [], "sell": []})
        s[side] = (s[side] + [max(min(pct, 0.5), -0.9)])[-60:]

    def paper_slip(self, side: str) -> float:
        """Poślizg do ćwiczeń: mediana z prawdziwych transakcji (min. 1%, maks. 15%), bez danych – 2%."""
        vals = sorted(self.__dict__.get("slips", {}).get(side, []))
        if len(vals) < 5:
            return 0.02
        med = vals[len(vals) // 2]
        return min(max(-med, 0.01), 0.15)

    async def reclaim_rent(self, u, mint: str | None = None) -> int:
        """Zamyka puste konta tokenów w prawdziwym portfelu – depozyt (~0,002 SOL za konto) wraca."""
        if not self.is_live(u):
            return 0
        pub, kp = self.wallet(u["tg_id"])
        accounts = await self.rpc.empty_token_accounts(pub, mint)
        n = await self.rpc.close_token_accounts(kp, accounts) if accounts else 0
        if n:
            self._bal.pop(pub, None)
            log.info("Solana %s: zamknięto %d pustych kont tokenów (+%.4f SOL)", u["tg_id"], n, n * ATA_RENT / LAMPORTS)
        return n

    async def clean_dead(self, u) -> tuple[int, float, list[str]]:
        """💀 Martwe tokeny (brak trasy sprzedaży): prawdziwe SOL – spala i zamyka konta (wraca ~0,002 SOL depozytu
        z każdego), ćwiczenia – usuwa z portfela. Strata zapisuje się w historii. Zwraca (ile, SOL, symbole)."""
        hold = await self.holdings(u)
        dead = [m for m in hold if hold[m] > 0 and self.is_dead(m)]
        if not dead:
            return 0, 0.0, []
        names = [(self.cached_token(m) or (m[:4], 0))[0] for m in dead]
        paper = not self.is_live(u)
        if paper:
            self.paper_apply(u["tg_id"], {m: -hold[m] for m in dead})
            done, back = dead, 0.0
        else:
            pub, kp = self.wallet(u["tg_id"])
            accs = [a for a in await self.rpc.token_accounts_full(pub) if a["mint"] in dead and not a["frozen"]]
            n = await self.rpc.burn_and_close(kp, accs) if accs else 0
            done = [a["mint"] for a in accs[:n]]
            back = n * ATA_RENT / LAMPORTS
            self._bal.pop(pub, None)
        for m in done:
            basis = self.cost_basis(u["tg_id"], m, paper)
            dec = self._dec.get(m) or (self.cached_token(m) or ("", 0))[1]
            self.record(u["tg_id"], None, None, "sell", m, 0.0, hold[m] / 10 ** dec, "filled",
                        "💀 martwy token (rug pull) usunięty" + ("" if paper else " – odzyskany depozyt konta"), paper,
                        pnl=-(basis[0] if basis else 0))
        return len(done), back, names

    async def clean_dust(self, u) -> tuple[int, float]:
        """Prawdziwe SOL: resztki tokenów warte < 0,0005 SOL (i puste konta) – spala i zamyka konta, każde oddaje
        ~0,002 SOL depozytu. Sprzedaż takiej resztki kosztowałaby więcej, niż jest warta. Nie rusza tokenów
        kupionych w ostatnich 2 h ani zamrożonych. Zwraca (ile kont, ile SOL wróciło)."""
        if not self.is_live(u):
            return 0, 0.0
        pub, kp = self.wallet(u["tg_id"])
        accs = await self.rpc.token_accounts_full(pub)
        accs = [a for a in accs if a["mint"] != SOL_MINT and not a["frozen"]]
        if not accs:
            return 0, 0.0
        recent = {r["mint"] for r in self.db._q("SELECT DISTINCT mint FROM sol_trades WHERE tg_id = ? AND side = 'buy' "
                                                "AND ts >= ?", (u["tg_id"], now_ms() - 2 * 3600 * 1000))}
        need = [a for a in accs if a["raw"] > 0 and a["mint"] not in recent]
        px = {}
        if need:
            try:
                px = await asyncio.wait_for(self.jup.prices([a["mint"] for a in need][:49] + [SOL_MINT]), 8)
            except Exception:  # noqa: BLE001
                px = {}
        sol_usd = px.get(SOL_MINT)
        dust = []
        for a in accs:
            if a["raw"] == 0:
                dust.append(a)  # puste konto
            elif a["mint"] not in recent and sol_usd and a["mint"] in px:
                value = a["raw"] / 10 ** a["decimals"] * px[a["mint"]] / sol_usd
                if value < DUST_SOL:
                    dust.append(a)
        if not dust:
            return 0, 0.0
        n = await self.rpc.burn_and_close(kp, dust)
        if n:
            self._bal.pop(pub, None)
            for a in dust[:n]:
                if a["raw"] > 0:
                    basis = self.cost_basis(u["tg_id"], a["mint"], False)
                    self.record(u["tg_id"], None, None, "sell", a["mint"], 0.0, a["raw"] / 10 ** a["decimals"], "filled",
                                "🧹 pył spalony – odzyskany depozyt konta", False, pnl=-(basis[0] if basis else 0))
            log.info("Solana %s: spalono pył i zamknięto %d kont (+%.4f SOL)", u["tg_id"], n, n * ATA_RENT / LAMPORTS)
        return n, n * ATA_RENT / LAMPORTS

    async def paper_close_unroutable(self, u) -> list[str]:
        """Ćwiczenia: zamyka pozycje w tokenach bez trasy wymiany (np. tokenizowane akcje kupione po cenie
        tradera, zanim ćwiczenia zaczęły odrzucać takie zakupy) – po cenie rynkowej, o 2% gorzej.
        Na prawdziwych SOL nic nie robi. Zwraca symbole zamkniętych."""
        if self.is_live(u):
            return []
        uid = u["tg_id"]
        hold = {m: r for m, r in self.paper(uid).items() if m != SOL_MINT and r > 0 and not is_launchpad(m)}
        if not hold:
            return []
        try:
            px = await asyncio.wait_for(self.jup.prices(list(hold)[:49] + [SOL_MINT]), 8)
        except Exception:  # noqa: BLE001
            return []
        sol_usd = px.get(SOL_MINT)
        closed = []
        for mint, raw in hold.items():
            if not sol_usd or mint not in px:
                continue
            try:
                await self.jup.quote(mint, SOL_MINT, raw, 3000)
                continue  # trasa jest – normalna sprzedaż zadziała
            except ChainError as e:
                if not any(x in str(e) for x in ("No routes", "not tradable", "ROUTE", "TRADABLE")):
                    continue
            sym, dec = self.cached_token(mint) or await self.token_info(mint)
            out = int(raw / 10 ** dec * px[mint] / sol_usd * LAMPORTS * 0.98)
            basis = self.cost_basis(uid, mint, True)
            raw, out = self.paper_trade(u, "sell", mint, raw, out)
            fill = self.sell_pnl(basis, Fill(out / LAMPORTS, raw / 10 ** dec, None, 0, note="po cenie rynkowej"))
            self.record(uid, None, None, "sell", mint, fill.sol, fill.tokens, "filled",
                        "ćwiczenia: zamknięte po cenie rynkowej (token bez trasy wymiany)", True, pnl=fill.pnl)
            closed.append(sym)
            await asyncio.sleep(0.3)
        return closed

    def after_sell(self, u, mint: str, sold_all: bool):
        """Po sprzedaży całości na prawdziwych SOL – w tle zamykamy puste konto tokena (odzysk depozytu)."""
        if sold_all and self.is_live(u):
            async def later():
                await asyncio.sleep(3)
                try:
                    await self.reclaim_rent(u, mint)
                except Exception:  # noqa: BLE001
                    log.debug("Zamykanie konta tokena", exc_info=True)
            asyncio.create_task(later())

    async def sell_with_retry(self, u, mint: str, amount: int, t_recv: float | None = None,
                              decimals: int | None = None, fallback: Swap | None = None) -> "Fill":
        """Sprzedaż, która nie weszła (cena uciekła, chwilowy limit, token przeszedł na giełdę), jest ponawiana
        do 2 razy z większą tolerancją ceny – lepiej sprzedać trochę taniej niż zostać ze spadającym tokenem."""
        uu = dict(u) if not isinstance(u, dict) else dict(u)
        for attempt in range(3):
            try:
                return await self.execute(uu, "sell", mint, amount, t_recv if attempt == 0 else None,
                                          decimals=decimals, fallback=fallback)
            except ChainError as e:
                if attempt == 2 or not self.is_live(uu):
                    raise
                log.info("Solana %s: sprzedaż %s nie weszła (%s) – ponawiam", uu["tg_id"], mint[:6], str(e)[:120])
                await asyncio.sleep(1.5 * (attempt + 1))
                uu["sol_slippage_bps"] = min(int(uu["sol_slippage_bps"] * 2), 4900)
                amount = min(amount, await self.token_raw(uu, mint))
                if amount <= 0:
                    raise
        raise ChainError("sprzedaż nie weszła")

    @staticmethod
    def turbo(u) -> bool:
        return bool("sol_turbo" in u.keys() and u["sol_turbo"])

    def note_timing(self, side, turbo, t0, t_q, t_s, t_b, confirm_s):
        """Pomiar etapów transakcji LIVE: przygotowanie, wycena, budowa, potwierdzenie (ms)."""
        tm = {"side": side, "turbo": turbo, "prep": int((t_q - t0) * 1000), "quote": int((t_s - t_q) * 1000),
              "build": int((t_b - t_s) * 1000), "confirm": int((confirm_s or 0) * 1000), "ts": now_ms()}
        self.timings = (self.timings + [tm])[-50:]
        log.info("Solana LIVE %s%s: przygotowanie %d ms · wycena %d ms · budowa %d ms · potwierdzenie %d ms",
                 side, " ⚡turbo" if turbo else "", tm["prep"], tm["quote"], tm["build"], tm["confirm"])

    def trade_tip(self, u, lamports: int | None) -> int:
        """Opłata za szybkość / napiwek dla JEDNEJ transakcji: ~0,5% jej kwoty (min. 10 000 lamportów, maks. Twoje
        ustawienie „Szybkość”). Stały napiwek 0,0002 SOL przy sprzedaży za 0,002 SOL to było 10% kwoty."""
        cap = self.priority(u)
        if not lamports:
            return cap
        return int(min(cap, max(10_000, lamports * 0.005)))

    def priority(self, u) -> int:
        """Maks. opłata za szybkość (lamporty) – ustawienie użytkownika albo domyślna z konfiguracji."""
        v = u["sol_priority"] if "sol_priority" in u.keys() and u["sol_priority"] else self.cfg.sol_priority_max_lamports
        return int(min(max(v, 10_000), 10_000_000))

    async def _live_pumpportal(self, u, side, mint, amount_raw, t0, t_recv, decimals) -> Fill:
        pub, kp = self.wallet(u["tg_id"])
        pre_tok = await self.token_raw(u, mint)
        pre_sol = await self.rpc.balance(pub)
        if side == "buy":
            amount, in_sol = round(amount_raw / LAMPORTS, 9), True
        else:
            amount, in_sol = ("100%" if amount_raw >= pre_tok else amount_raw / 10 ** decimals), False
        self._fresh(t_recv, side)
        try:
            tx = await self.pump.tx(kp, side, mint, amount, in_sol, u["sol_slippage_bps"] / 100,
                                    self.trade_tip(u, amount_raw if side == "buy" else None) / LAMPORTS)
        except ChainError as e:
            raise Skip(f"brak trasy wymiany (Jupiter i Pump.fun): {e}") from e
        lat = int((time.monotonic() - t0) * 1000)
        self._bal.pop(pub, None)
        sig, confirm_s = await self.rpc.send_and_confirm(tx)
        try:
            ch = await self.rpc.balance_change(sig, pub, mint)
        except Exception:  # noqa: BLE001
            ch = None
        if ch:
            sol_d, tok_d, new_acc = ch
            if side == "buy" and tok_d > 0:
                return Fill((-sol_d - (ATA_RENT if new_acc else 0)) / LAMPORTS, tok_d / 10 ** decimals, sig, lat,
                            confirm_s, note="przez Pump.fun")
            if side == "sell" and tok_d < 0:
                return Fill(max(sol_d, 0) / LAMPORTS, -tok_d / 10 ** decimals, sig, lat, confirm_s, note="przez Pump.fun")
        try:  # zapas: rzeczywiste ilości z sald po transakcji
            post_tok, post_sol = await self.token_raw(u, mint), await self.rpc.balance(pub)
        except Exception:  # noqa: BLE001
            post_tok, post_sol = pre_tok, pre_sol
        if side == "buy":
            sol, tokens = amount_raw / LAMPORTS, max(post_tok - pre_tok, 0) / 10 ** decimals
        else:
            sol, tokens = max(post_sol - pre_sol, 0) / LAMPORTS, amount_raw / 10 ** decimals
        return Fill(sol, tokens, sig, lat, confirm_s, note="przez Pump.fun")

    # ---------- podłączanie własnego portfela (np. z Axiom) ----------
    @staticmethod
    def parse_secret(text: str) -> Keypair:
        """Klucz prywatny w formacie Axiom/Phantom (base58) albo solana-keygen (lista 64 liczb)."""
        s = (text or "").strip().strip('"').strip("'")
        try:
            if s.startswith("["):
                raw = bytes(json.loads(s))
            else:
                raw = b58decode(s)
            if len(raw) == 64:
                kp = Keypair.from_bytes(raw)
            elif len(raw) == 32:
                kp = Keypair.from_seed(raw)
            else:
                raise ValueError(len(raw))
        except Exception:  # noqa: BLE001
            raise Skip("to nie wygląda na klucz prywatny Solany – w Axiom: Settings → Wallets → Export private "
                       "key, skopiuj cały ciąg znaków") from None
        return kp

    def wallet_source(self, tg_id: int) -> str | None:
        r = self.db._q("SELECT source FROM sol_wallets WHERE tg_id = ?", (tg_id,))
        return r[0]["source"] if r else None

    def import_wallet(self, tg_id: int, kp: Keypair, source: str = "import") -> str:
        """Podłącza własny portfel. Poprzedni (np. utworzony przez bota) trafia do kopii – nic nie ginie."""
        self.wallet(tg_id)
        old = self.db._q("SELECT * FROM sol_wallets WHERE tg_id = ?", (tg_id,))[0]
        pub = str(kp.pubkey())
        if old["pubkey"] == pub:
            return pub
        with self.db.lock, self.db.conn:
            self.db.conn.execute("INSERT INTO sol_wallet_backup (tg_id, pubkey, secret, source, replaced_at) "
                                 "VALUES (?, ?, ?, ?, ?)", (tg_id, old["pubkey"], old["secret"], old["source"], now_ms()))
            self.db.conn.execute("UPDATE sol_wallets SET pubkey = ?, secret = ?, source = ? WHERE tg_id = ?",
                                 (pub, self.vault.enc(str(kp)), source, tg_id))
        self._bal.pop(old["pubkey"], None)
        return pub

    def restore_wallet(self, tg_id: int) -> str | None:
        """Wraca do poprzedniego portfela (np. portfela bota) – obecny trafia do kopii."""
        rows = self.db._q("SELECT * FROM sol_wallet_backup WHERE tg_id = ? ORDER BY id DESC LIMIT 1", (tg_id,))
        if not rows:
            return None
        b = rows[0]
        cur = self.db._q("SELECT * FROM sol_wallets WHERE tg_id = ?", (tg_id,))[0]
        with self.db.lock, self.db.conn:
            self.db.conn.execute("DELETE FROM sol_wallet_backup WHERE id = ?", (b["id"],))
            self.db.conn.execute("INSERT INTO sol_wallet_backup (tg_id, pubkey, secret, source, replaced_at) "
                                 "VALUES (?, ?, ?, ?, ?)", (tg_id, cur["pubkey"], cur["secret"], cur["source"], now_ms()))
            self.db.conn.execute("UPDATE sol_wallets SET pubkey = ?, secret = ?, source = ? WHERE tg_id = ?",
                                 (b["pubkey"], b["secret"], b["source"] or "bot", tg_id))
        self._bal.pop(cur["pubkey"], None)
        return b["pubkey"]

    async def _trader_price(self, sw: Swap) -> float | None:
        """Cena z transakcji tradera w lamportach za jednostkę tokena (płacił/dostał SOL albo USDC/USDT)."""
        if sw.token_raw <= 0 or sw.quote_amount <= 0:
            return None
        if sw.quote_symbol == "SOL":
            return sw.quote_amount * LAMPORTS / sw.token_raw
        try:
            sol_usd = (await asyncio.wait_for(self.jup.prices([SOL_MINT]), 3)).get(SOL_MINT)
        except Exception:  # noqa: BLE001
            sol_usd = None
        return sw.quote_amount / sol_usd * LAMPORTS / sw.token_raw if sol_usd else None

    def _paper_at_trader_price(self, u, side, mint, amount_raw, sw: Swap, t0: float, decimals: int,
                               per: float | None = None) -> Fill:
        """Ćwiczenia: Jupiter nie zna jeszcze tokena (np. świeży z Pump.fun) – liczymy po cenie z transakcji
        tradera, o 2% gorzej (poślizg). Na prawdziwych SOL takiej kopii nie da się zrobić bez trasy."""
        per = per or sw.quote_amount * LAMPORTS / sw.token_raw
        if side == "buy":
            got = int(amount_raw / per * 0.98)
            self.paper_trade(u, side, mint, amount_raw, got)
            sol_raw, tok_raw = amount_raw, got
        else:
            out = int(amount_raw * per * 0.98)
            amount_raw, out = self.paper_trade(u, side, mint, amount_raw, out)
            sol_raw, tok_raw = out, amount_raw
        return Fill(sol_raw / LAMPORTS, tok_raw / 10 ** decimals, None, int((time.monotonic() - t0) * 1000),
                    note="po cenie tradera")

    async def sell_manual(self, u, mint: str, pct: int) -> Fill:
        held = await self.token_raw(u, mint)
        if held <= 0:
            raise Skip("nie masz tego tokena")
        basis = self.cost_basis(u["tg_id"], mint, not self.is_live(u))
        try:
            fill = self.sell_pnl(basis, await self.sell_with_retry(u, mint, held if pct >= 100 else int(held * pct / 100)))
        except ChainError as e:
            raise Skip(friendly_error(e, u)) from e
        self.after_sell(u, mint, pct >= 100)
        self.record(u["tg_id"], None, None, "sell", mint, fill.sol, fill.tokens, "filled", "ręcznie",
                    not self.is_live(u), fill.sig, fill.latency_ms, fill.pnl)
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
            if sw.quote_symbol == "SOL" and sw.token_raw > 0 and sw.quote_amount > 0:
                # po cenie z transakcji samego tradera – bez zapytania do Jupitera (darmowy limit: 10 / 10 s
                # zostaje na prawdziwe kopie; wcześniej portfele wzorcowe zjadały połowę limitu)
                per_token = sw.quote_amount * LAMPORTS / sw.token_raw  # lamporty za 1 jednostkę tokena
                if sw.side == "buy":
                    got = int(amount / per_token)
                    self.paper_apply(rid, {SOL_MINT: -(amount + PAPER_TX_FEE), sw.mint: got})
                    sol, tok = amount / LAMPORTS, got / 10 ** sw.decimals
                else:
                    out = int(amount * per_token)
                    self.paper_apply(rid, {sw.mint: -amount, SOL_MINT: out - PAPER_TX_FEE})
                    sol, tok = out / LAMPORTS, amount / 10 ** sw.decimals
            else:
                ref = {"tg_id": rid, "sol_live": 0, "sol_slippage_bps": 1000}
                fill = await self.execute(ref, sw.side, sw.mint, amount)
                sol, tok = fill.sol, fill.tokens
            # info = za ile handlował sam trader (SOL) – optymalizator odtwarza z tego różne strategie
            self.record(rid, lead["id"], sw.signature, sw.side, sw.mint, sol, tok, "filled",
                        f"{sw.quote_amount:.6g}" if sw.quote_symbol == "SOL" else None, True)
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
        start = self.cfg.sol_paper_start  # portfel wzorcowy tradera zawsze startuje z tej samej kwoty
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
