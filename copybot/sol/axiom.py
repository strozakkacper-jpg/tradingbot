"""Pulpit główny w stylu Axiom: kopiowanie portfeli Solany na pierwszym planie.

- pulpit: cena SOL, saldo, pozycje, wynik, kopiowane portfele, ostatnie ruchy
- karta portfela: wyniki, kopiuj / tylko powiadomienia / usuń, osobna kwota zakupu dla tego portfela
- ruchy na żywo, pozycje z PnL, karta tokena po wklejeniu adresu (CA) z szybkim kupnem/sprzedażą
- automatyczne take-profit / stop-loss pozycji
"""
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from ..bot import S, UI_TIMEOUT, admin_ids, header, require_accepted, show
from ..db import now_ms
from .engine import Skip, fmt_tokens, short
from .parser import LAMPORTS, SOL_MINT
from .ui import valid_address

log = logging.getLogger(__name__)
MAIN = [B("« Pulpit", callback_data="home")]
FOLLOW_AMOUNTS = (0.05, 0.1, 0.25, 0.5, 1.0)
FOLLOW_PCTS = (5, 10, 25, 50, 100)
TPS = (0, 50, 100, 200, 500)
SLS = (0, 20, 30, 50)
ADD_ADDR, BUY_X = 70, 71


async def _ack(update: Update, text=None, alert=False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=alert)
        except BadRequest:
            pass


def usd(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1_000_000_000:
        return f"${x / 1e9:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1e6:.2f}M"
    if x >= 1_000:
        return f"${x / 1e3:.1f}K"
    if x >= 1:
        return f"${x:.2f}"
    from ..engine import fmt_price
    return f"${fmt_price(x)}"


def pct(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) < 0.05:
        return "0.0%"
    return f"{'🟢' if x >= 0 else '🔴'}{x:+.1f}%"


def tp_txt(u) -> str:
    return f"+{u['sol_tp']:g}%" if u["sol_tp"] else "wył."


def sl_txt(u) -> str:
    return f"−{u['sol_sl']:g}%" if u["sol_sl"] else "wył."


def hhmm(s, ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(s.cfg.timezone)).strftime("%H:%M")


def sol_leaders(s, uid: int) -> list[tuple]:
    """[(trader, kopiuje?), …] – śledzone portfele Solany użytkownika."""
    follows = s.db.follows_of(uid)
    out = []
    for lid, copy in follows.items():
        lead = s.db.get_leader(lid)
        if lead and lead["source"] == "sol":
            out.append((lead, copy))
    return out


async def portfolio(s, u) -> dict:
    """Saldo SOL, pozycje z wartością i PnL – jednym zapytaniem o ceny."""
    se = s.sol
    lam = await se.sol_lamports(u, max_age=5)
    hold = await se.holdings(u)
    values = await se.values_sol(hold)
    try:
        sol_usd = (await se.jup.prices([SOL_MINT])).get(SOL_MINT)
    except Exception:  # noqa: BLE001
        sol_usd = None
    positions = []
    for mint, raw in hold.items():
        sym, dec = se.cached_token(mint) or await se.token_info(mint)
        val = values.get(mint)
        basis = se.cost_basis(u["tg_id"], mint, not se.is_live(u))
        pnl = (val / basis[0] - 1) * 100 if (val is not None and basis and basis[0] > 0) else None
        positions.append({"mint": mint, "symbol": sym, "tokens": raw / 10 ** dec, "value": val, "pnl": pnl,
                          "cost": basis[0] if basis else None})
    positions.sort(key=lambda p: -(p["value"] or 0))
    total = lam / LAMPORTS + sum(p["value"] or 0 for p in positions)
    return {"sol": lam / LAMPORTS, "positions": positions, "total": total, "sol_usd": sol_usd}


# ---------------- teksty po ludzku ----------------

def mode_label(live: bool) -> str:
    return "💰 prawdziwe pieniądze" if live else "🧪 ćwiczenia (wirtualne SOL)"


def in_usd(sol: float | None, sol_usd: float | None) -> str:
    if sol is None or not sol_usd:
        return ""
    v = sol * sol_usd
    return f" (≈ {usd(v)})" if abs(v) >= 1 else f" (≈ ${v:.2f})"


def plural_wallets(n: int) -> str:
    if n == 1:
        return "1 portfel"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} portfele"
    return f"{n} portfeli"


def follow_amount_txt(fr) -> str | None:
    """Osobna kwota ustawiona dla konkretnego portfela (None = jak w ustawieniach)."""
    if fr and fr["sol_buy"]:
        return f"{fr['sol_buy']:g} SOL"
    if fr and fr["sol_pct"]:
        return f"{fr['sol_pct']:g}% jego kwoty"
    return None


