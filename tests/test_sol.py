"""Kopiowanie portfeli Solany (traderzy z Axiom, Pump.fun…)."""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_private import OWNER, FRIEND, B, last_text, onboard, setup, upd  # noqa: E402

from copybot import mirror, traders as T  # noqa: E402
from copybot.reports import leader_report, ranking  # noqa: E402
from copybot.sol import ui as SU  # noqa: E402
from copybot.sol.engine import SolEngine  # noqa: E402
from copybot.sol.parser import LAMPORTS, SOL_MINT, USDC_MINT, Swap, _keys, parse_swap  # noqa: E402
from copybot.sol.watcher import SolWatcher  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
WALLET = "Wa11et1111111111111111111111111111111111111"
TOKEN = "Tok3n11111111111111111111111111111111111111"


class FakeJup:
    """1 SOL = 1 000 000 tokenów (6 miejsc po przecinku)."""
    def __init__(self):
        self.calls = 0

    async def quote(self, i, o, amount, slip):
        self.calls += 1
        return {"outAmount": str(amount * 1_000_000 // LAMPORTS * 1000 if i == SOL_MINT else amount * 1000)}

    async def token(self, mint):
        return {"symbol": "MEME", "decimals": 6}


class FakeRpc:
    async def balance(self, pk):
        return 2 * LAMPORTS

    async def token_accounts(self, pk):
        return {TOKEN: (5, 6)}


def sol_setup(tmp_path):
    s, ctx, sent = setup(tmp_path)
    s.sol = SolEngine(s.cfg, s.db, s.vault, FakeRpc(), FakeJup(), s.engine.notify)
    return s, ctx, sent


def swap(side, fraction, sig="s1"):
    return Swap(sig, WALLET, side, TOKEN, 1000, 6, "SOL", 1.0, fraction, 1)


# ---------- rozpoznawanie wymian (prawdziwe transakcje z mainnetu) ----------

def test_parser_real_transactions():
    tx0 = json.load(open(FIX / "tx0.json"))
    sw = parse_swap(tx0, _keys(tx0)[0])
    assert sw.side == "sell" and sw.quote_symbol == "USDC"
    tx1 = json.load(open(FIX / "tx1.json"))
    assert parse_swap(tx1, _keys(tx1)[0]) is None


def test_parser_buy_with_sol():
    tx = {"blockTime": 1, "meta": {"err": None, "fee": 5000, "preBalances": [10 * LAMPORTS],
                                   "postBalances": [8 * LAMPORTS - 5000], "preTokenBalances": [],
                                   "postTokenBalances": [{"mint": TOKEN, "owner": WALLET,
                                                          "uiTokenAmount": {"amount": "1000", "decimals": 6}}]},
          "transaction": {"signatures": ["S"], "message": {"accountKeys": [{"pubkey": WALLET}]}}}
    sw = parse_swap(tx, WALLET)
    assert sw.side == "buy" and sw.fraction == pytest.approx(0.2)


# ---------- silnik ----------

def test_copy_demo_fixed_ratio_sell_and_alerts(tmp_path):
    s, ctx, sent = sol_setup(tmp_path)
    lid = s.db.create_leader("Axiom-wieloryb", "sol", OWNER, "active", wallet=WALLET)
    for uid, kw in ((OWNER, {"sol_buy": 0.5}), (FRIEND, {"sol_buy_mode": "ratio", "sol_mult": 2.0, "sol_max": 0})):
        s.db.ensure_user(uid, None, 1000, "USDT")
        s.db.update_user(uid, approved=1, accepted=1, active=1, **kw)
        s.db.follow(uid, lid, True)
        s.sol.wallet(uid)
    s.db.ensure_user(300, None, 1000, "USDT")
    s.db.update_user(300, approved=1, accepted=1)
    s.db.follow(300, lid, False)  # tylko powiadomienia

    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    assert res == {"filled": 2, "alert": 1}
    assert s.sol.paper(OWNER)[TOKEN] == 500_000_000  # 0,5 SOL
    assert s.sol.paper(FRIEND)[SOL_MINT] == 5 * LAMPORTS - LAMPORTS - 200_000  # 10% × 2 z 5 SOL
    assert any(uid == OWNER and "⚡" in t and "ms od wykrycia" in t for uid, t in sent)
    assert any(uid == 300 and "👁 Axiom-wieloryb kupił" in t for uid, t in sent)
    assert s.sol.history(OWNER)[0]["latency_ms"] is not None

    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 0.5, "s2"), time.monotonic()))
    assert s.sol.paper(OWNER)[TOKEN] == 250_000_000
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "s3"), time.monotonic()))
    assert TOKEN not in s.sol.paper(OWNER)