def risk(info: dict) -> tuple[str, list[str]]:
    """Prosta ocena ryzyka tokena (🟢/🟡/🔴) + wyjaśnienia dla laika. To podpowiedź, nie gwarancja."""
    a = info.get("audit") or {}
    score, notes = 0, []
    if a.get("mintAuthorityDisabled") is not None:
        if a["mintAuthorityDisabled"]:
            notes.append("✅ mint wył. – nikt nie dodrukuje nowych tokenów")
        else:
            score += 2
            notes.append("⚠️ twórca może dodrukować tokeny (Twoje stracą wartość)")
    if a.get("freezeAuthorityDisabled") is not None:
        if a["freezeAuthorityDisabled"]:
            notes.append("✅ nikt nie może zamrozić Twoich tokenów")
        else:
            score += 2
            notes.append("⚠️ twórca może zamrozić Twoje tokeny (nie sprzedasz ich)")
    liq = info.get("liquidity")
    if liq is not None:
        if liq < 10_000:
            score += 2
            notes.append(f"⚠️ bardzo mało pieniędzy w puli ({usd(liq)}) – sprzedaż może mocno zbić cenę")
        elif liq < 50_000:
            score += 1
            notes.append(f"🟡 niewiele pieniędzy w puli ({usd(liq)})")
    holders = info.get("holderCount")
    if holders is not None:
        if holders < 100:
            score += 2
            notes.append("⚠️ bardzo mało posiadaczy – token jest świeży albo nikt go nie chce")
        elif holders < 1000:
            score += 1
    top = a.get("topHoldersPercentage")
    if top is not None:
        if top > 50:
            score += 2
            notes.append(f"⚠️ 10 największych portfeli ma {top:.0f}% tokenów – mogą nagle sprzedać i zbić cenę")
        elif top > 30:
            score += 1
            notes.append(f"🟡 10 największych portfeli ma {top:.0f}% tokenów")
    level = "🟢 niskie" if score <= 1 else "🟡 średnie" if score <= 3 else "🔴 wysokie"
    return level, notes


# ---------------- widoki ----------------

async def home_view(s, u, notice: str | None = None) -> tuple[str, KB]:
    se = s.sol
    uid = u["tg_id"]
    se.wallet(uid)
    live = se.is_live(u)
    pf = await portfolio(s, u)
    px = pf["sol_usd"]
    try:
        info = await se.jup.token_full(SOL_MINT)
        sol_ch = (info or {}).get("stats24h", {}).get("priceChange")
    except Exception:  # noqa: BLE001
        sol_ch = None
    wallets = sol_leaders(s, uid)
    n_copy = sum(1 for _, c in wallets if c)
    from .ui import mode_txt
    amount = mode_txt(u["sol_buy_mode"], u["sol_buy"], u["sol_pct"], u["sol_mult"])
    if not wallets:
        status = ("👉 <b>Zacznij tutaj:</b> kliknij <b>➕ Dodaj portfel</b> i wklej adres tradera, którego chcesz "
                  "kopiować. Bot będzie powtarzał jego zakupy i sprzedaże.")
    elif not u["active"]:
        status = "⏸ <b>Bot stoi.</b> Kliknij <b>▶️ Włącz kopiowanie</b>, żeby zaczął powtarzać ruchy traderów."
    elif not n_copy:
        status = "👁 Tylko obserwujesz – kliknij portfel i <b>✅ Kopiuj</b>, żeby bot powtarzał jego ruchy."
    else:
        status = (f"✅ <b>Bot działa.</b> Kopiuje {plural_wallets(n_copy)}. Gdy trader kupi albo sprzeda token, "
                  f"Ty zrobisz to samo automatycznie – {amount}.")
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"🟣 <b>Copy Trading · Solana</b> · {mode_label(live)}", "", status]
    if s.db.get_state("paused") == "1":
        lines.append("⏸ <i>wstrzymane przez admina</i>")
    tokens_val = pf["total"] - pf["sol"]
    lines += ["", f"👛 Wolne: <b>{pf['sol']:.4f} SOL</b>{in_usd(pf['sol'], px)}",
              f"🪙 Tokeny: pozycje {len(pf['positions'])} ≈ {tokens_val:.3f} SOL{in_usd(tokens_val, px)}"]
    if not live:
        lines.append(f"📈 Wynik ćwiczeń: {pct((pf['total'] / s.cfg.sol_paper_start - 1) * 100)} "
                     f"(start {s.cfg.sol_paper_start:g} SOL → teraz {pf['total']:.4f} SOL)")
    if px:
        lines.append(f"Kurs SOL {usd(px)}" + (f" · {pct(sol_ch)} w 24 h" if sol_ch is not None else ""))
    last = s.db._q("SELECT latency_ms FROM sol_trades WHERE tg_id = ? AND latency_ms IS NOT NULL ORDER BY id DESC "
                   "LIMIT 1", (uid,))
    if last:
        lines.append(f"⚡ Ostatnia kopia w {last[0]['latency_ms'] / 1000:.2f} s od ruchu tradera")

    lines += ["", f"👥 <b>Kopiowane portfele</b> ({len(wallets)}) – możesz kopiować kilka naraz"]
    day_ago = now_ms() - 24 * 3600 * 1000
    rows = []
    for lead, copy in wallets[:8]:
        n = s.db._q("SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND leader_id = ? AND status = 'filled' "
                    "AND ts >= ?", (uid, lead["id"], day_ago))[0]["n"]
        try:
            rep = await asyncio.wait_for(se.report(lead["id"]), 5)
            res = pct(rep["total"] * 100)
        except Exception:  # noqa: BLE001
            res = "—"
        icon = "✅" if copy else "👁"
        own = follow_amount_txt(s.db.follow_row(uid, lead["id"]))
        lines.append(f"{icon} <b>{html.escape(lead['name'])}</b> · jego wynik {res} · kopii 24h: {n}"
                     + (f" · {own}" if own else ""))
        rows.append([B(f"{icon} {lead['name'][:30]}", callback_data=f"aw:{lead['id']}")])
    if wallets:
        lines.append("<i>✅ = kopiujesz · 👁 = tylko powiadomienia</i>")

    feed = se.history(uid, 4)
    if feed:
        lines += ["", "📡 <b>Ostatnie ruchy</b>"]
        for r in feed:
            lines.append(_feed_line(s, r))
    lines += ["", "<i>💡 Wklej adres portfela – zaczniesz go kopiować. Wklej adres tokena – zobaczysz, czy jest "
                  "bezpieczny, i kupisz go ręcznie.</i>"]

    kb = [[B("⏸ Wyłącz kopiowanie" if u["active"] else "▶️ Włącz kopiowanie", callback_data="ax:tog")],
          [B("➕ Dodaj portfel", callback_data="ax:add")]] + rows + [
          [B("💼 Moje tokeny", callback_data="ax:pos"), B("📡 Historia", callback_data="ax:feed")],
          [B("📊 Raport sesji", callback_data="ax:rep"), B("🔥 Popularne", callback_data="ax:trend")],
          [B("⚙️ Ile kupować / ustawienia", callback_data="ax:set")],
          [B("👛 Portfel / wpłata", callback_data="sx:wallet"),
           B("Na 💰 prawdziwe" if not live else "Wróć do 🧪 ćwiczeń", callback_data="sx:mode")],
          [B("❓ Jak to działa", callback_data="ax:help"), B("📖 Słowniczek", callback_data="ax:dict")],
          [B("🏦 Giełdy (CEX) →", callback_data="menu")] + ([B("🛠 Admin", callback_data="admin")]
                                                          if uid in admin_ids(s) else []),
          [B("🔄 Odśwież", callback_data="home")]]
    return "\n".join(lines), KB(kb)


HELP = """❓ <b>Jak to działa – w 4 krokach</b>

<b>1. Wybierasz tradera.</b> Na Solanie każdy portfel jest publiczny – widać, co kto kupuje. Znajdź kogoś, kto dobrze zarabia (np. na gmgn.ai, kolscan.io albo w Axiom) i skopiuj adres jego portfela.

<b>2. Dodajesz go do bota.</b> ➕ Dodaj portfel → wklejasz adres. Możesz dodać kilku traderów naraz – bot kopiuje wszystkich jednocześnie.

<b>3. Ustalasz, ile grać.</b> ⚙️ Ustawienia: stała kwota (np. 0,05 SOL na każdy zakup) albo % kwoty tradera (np. 10%: on kupi za 5 SOL, Ty za 0,5 SOL). Każdemu traderowi możesz ustawić inną kwotę na jego karcie.

<b>4. Włączasz ▶️ kopiowanie.</b> Od teraz, gdy trader coś kupi, bot w ułamku sekundy kupuje to samo za Twoją kwotę. Gdy sprzeda – sprzedajesz tę samą część. Dostajesz powiadomienie o każdym ruchu, a po wyłączeniu – raport, ile zarobiłeś.

🧪 <b>Ćwiczenia</b>: na start bot gra wirtualnymi 5 SOL – sprawdzisz tradera bez ryzyka. Gdy wyniki Ci się podobają, przejdź na 💰 prawdziwe pieniądze (wpłać SOL na swój adres z „👛 Portfel”).

🛑 <b>Bezpieczniki</b>: maks. kwota na jeden zakup, automatyczna sprzedaż przy zysku albo stracie, pomijanie spóźnionych kopii.

⚠️ Memecoiny to bardzo ryzykowna gra – nawet dobry trader potrafi mieć serię strat. Graj tylko kwotą, którą możesz stracić."""

GLOSSARY = """📖 <b>Słowniczek</b>

<b>SOL</b> – waluta sieci Solana. Za nią kupujesz tokeny.
<b>Token / memecoin</b> – „żetonowa” kryptowaluta; ich ceny potrafią skoczyć ×10 albo spaść do zera w kilka minut.
<b>Portfel (adres)</b> – konto w sieci Solana, np. 7xKX…AsU. Każdy widzi, co się na nim dzieje.
<b>Trader / lider</b> – osoba, której ruchy kopiujesz.
<b>Pozycja</b> – token, który masz kupiony i jeszcze nie sprzedałeś.
<b>Zysk/strata (PnL)</b> – o ile zmieniła się wartość tego, co kupiłeś.
<b>Kapitalizacja (MC)</b> – ile wart jest cały token (cena × liczba wszystkich tokenów).
<b>Płynność</b> – ile pieniędzy jest w puli wymiany. Mała płynność = trudno sprzedać bez dużego spadku ceny.
<b>Posiadacze (holderzy)</b> – ile portfeli ma ten token.
<b>Tolerancja ceny (poślizg, slippage)</b> – o ile cena może się zmienić, zanim zakup dojdzie do skutku.
<b>Take-profit</b> – automatyczna sprzedaż, gdy zarobisz np. +100%.
<b>Stop-loss</b> – automatyczna sprzedaż, gdy tracisz np. −30%, żeby nie stracić więcej.
<b>Ćwiczenia (demo)</b> – gra wirtualnymi SOL, bez ryzyka.
<b>Sesja</b> – czas od ▶️ włączenia do ⏸ wyłączenia kopiowania; po niej dostajesz raport."""