def test_reference_report_for_ranking(tmp_path):
    s, ctx, _ = sol_setup(tmp_path)
    lid = s.db.create_leader("Sol", "sol", OWNER, "active", wallet=WALLET)
    asyncio.run(s.sol._reference(s.db.get_leader(lid), swap("buy", 0.5)))
    asyncio.run(s.sol._reference(s.db.get_leader(lid), swap("sell", 1.0, "s2")))
    rep = asyncio.run(leader_report(s, lid, max_age=0))
    assert rep["unit"] == "SOL" and rep["closed"] == 1
    rows = asyncio.run(ranking(s))
    assert rows and rows[0][0]["id"] == lid


def test_mirror_ignores_solana_traders(tmp_path):
    s, ctx, _ = sol_setup(tmp_path)
    onboard(s, ctx, OWNER)
    lid = s.db.create_leader("Sol", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    assert asyncio.run(mirror.target_weights(s, s.db.get_user(OWNER))) == {}


# ---------- interfejs ----------

def test_add_solana_trader_flow(tmp_path):
    s, ctx, _ = sol_setup(tmp_path)
    onboard(s, ctx, OWNER)

    async def flow():
        await T.add_start(upd(OWNER, data="add"), ctx)
        await T.add_name(upd(OWNER, text="Axiom Kowal"), ctx)
        assert await T.add_source(upd(OWNER, data="src:sol"), ctx) == T.A_WALLET
        assert await T.add_wallet(upd(OWNER, text="to nie adres"), ctx) == T.A_WALLET
        await T.add_wallet(upd(OWNER, text=f" {WALLET} "), ctx)

    asyncio.run(flow())
    lead = s.db.leaders("active")[0]
    assert lead["source"] == "sol" and lead["wallet"] == WALLET
    assert s.sol.watched_wallets() == {WALLET}
    u = upd(OWNER, data=f"cp:{lead['id']}")
    asyncio.run(T.on_trader_button(u, ctx))
    assert s.sol.has_wallet(OWNER)  # portfel Solany (demo) utworzony przy kopiowaniu
    u = upd(OWNER, data=f"tr:{lead['id']}")
    asyncio.run(T.on_trader_button(u, ctx))
    assert "solscan.io/account/" + WALLET in last_text(u) and "SOL" in last_text(u)


def test_solana_screen_settings_and_sell(tmp_path):
    s, ctx, _ = sol_setup(tmp_path)
    onboard(s, ctx, OWNER)
    s.sol.wallet(OWNER)
    s.sol.paper_apply(OWNER, {TOKEN: 1_000_000_000})
    u = upd(OWNER, data="sx")
    asyncio.run(SU.on_sol_button(u, ctx))
    text = last_text(u)
    assert "Solana" in text and "5.0000 SOL" in text and "MEME" in text
    asyncio.run(SU.on_sol_button(upd(OWNER, data="sx:amt:0.25"), ctx))
    asyncio.run(SU.on_sol_button(upd(OWNER, data="sx:slip:2000"), ctx))
    usr = s.db.get_user(OWNER)
    assert usr["sol_buy"] == 0.25 and usr["sol_slippage_bps"] == 2000
    u = upd(OWNER, data=f"sx:sell:100:{TOKEN}")
    asyncio.run(SU.on_sol_button(u, ctx))
    assert "Sprzedano" in last_text(u) and TOKEN not in s.sol.paper(OWNER)
    asyncio.run(SU.on_sol_button(upd(OWNER, data="sx:modeok"), ctx))
    assert s.db.get_user(OWNER)["sol_live"] == 1
    u = upd(OWNER, data="menu")
    asyncio.run(B.on_button(u, ctx))
    assert "Solana:</b> 💰 LIVE" in last_text(u)


# ---------- nasłuch: szybka ścieżka Helius i powrót do zapasowej ----------

class FakeWS:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self, timeout=None):
        if self.replies:
            return SimpleNamespace(type=__import__("aiohttp").WSMsgType.TEXT, data=json.dumps(self.replies.pop(0)))
        await asyncio.sleep(timeout or 0.1)
        raise asyncio.TimeoutError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_enhanced_websocket_full_tx_and_fallback(tmp_path):
    tx0 = json.load(open(FIX / "tx0.json"))
    wallet = _keys(tx0)[0]
    got = []

    async def on_swap(w, sw, t):
        got.append((w, sw.side, (time.monotonic() - t) * 1000))

    ws_ok = FakeWS([{"id": 1, "result": 77},
                    {"method": "transactionNotification",
                     "params": {"subscription": 77, "result": {"signature": "SIG", "transaction": tx0}}}])
    session = MagicMock()
    session.ws_connect = MagicMock(return_value=ws_ok)
    w = SolWatcher("wss://x", "wss://atlas", None, session, lambda: {wallet}, on_swap)

    async def run_ok():
        task = asyncio.create_task(w._session("wss://atlas", enhanced=True))
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(run_ok())
    assert ws_ok.sent[0]["method"] == "transactionSubscribe"
    assert ws_ok.sent[0]["params"][1]["commitment"] == "processed"
    assert got and got[0][1] == "sell" and got[0][2] < 100  # bez dodatkowego getTransaction

    ws_err = FakeWS([{"id": 1, "error": {"code": -32601, "message": "Method not found"}}])
    session.ws_connect = MagicMock(return_value=ws_err)
    w2 = SolWatcher("wss://x", "wss://atlas", None, session, lambda: {wallet}, on_swap)
    with pytest.raises(ConnectionError):
        asyncio.run(w2._session("wss://atlas", enhanced=True))
    assert w2.enhanced_failed  # kolejne połączenie pójdzie zapasową ścieżką logsSubscribe