def _feed_line(s, r) -> str:
    sym = html.escape(r["symbol"] or short(r["mint"]))
    t = hhmm(s, r["ts"])
    src = html.escape(r["info"] or "")
    if r["status"] == "filled":
        lat = f" ⚡{r['latency_ms'] / 1000:.2f} s" if r["latency_ms"] is not None else ""
        verb = "kupiono" if r["side"] == "buy" else "sprzedano"
        return f"{'🟢' if r['side'] == 'buy' else '🔴'} {t} {verb} <b>{sym}</b> za {r['sol']:.4f} SOL · {src}{lat}"
    return f"⚠️ {t} {sym} – nie skopiowano: <i>{html.escape((r['info'] or r['status'])[:60])}</i>"


async def wallet_card(s, u, lid: int, notice: str | None = None) -> tuple[str, KB]:
    lead = s.db.get_leader(lid)
    if not lead or lead["status"] != "active":
        return "Ten portfel nie jest już śledzony.", KB([MAIN])
    uid = u["tg_id"]
    fr = s.db.follow_row(uid, lid)
    status = None if not fr else bool(fr["copy"])
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"👤 <b>{html.escape(lead['name'])}</b>", f"<code>{lead['wallet']}</code>",
        f'<a href="https://solscan.io/account/{lead["wallet"]}">Solscan</a> · '
        f'<a href="https://gmgn.ai/sol/address/{lead["wallet"]}">GMGN – jego wyniki</a>', ""]
    try:
        rep = await asyncio.wait_for(s.sol.report(lid), 8)
        wr = f"{rep['win_rate'] * 100:.0f}%" if rep["win_rate"] is not None else "—"
        lines += [f"📊 <b>Jak mu idzie:</b> {pct(rep['total'] * 100)} – tyle zarobiłby ktoś, kto kopiuje każdy jego "
                  f"ruch od {hhmm(s, rep['since'])}",
                  f"Ostatnie 7 dni {pct(rep['d7'] * 100 if rep['d7'] is not None else None)} · udane transakcje {wr} "
                  f"({rep['wins']} z {rep['closed']}) · największy spadek po drodze {pct(-rep['max_dd'] * 100)}"]
    except Exception:  # noqa: BLE001
        lines.append("📊 Wyniki chwilowo niedostępne")
    mine = s.db._q("""SELECT side, COUNT(*) n, SUM(sol) sol FROM sol_trades WHERE tg_id = ? AND leader_id = ?
                      AND status = 'filled' GROUP BY side""", (uid, lid))
    d = {r["side"]: (r["n"], r["sol"] or 0) for r in mine}
    lines.append(f"🔁 Twoje kopie: kupna {d.get('buy', (0, 0))[0]} ({d.get('buy', (0, 0))[1]:.3f} SOL) · "
                 f"sprzedaże {d.get('sell', (0, 0))[0]} ({d.get('sell', (0, 0))[1]:.3f} SOL)")
    own = follow_amount_txt(fr)
    from .ui import mode_txt
    default = mode_txt(u["sol_buy_mode"], u["sol_buy"], u["sol_pct"], u["sol_mult"])
    lines.append(f"💵 Gdy on kupi, Ty kupujesz za: <b>{own or f'jak w ustawieniach ({default})'}</b>")
    lines.append({True: "\n✅ <b>Kopiujesz</b> ten portfel – bot powtarza jego zakupy i sprzedaże.",
                  False: "\n👁 Tylko powiadomienia – widzisz jego ruchy, ale bot nic nie kupuje.",
                  None: "\n▫️ Nie śledzisz."}[status])

    def mark(cur, v, label):
        return f"✓ {label}" if cur and abs(cur - v) < 1e-9 else label
    kb = []
    if status is True:
        kb.append([B("👁 Tylko powiadomienia", callback_data=f"wt:{lid}"), B("🔕 Usuń", callback_data=f"uf:{lid}")])
    elif status is False:
        kb.append([B("✅ Kopiuj", callback_data=f"cp:{lid}"), B("🔕 Usuń", callback_data=f"uf:{lid}")])
    else:
        kb.append([B("✅ Kopiuj", callback_data=f"cp:{lid}"), B("👁 Obserwuj", callback_data=f"wt:{lid}")])
    sol_buy = fr["sol_buy"] if fr else None
    sol_pct = fr["sol_pct"] if fr else None
    kb += [header("Stała kwota na każdy zakup"),
           [B("✓ jak w ustawieniach" if not own else "jak w ustawieniach", callback_data=f"aa:{lid}:0")],
           [B(mark(sol_buy, v, f"{v:g} SOL"), callback_data=f"aa:{lid}:{v}") for v in FOLLOW_AMOUNTS],
           header("…albo % tego, za ile kupił on"),
           [B(mark(sol_pct, v, f"{v:g}%"), callback_data=f"ap:{lid}:{v:g}") for v in FOLLOW_PCTS],
           [B("✏️ Zmień nazwę", callback_data=f"ren:{lid}"), B("🔄 Odśwież", callback_data=f"aw:{lid}")], MAIN]
    return "\n".join(lines), KB(kb)


async def positions_view(s, u) -> tuple[str, KB]:
    pf = await portfolio(s, u)
    px = pf["sol_usd"]
    lines = [f"💼 <b>Moje tokeny</b> · {mode_label(s.sol.is_live(u))}", ""]
    kb = []
    for p in pf["positions"][:12]:
        val = f"{p['value']:.4f} SOL{in_usd(p['value'], px)}" if p["value"] is not None else "brak ceny"
        lines.append(f"• <b>{html.escape(p['symbol'])}</b> {fmt_tokens(p['tokens'])} szt. ≈ {val} · "
                     f"zysk/strata {pct(p['pnl'])}")
        kb.append([B(f"{p['symbol'][:12]} {pct(p['pnl'])}", callback_data=f"tk:{p['mint']}"),
                   B("🔴 Sprzedaj wszystko", callback_data=f"ts:100:{p['mint']}")])
    if not pf["positions"]:
        lines.append("Nie masz jeszcze żadnych tokenów – pojawią się tu, gdy bot skopiuje czyjś zakup.")
    else:
        lines.append("\n<i>Kliknij token, żeby zobaczyć szczegóły albo sprzedać część.</i>")
    lines += ["", f"Wolne SOL: {pf['sol']:.4f} · wszystko razem ≈ <b>{pf['total']:.4f} SOL</b>{in_usd(pf['total'], px)}"]
    if u["sol_tp"] or u["sol_sl"]:
        lines.append(f"Auto-sprzedaż: przy zysku {tp_txt(u)} · przy stracie {sl_txt(u)}")
    kb += [[B("🔄 Odśwież", callback_data="ax:pos")], MAIN]
    return "\n".join(lines), KB(kb)


def feed_view(s, u) -> tuple[str, KB]:
    rows = s.sol.history(u["tg_id"], 15)
    lines = ["📡 <b>Historia ruchów</b> (kopie i Twoje ręczne transakcje)", ""]
    lines += [_feed_line(s, r) for r in rows] or ["Jeszcze nic – ruchy pojawią się, gdy kopiowany trader coś kupi."]
    return "\n".join(lines), KB([[B("🔄 Odśwież", callback_data="ax:feed")], MAIN])


def settings_view(s, u) -> tuple[str, KB]:
    from .ui import settings_view as base
    text, kb = base(u)
    text += (f"\n\n🎯 <b>Sprzedaj automatycznie przy zysku</b> (take-profit): {tp_txt(u)}\n"
             f"🛑 <b>Sprzedaj automatycznie przy stracie</b> (stop-loss): {sl_txt(u)}\n"
             "Bot co minutę sprawdza Twoje tokeny. Np. +100% = sprzeda, gdy token podwoi wartość; −30% = sprzeda, "
             "gdy stracisz 30%, żeby nie stracić więcej.")
    rows = list(kb.inline_keyboard[:-1])
    rows += [header("Sprzedaj przy zysku (take-profit)"),
             [B(("✓ " if abs(u["sol_tp"] - v) < 1e-9 else "") + (f"+{v}%" if v else "wył"), callback_data=f"ax:tp:{v}")
              for v in TPS],
             header("Sprzedaj przy stracie (stop-loss)"),
             [B(("✓ " if abs(u["sol_sl"] - v) < 1e-9 else "") + (f"−{v}%" if v else "wył"), callback_data=f"ax:sl:{v}")
              for v in SLS], MAIN]
    return text, KB(rows)


async def token_card(s, u, mint: str, notice: str | None = None) -> tuple[str, KB]:
    se = s.sol
    info = await se.jup.token_full(mint)
    if not info:
        return f"❌ Nie znalazłem tokena <code>{html.escape(mint)}</code>.", KB([MAIN])
    held = await se.token_raw(u, mint)
    dec = int(info.get("decimals") or 0)
    se.db._q("INSERT OR REPLACE INTO sol_tokens VALUES (?, ?, ?)", (mint, info.get("symbol") or short(mint), dec))
    st = {k: (info.get(f"stats{k}") or {}).get("priceChange") for k in ("5m", "1h", "24h")}
    holders = f"{info['holderCount']:,}".replace(",", " ") if info.get("holderCount") else "—"
    level, notes = risk(info)
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"🪙 <b>{html.escape(info.get('symbol') or '?')}</b> · {html.escape(info.get('name') or '')}",
        f"<code>{mint}</code>", "",
        f"💲 Cena {usd(info.get('usdPrice'))}",
        f"🏷 Wartość całego tokena: {usd(info.get('mcap'))} · pieniądze w puli: {usd(info.get('liquidity'))}",
        f"📈 Zmiana ceny: 5 min {pct(st['5m'])} · 1 h {pct(st['1h'])} · 24 h {pct(st['24h'])}",
        f"👥 Posiadacze: {holders}", "",
        f"🛡 <b>Ryzyko: {level}</b>",
    ] + [f"  {n}" for n in notes] + ["<i>To tylko podpowiedź – nawet „🟢” token może spaść do zera.</i>"]
    if held > 0:
        vals = await se.values_sol({mint: held})
        basis = se.cost_basis(u["tg_id"], mint, not se.is_live(u))
        v = vals.get(mint)
        pnl = (v / basis[0] - 1) * 100 if (v and basis and basis[0] > 0) else None
        lines.append(f"\n💼 Masz {fmt_tokens(held / 10 ** dec)} szt. ≈ {v:.4f} SOL · zysk/strata {pct(pnl)}"
                     if v is not None else f"\n💼 Masz {fmt_tokens(held / 10 ** dec)} szt.")
    lines.append(f"\n{mode_label(se.is_live(u))} · tolerancja ceny {u['sol_slippage_bps'] / 100:g}%")
    presets = [float(x) for x in (u["sol_presets"] or "0.05,0.1,0.5,1").split(",") if x.strip()][:4]
    kb = [header("🟢 Kup za (SOL)"),
          [B(f"{p:g}", callback_data=f"tb:{p:g}:{mint}") for p in presets] + [B("inna kwota", callback_data=f"tbx:{mint}")]]
    if held > 0:
        kb += [header("🔴 Sprzedaj część"),
               [B(f"{p}%" if p < 100 else "wszystko", callback_data=f"ts:{p}:{mint}") for p in (25, 50, 100)]]
    kb += [[B("🔄 Odśwież", callback_data=f"tk:{mint}"),
            B("📈 Wykres ceny", url=f"https://dexscreener.com/solana/{mint}")], MAIN]
    return "\n".join(lines), KB(kb)