def test_parser_transaction_version_1():
    """Nowy format transakcji Solany (wersja 1, trasy Jupitera); podpisuje pośrednik, tokeny ma inny portfel."""
    tx = json.load(open(FIX / "tx_v1.json"))
    assert tx["version"] == 1
    assert parse_swap(tx, _keys(tx)[0]) is None  # pośrednik nic nie kupił
    sw = parse_swap(tx, "GTXo3RCDkn35dpwPiCVz32XjvCATYe2hvZnYhwXQgVAG")
    assert sw.side == "buy" and sw.quote_symbol == "USDC" and sw.quote_amount == pytest.approx(19.339, abs=0.01)


def test_enhanced_forbidden_falls_back_immediately():
    import aiohttp
    calls = []

    class Sess:
        def ws_connect(self, url, **kw):
            calls.append(url)
            if "atlas" in url:
                raise aiohttp.WSServerHandshakeError(request_info=MagicMock(real_url="wss://atlas"), history=(),
                                                     status=403, message="Forbidden")
            return FakeWS([])

    w = SolWatcher("wss://std", "wss://atlas", None, Sess(), lambda: {WALLET}, AsyncMock())

    async def go():
        t = asyncio.create_task(w.run())
        await asyncio.sleep(0.5)
        t.cancel()
    asyncio.run(go())
    assert w.enhanced_failed and w.mode == "logs" and calls[0] == "wss://atlas" and "wss://std" in calls


def test_log_prefilter_skips_non_swaps():
    from copybot.sol.watcher import looks_like_swap
    assert looks_like_swap(["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [2]", "Program log: x"])
    assert looks_like_swap(["Program Abc invoke [1]", "Program log: Instruction: Swap"])
    assert not looks_like_swap(["Program 11111111111111111111111111111111 invoke [1]",
                                "Program ComputeBudget111111111111111111111111111111 invoke [1]"])
    assert looks_like_swap(None)


def test_same_quote_shared_between_followers():
    from copybot.sol.chain import Jupiter
    calls = []

    class J(Jupiter):
        async def _quote(self, *a):
            calls.append(a)
            await asyncio.sleep(0.05)
            return {"outAmount": "123"}

    j = J("https://x", "", None)

    async def go():
        return await asyncio.gather(*(j.quote(SOL_MINT, TOKEN, 50_000_000, 1000) for _ in range(3)),
                                    j.quote(SOL_MINT, TOKEN, 70_000_000, 1000))
    res = asyncio.run(go())
    assert len(calls) == 2 and res[0]["outAmount"] == "123"


def test_stale_copy_skipped_and_ratio_message(tmp_path):
    s, ctx, sent = sol_setup(tmp_path)
    lid = s.db.create_leader("Sol", "sol", OWNER, "active", wallet=WALLET)
    s.db.ensure_user(OWNER, None, 1000, "USDT")
    s.db.update_user(OWNER, approved=1, accepted=1, active=1)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic() - 30))  # 30 s spóźnienia
    assert res["skipped"] == 1 and any("za późno" in t for _, t in sent)
    s.db.update_user(OWNER, sol_buy_mode="ratio")
    sent.clear()
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.0001, "s9"), time.monotonic()))
    assert any("poniżej minimum" in t and "stałą kwotę" in t for _, t in sent)