async def trend_view(s, u) -> tuple[str, KB]:
    items = await s.sol.jup.trending("1h", 10)
    lines = ["🔥 <b>Popularne teraz</b> (trendy z ostatniej godziny)", ""]
    kb, row = [], []
    for i, t in enumerate(items[:10], 1):
        ch = (t.get("stats1h") or {}).get("priceChange")
        lines.append(f"{i}. <b>{html.escape(t.get('symbol') or '?')}</b> · wartość {usd(t.get('mcap'))} · "
                     f"1 h {pct(ch)}")
        row.append(B(t.get("symbol", "?")[:10], callback_data=f"tk:{t['id']}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if not items:
        lines.append("Brak danych.")
    else:
        lines += ["", "<i>Kliknij token, żeby zobaczyć szczegóły i ocenę ryzyka. Popularny ≠ bezpieczny.</i>"]
    kb.append(MAIN)
    return "\n".join(lines), KB(kb)


# ---------------- akcje ----------------

async def buy(s, u, mint: str, sol: float) -> str:
    se = s.sol
    fill = await se.execute(u, "buy", mint, int(sol * LAMPORTS))
    se.record(u["tg_id"], None, None, "buy", mint, fill.sol, fill.tokens, "filled", "ręcznie", not se.is_live(u),
              fill.sig, fill.latency_ms)
    sym, _ = se.cached_token(mint) or await se.token_info(mint)
    return f"✅ Kupiono {fmt_tokens(fill.tokens)} {sym} za {fill.sol:.4f} SOL{await se.usd_txt(fill.sol)}"


async def on_button(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return await _ack(update)
    s = S(ctx)
    if not getattr(s, "sol", None):
        return await _ack(update, "Solana jest wyłączona", True)
    data = update.callback_query.data
    uid = u["tg_id"]
    p = data.split(":")
    try:
        if data == "home":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(home_view(s, u), UI_TIMEOUT))
        if data == "ax:tog":
            from . import session
            if u["active"]:
                s.db.update_user(uid, active=0)
                await _ack(update, "Kopiowanie wyłączone – wysyłam raport sesji 📊")
                await session.end(s, s.db.get_user(uid), ctx.bot, "kopiowanie wyłączone")
            elif not any(c for _, c in sol_leaders(s, uid)) and not s.db.copied_leaders(uid):
                return await _ack(update, "Najpierw dodaj portfel i kliknij ✅ Kopiuj", True)
            else:
                await _ack(update, "Kopiowanie włączone ▶️")
                s.db.update_user(uid, active=1)
                await session.start(s, s.db.get_user(uid))
            return await show(update, *await home_view(s, s.db.get_user(uid)))
        if data == "ax:rep":
            from . import session
            await _ack(update, "⏳")
            text = await asyncio.wait_for(session.build(s, u), UI_TIMEOUT)
            return await show(update, text, KB([[B("🔄 Odśwież", callback_data="ax:rep")], MAIN]))
        if data == "ax:pos":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(positions_view(s, u), UI_TIMEOUT))
        if data == "ax:feed":
            await _ack(update)
            return await show(update, *feed_view(s, u))
        if data == "ax:set":
            await _ack(update)
            return await show(update, *settings_view(s, u))
        if p[0] == "ax" and p[1] in ("tp", "sl"):
            s.db.update_user(uid, **{f"sol_{p[1]}": float(p[2])})
            await _ack(update, "Zapisano")
            return await show(update, *settings_view(s, s.db.get_user(uid)))
        if data in ("ax:help", "ax:dict"):
            await _ack(update)
            return await show(update, HELP if data == "ax:help" else GLOSSARY,
                              KB([[B("📖 Słowniczek" if data == "ax:help" else "❓ Jak to działa",
                                     callback_data="ax:dict" if data == "ax:help" else "ax:help")], MAIN]))
        if data == "ax:trend":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(trend_view(s, u), UI_TIMEOUT))
        if p[0] == "aw":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(wallet_card(s, u, int(p[1])), UI_TIMEOUT))
        if p[0] in ("aa", "ap"):  # osobna kwota dla portfela: stała (SOL) albo % kwoty tradera
            lid, amt = int(p[1]), float(p[2])
            if not s.db.follow_row(uid, lid):
                s.db.follow(uid, lid, False)
            if p[0] == "aa":
                s.db.set_follow_amount(uid, lid, sol=amt or None)
                msg = f"Kupujesz za {amt:g} SOL" if amt else "Kwota jak w ustawieniach"
            else:
                s.db.set_follow_amount(uid, lid, pct=amt)
                msg = f"Kupujesz za {amt:g}% tego, za ile kupi on"
            await _ack(update, msg)
            return await show(update, *await wallet_card(s, s.db.get_user(uid), lid))
        if p[0] == "aq":  # szybkie śledzenie wklejonego portfela
            mode, addr = p[1], p[2]
            lid = _ensure_leader(s, uid, addr)
            s.db.follow(uid, lid, mode == "copy")
            s.sol.wallet(uid)
            msg = "✅ Kopiujesz ten portfel" + ("" if u["active"] else " – włącz kopiowanie na pulpicie ▶️")
            await _ack(update, msg[:190], True)
            return await show(update, *await wallet_card(s, s.db.get_user(uid), lid, msg if mode == "copy" else None))
        if p[0] == "tk":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(token_card(s, u, p[1]), UI_TIMEOUT))
        if p[0] == "tb":
            await _ack(update, "Kupuję…")
            try:
                msg = await buy(s, u, p[2], float(p[1]))
            except Skip as e:
                msg = f"⚠️ {e}"
            return await show(update, *await token_card(s, s.db.get_user(uid), p[2], msg))
        if p[0] == "ts":
            await _ack(update, "Sprzedaję…")
            try:
                fill = await s.sol.sell_manual(u, p[2], int(p[1]))
                msg = f"✅ Sprzedano za {fill.sol:.4f} SOL{await s.sol.usd_txt(fill.sol)}"
            except Skip as e:
                msg = f"⚠️ {e}"
            return await show(update, *await token_card(s, s.db.get_user(uid), p[2], msg))
    except Exception as e:  # noqa: BLE001
        log.exception("Pulpit Solany")
        return await show(update, f"❌ {html.escape(str(e)[:200]) or 'przekroczony czas'}", KB([MAIN]))


def _ensure_leader(s, uid: int, addr: str) -> int:
    rows = s.db._q("SELECT id FROM leaders WHERE source = 'sol' AND status = 'active' AND wallet = ?", (addr,))
    if rows:
        return rows[0]["id"]
    lid = s.db.create_leader(f"Portfel {short(addr)}", "sol", uid, "active", wallet=addr)
    return lid


# ---------------- wklejanie adresu (portfel albo token) ----------------

async def on_paste(update: Update, ctx):
    s = S(ctx)
    if not getattr(s, "sol", None):
        return
    addr = valid_address(update.message.text or "")
    if not addr:
        if not (u := await require_accepted(update, ctx)):
            return
        many = wallet_lines(update.message.text or "")
        if len(many) >= 2:  # kilka adresów naraz – kopiujemy wszystkie
            return await _add_many(update, s, u["tg_id"], many)
        await update.message.reply_text("Wklej adres portfela (żeby go kopiować) albo tokena (żeby go kupić). "
                                        "Możesz wkleić kilka portfeli naraz – każdy w nowej linii. Pulpit: /menu")
        return
    if not (u := await require_accepted(update, ctx)):
        return
    info = None
    try:
        info = await asyncio.wait_for(s.sol.jup.token_full(addr), 6)
    except Exception:  # noqa: BLE001
        pass
    if info:  # to token
        text, kb = await token_card(s, u, addr)
        return await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                                        disable_web_page_preview=True)
    preview = ""
    try:
        lam = await asyncio.wait_for(s.sol.rpc.balance(addr), 6)
        preview = f"\nSaldo: {lam / LAMPORTS:.3f} SOL"
    except Exception:  # noqa: BLE001
        pass
    await update.effective_chat.send_message(
        f"👤 Portfel <code>{addr}</code>{preview}\n"
        f'<a href="https://gmgn.ai/sol/address/{addr}">Zobacz jego wyniki na GMGN</a>\n\nCo robimy?\n'
        "<i>✅ Kopiuj – bot powtarza jego zakupy i sprzedaże. 👁 – tylko dostajesz powiadomienia.</i>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=KB([[B("✅ Kopiuj jego transakcje", callback_data=f"aq:copy:{addr}")],
                         [B("👁 Tylko powiadomienia", callback_data=f"aq:watch:{addr}")], MAIN]))


# ---------------- rozmowy: dodaj portfel, kup za X SOL ----------------

async def add_start(update: Update, ctx):
    await _ack(update)
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "➕ <b>Dodaj portfel do kopiowania</b>\n\nWklej adres portfela tradera (z Axiom, GMGN, Solscan, kolscan.io…).\n"
        "Możesz dopisać nazwę po spacji, np. <code>7xKX…AsU Kowal</code>\n\n"
        "👥 <b>Kilku naraz?</b> Wklej kilka adresów – każdy w nowej linii. Bot będzie kopiował wszystkich "
        "jednocześnie.\n/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return ADD_ADDR


async def add_addr(update: Update, ctx):
    s = S(ctx)
    uid = update.effective_user.id
    many = wallet_lines(update.message.text or "")
    if len(many) >= 2:
        await _add_many(update, s, uid, many)
        return ConversationHandler.END
    parts = (update.message.text or "").split(maxsplit=1)
    addr = valid_address(parts[0]) if parts else None
    if not addr:
        await update.message.reply_text("To nie jest adres Solany. Wklej sam adres portfela (32–44 znaki).")
        return ADD_ADDR
    lid = _ensure_leader(s, uid, addr)
    if len(parts) > 1:
        s.db.update_leader(lid, name=parts[1].strip()[:40])
    s.db.follow(uid, lid, True)
    s.sol.wallet(uid)
    u = s.db.get_user(uid)
    text, kb = await wallet_card(s, u, lid, "Dodano – kopiujesz ten portfel" +
                                 ("" if u["active"] else ". Włącz kopiowanie na pulpicie ▶️"))
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
    return ConversationHandler.END


def wallet_lines(text: str) -> list[tuple[str, str | None]]:
    """Kilka adresów w jednej wiadomości: każdy w nowej linii (albo po przecinku), opcjonalnie z nazwą po spacji."""
    out, seen = [], set()
    for line in text.replace(",", "\n").splitlines():
        parts = line.strip().split(maxsplit=1)
        addr = valid_address(parts[0]) if parts else None
        if addr and addr not in seen:
            seen.add(addr)
            out.append((addr, parts[1].strip()[:40] if len(parts) > 1 else None))
    return out[:20]


async def _add_many(update: Update, s, uid: int, many: list[tuple[str, str | None]]):
    names = []
    for addr, name in many:
        lid = _ensure_leader(s, uid, addr)
        if name:
            s.db.update_leader(lid, name=name)
        s.db.follow(uid, lid, True)
        names.append(name or f"Portfel {short(addr)}")
    s.sol.wallet(uid)
    u = s.db.get_user(uid)
    notice = (f"Dodano {plural_wallets(len(many))} – kopiujesz je wszystkie naraz: {', '.join(names)}"
              + ("" if u["active"] else ". Włącz kopiowanie ▶️"))
    text, kb = await home_view(s, u, notice)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)


async def buyx_start(update: Update, ctx):
    await _ack(update)
    ctx.user_data["buyx"] = update.callback_query.data.split(":")[1]
    await update.effective_chat.send_message("Za ile SOL kupić? Wpisz liczbę, np. 0.3\n/anuluj – przerwij")
    return BUY_X


async def buyx_amount(update: Update, ctx):
    s = S(ctx)
    mint = ctx.user_data.pop("buyx", None)
    try:
        sol = float((update.message.text or "").replace(",", ".").split()[0])
        assert 0 < sol <= 1000
    except (ValueError, IndexError, AssertionError):
        await update.message.reply_text("Nieprawidłowa kwota.")
        return ConversationHandler.END
    u = s.db.get_user(update.effective_user.id)
    try:
        msg = await buy(s, u, mint, sol)
    except Skip as e:
        msg = f"⚠️ {e}"
    text, kb = await token_card(s, s.db.get_user(u["tg_id"]), mint, msg)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx):
    ctx.user_data.pop("buyx", None)
    await update.message.reply_text("Anulowano. /menu – pulpit")
    return ConversationHandler.END


# ---------------- auto take-profit / stop-loss ----------------

async def tpsl_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    users = s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id "
                    "WHERE u.approved = 1 AND (u.sol_tp > 0 OR u.sol_sl > 0)")
    for u in users:
        try:
            pf = await portfolio(s, u)
        except Exception:  # noqa: BLE001
            continue
        for pos in pf["positions"]:
            pnl = pos["pnl"]
            if pnl is None:
                continue
            hit = ("🎯 Take-profit – zysk osiągnięty!" if u["sol_tp"] and pnl >= u["sol_tp"] else
                   "🛑 Stop-loss – ucinam stratę" if u["sol_sl"] and pnl <= -u["sol_sl"] else None)
            if not hit:
                continue
            try:
                fill = await s.sol.sell_manual(u, pos["mint"], 100)
                await s.sol.notify(u["tg_id"], f"{hit}\n{pos['symbol']}: {pnl:+.1f}% → bot sprzedał wszystko za "
                                               f"{fill.sol:.4f} SOL{await s.sol.usd_txt(fill.sol)}")
            except Exception as e:  # noqa: BLE001
                log.warning("TP/SL %s %s: %s", u["tg_id"], pos["symbol"], e)


async def cmd_report(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        from . import session
        text = await session.build(S(ctx), u)
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                                 reply_markup=KB([[B("🔄 Odśwież", callback_data="ax:rep")], MAIN]))


def register(app: Application):
    """Przyciski i rozmowy pulpitu – rejestrować PRZED bot.register."""
    app.add_handler(CommandHandler("raport", cmd_report))
    text = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE
    cancel = [CommandHandler("anuluj", conv_cancel)]
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_start, pattern="^ax:add$"),
                                                      CommandHandler("portfel", add_start)],
                                        states={ADD_ADDR: [MessageHandler(text, add_addr)]},
                                        fallbacks=cancel, allow_reentry=True, conversation_timeout=300))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(buyx_start, pattern=r"^tbx:")],
                                        states={BUY_X: [MessageHandler(text, buyx_amount)]},
                                        fallbacks=cancel, allow_reentry=True, conversation_timeout=120))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(home|ax:.*|aw:\d+|a[ap]:\d+:.*|aq:.*|tk:.*|tb:.*|ts:.*)$"))


def register_paste(app: Application):
    """Wklejony adres – rejestrować NA KOŃCU (po wszystkich rozmowach), żeby nie przechwytywać ich odpowiedzi."""
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_paste))
