"""Mini aplikacja (Telegram Mini App): API Solany, bezpieczniki, zwrot wkładu, rangi i podgląd admina."""
import asyncio
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent))
from test_private import FRIEND, OWNER, onboard, upd  # noqa: E402
from test_sol import TOKEN, WALLET, sol_setup, swap  # noqa: E402
from copybot.sol.parser import SOL_MINT  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from copybot.sol import admin as ADM  # noqa: E402
from copybot.sol import axiom as AX  # noqa: E402
from copybot.web import build_app  # noqa: E402

W2 = "Wa11et2222222222222222222222222222222222222"


def init_data(uid: int, token: str = "1:X") -> str:
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "X"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def ready(tmp_path):
    s, ctx, sent = sol_setup(tmp_path)
    onboard(s, ctx, OWNER)
    onboard(s, ctx, FRIEND)
    s.db.update_user(FRIEND, approved=1, accepted=1)
    return s, ctx, sent


def run(s, steps):
    """Uruchamia serwer panelu i podaje funkcji `steps` klienta (get/post jako dany użytkownik)."""
    async def go():
        client = TestClient(TestServer(build_app(s)))
        await client.start_server()

        async def call(uid, path, body=None):
            h = {"X-Init-Data": init_data(uid)} if uid else {}
            r = await (client.post(path, json=body, headers=h) if body is not None else client.get(path, headers=h))
            return r.status, await r.json()
        try:
            await steps(call)
        finally:
            await client.close()
    asyncio.run(go())


def test_app_page_and_auth(tmp_path):
    s, _, _ = ready(tmp_path)

    async def steps(call):
        st, j = await call(None, "/api/sol/state")
        assert st == 401
        st, j = await call(OWNER, "/api/sol/state")
        assert st == 200 and j["user"]["admin"] and j["total"] == 5.0 and j["help"] and not j["active"]
    run(s, steps)


def test_app_full_flow(tmp_path):
    s, _, sent = ready(tmp_path)

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "toggle"})
        assert st == 400 and "Najpierw dodaj" in j["error"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "add_wallets", "text": f"{WALLET} Kowal\n{W2} Wieloryb"})
        assert st == 200 and len(j["ids"]) == 2
        lid = j["ids"][0]
        st, j = await call(OWNER, "/api/sol/action", {"action": "amount", "lid": lid, "pct": 25})
        assert st == 200 and s.db.follow_row(OWNER, lid)["sol_pct"] == 25
        st, j = await call(OWNER, "/api/sol/action", {"action": "settings", "field": "sol_live", "value": 1})
        assert st == 400  # spoza listy – nie da się przełączyć na prawdziwe pieniądze bokiem
        st, j = await call(OWNER, "/api/sol/action", {"action": "settings", "field": "sol_slippage_bps", "value": 1})
        assert st == 400
        st, j = await call(OWNER, "/api/sol/action", {"action": "mode", "live": True})
        assert st == 400 and not s.db.get_user(OWNER)["sol_live"]  # bez potwierdzenia
        st, j = await call(OWNER, "/api/sol/action", {"action": "toggle"})
        assert st == 200 and s.db.get_user(OWNER)["active"] == 1
        await s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic())  # trader kupił za 1 SOL → my za 25%
        assert s.sol.paper(OWNER)[TOKEN] == 250_000_000
        st, j = await call(OWNER, "/api/sol/state")
        assert j["active"] and len(j["wallets"]) == 2 and j["feed"][0]["symbol"] == "MEME" and len(j["positions"]) == 1
        assert j["wallets"][0]["own"]["pct"] == 25
        st, j = await call(OWNER, f"/api/sol/wallet/{lid}")
        assert st == 200 and j["status"] == "copy" and j["mine"]["buy"]["n"] == 1 and j["stats"]
        st, j = await call(OWNER, f"/api/sol/token/{TOKEN}")
        assert st == 200 and j["risk"]["level"].startswith("🟢") and j["mine"]["tokens"] == 250
        st, j = await call(OWNER, "/api/sol/action", {"action": "sell", "mint": TOKEN, "pct": 100})
        assert st == 200 and "Sprzedano" in j["msg"] and TOKEN not in s.sol.paper(OWNER)
        st, j = await call(OWNER, "/api/sol/action", {"action": "buy", "mint": TOKEN, "sol": 0.1})
        assert st == 200 and "Kupiono" in j["msg"]
        st, j = await call(OWNER, "/api/sol/trending")
        assert j["items"][0]["symbol"] == "MEME"
        st, j = await call(OWNER, "/api/sol/report")
        assert "Raport sesji" in j["html"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "follow", "lid": lid, "mode": "remove"})
        assert st == 200 and lid not in s.db.follows_of(OWNER)
    run(s, steps)


def test_safety_filter_and_day_limit(tmp_path):
    s, _, sent = ready(tmp_path)
    lid = s.db.create_leader("K", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1, sol_safe=1)
    good = s.sol.jup.token_full

    async def risky(mint):
        d = await good(mint)
        d["audit"] = {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": False, "topHoldersPercentage": 70}
        return d
    s.sol.jup.token_full = risky
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    assert res["skipped"] == 1 and any("🛡 pułapka" in t and "zamrozić" in t for _, t in sent)
    s.sol.jup.token_full = good
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "s2"), time.monotonic()))
    assert TOKEN in s.sol.paper(OWNER)

    # dzienny limit straty: sprzedaż ze stratą 50% → następne zakupy pominięte
    s.sol.record(OWNER, None, None, "buy", "Other1111111111111111111111111111111111111", 0.1, 100, "filled", "ręcznie",
                 True)
    s.sol.record(OWNER, None, None, "sell", "Other1111111111111111111111111111111111111", 0.05, 100, "filled",
                 "ręcznie", True)
    assert abs(s.sol.day_pnl(OWNER, True) + 0.05) < 1e-9
    s.db.update_user(OWNER, sol_day_limit=0.02)
    sent.clear()
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "s4"), time.monotonic()))
    assert res["skipped"] == 1 and any("dzienny limit straty" in t for _, t in sent)


def test_x2_sells_half_once(tmp_path):
    s, ctx, sent = ready(tmp_path)
    s.sol.wallet(OWNER)
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.5))
    s.db.update_user(OWNER, sol_x2=1)
    s.sol.jup.token_usd = 0.25  # ×2,5
    jctx = SimpleNamespace(application=ctx.application, bot=AsyncMock())
    asyncio.run(AX.tpsl_job(jctx))
    assert s.sol.paper(OWNER)[TOKEN] == 250_000_000 and any("ZWROT WKŁADU" in t for _, t in sent)
    asyncio.run(AX.tpsl_job(jctx))
    assert s.sol.paper(OWNER)[TOKEN] == 250_000_000  # drugi raz nie sprzedaje


def test_admin_overview_and_roles(tmp_path):
    s, ctx, sent = ready(tmp_path)
    s.sol.wallet(FRIEND)
    lid = s.db.create_leader("K", "sol", FRIEND, "active", wallet=WALLET)
    s.db.follow(FRIEND, lid, True)
    s.db.update_user(FRIEND, active=1)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))

    async def steps(call):
        st, j = await call(FRIEND, "/api/sol/admin/users")
        assert st == 400  # zwykły użytkownik nie widzi innych
        st, j = await call(OWNER, "/api/sol/admin/users")
        friend = next(x for x in j["users"] if x["id"] == FRIEND)
        assert j["owner"] and friend["active"] and friend["trades24"] == 1 and friend["positions"] == 1
        st, j = await call(OWNER, f"/api/sol/admin/user/{FRIEND}")
        assert j["history"][0]["symbol"] == "MEME" and j["positions_list"][0]["symbol"] == "MEME"
        st, j = await call(OWNER, "/api/sol/action", {"action": "admin_role", "uid": FRIEND})
        assert st == 200 and s.db.get_user(FRIEND)["role"] == "admin"
        st, j = await call(FRIEND, "/api/sol/admin/users")
        assert st == 200  # teraz jest adminem
        st, j = await call(FRIEND, "/api/sol/action", {"action": "admin_role", "uid": OWNER})
        assert st == 400  # admin nie nadaje rang – tylko właściciel
        st, j = await call(FRIEND, "/api/sol/action", {"action": "admin_stop", "uid": FRIEND})
        assert st == 200 and not s.db.get_user(FRIEND)["active"]
    run(s, steps)

    # w bocie: lista i podgląd
    u = upd(OWNER, data="adm:users")
    asyncio.run(ADM.on_button(u, ctx))
    assert "Kto handluje" in u.callback_query.edit_message_text.call_args.args[0]
    u = upd(OWNER, data=f"adm:u:{FRIEND}")
    asyncio.run(ADM.on_button(u, ctx))
    text = u.callback_query.edit_message_text.call_args.args[0]
    assert "⭐ admin" in text and "MEME" in text
    u = upd(FRIEND + 1, data="adm:users")
    asyncio.run(ADM.on_button(u, ctx))
    u.callback_query.answer.assert_called()


AXIOM = json.dumps([
    {"trackedWalletAddress": WALLET, "name": "mils", "emoji": "👻", "alertsOnToast": True, "groups": ["Main"]},
    {"trackedWalletAddress": W2, "name": "Cupsey", "emoji": "🐐", "alertsOnToast": False, "groups": ["Main"]},
])


def test_axiom_import_switch_and_priority(tmp_path):
    from copybot.sol.importer import parse_wallets
    # ucięty eksport (za długie wklejenie) – bierzemy to, co się da
    cut = AXIOM[:AXIOM.index("Cupsey") + 12]
    assert [w for w, *_ in parse_wallets(cut)] == [WALLET, W2] and parse_wallets(cut)[0][3] is True
    s, _, sent = ready(tmp_path)

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "import", "text": AXIOM})
        assert st == 200 and j["count"] == 2
        st, j = await call(OWNER, "/api/sol/saved")
        assert [i["name"] for i in j["items"]] == ["mils", "Cupsey"] and j["items"][0]["priority"]
        assert all(i["status"] is None for i in j["items"])  # import niczego nie włącza
        assert s.sol.watched_wallets() == set()  # i nie nasłuchuje (oszczędza limit RPC)
        st, j = await call(OWNER, "/api/sol/action", {"action": "switch", "wallet": W2, "on": True})
        assert st == 200 and s.sol.watched_wallets() == {W2}
        lead = next(ld for ld in s.db.leaders("active") if ld["wallet"] == W2)
        assert lead["name"] == "🐐 Cupsey"
        st, j = await call(OWNER, "/api/sol/action", {"action": "priority", "wallet": W2, "on": True})
        st, j = await call(OWNER, "/api/sol/saved")
        assert j["items"][0]["status"] == "copy" and j["items"][0]["priority"]  # ⭐ + włączony = na samej górze
        st, j = await call(OWNER, "/api/sol/state")
        assert j["wallets"][0]["priority"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "switch", "wallet": W2, "on": False})
        st, j = await call(OWNER, "/api/sol/saved")
        assert len(j["items"]) == 2 and all(i["status"] is None for i in j["items"])  # wyłączony zostaje na liście
        # ręcznie dodany i wyłączony też nie znika
        st, j = await call(OWNER, "/api/sol/action", {"action": "add_wallets", "text": "Wa11et3333333333333333333333333333333333333 Nowy"})
        st, j = await call(OWNER, "/api/sol/action", {"action": "switch", "wallet": "Wa11et3333333333333333333333333333333333333", "on": False})
        st, j = await call(OWNER, "/api/sol/saved")
        assert any(i["name"] == "Nowy" and i["status"] is None for i in j["items"])
    run(s, steps)


def test_copy_limit(tmp_path):
    from copybot.sol import api
    s, _, _ = ready(tmp_path)
    s.db.save_wallets(OWNER, [(f"Wa11et{i:037d}", None, None) for i in range(3)], "t")
    api.MAX_COPIED, old = 2, api.MAX_COPIED
    try:
        for it in api.saved_list(s, OWNER)[:2]:
            api.switch(s, OWNER, it["wallet"], True)
        try:
            api.switch(s, OWNER, api.saved_list(s, OWNER)[2]["wallet"], True)
            raise AssertionError("limit nie zadziałał")
        except api.ApiError as e:
            assert "maks. 2" in str(e)
    finally:
        api.MAX_COPIED = old


def test_bot_import_file_and_toggle(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, text=AXIOM)
    asyncio.run(AX.on_paste(u, ctx))
    assert "Zapisano 2 portfeli" in u.effective_chat.send_message.call_args.args[0]
    u = upd(OWNER, data="sv:p:0")
    asyncio.run(AX.on_saved_button(u, ctx))
    text = u.callback_query.edit_message_text.call_args.args[0]
    assert "⭐👻 mils" in text and "Cupsey" in text
    asyncio.run(AX.on_saved_button(upd(OWNER, data=f"sv:t:{WALLET}"), ctx))
    assert s.sol.watched_wallets() == {WALLET}
    asyncio.run(AX.on_saved_button(upd(OWNER, data=f"sv:s:{W2}"), ctx))
    from copybot.sol.api import saved_list
    assert [i["wallet"] for i in saved_list(s, OWNER) if i["priority"]] == [WALLET, W2]


def test_admin_pause_stops_solana_copies(tmp_path):
    s, _, _ = ready(tmp_path)
    lid = s.db.create_leader("K", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1)

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "admin_pause", "on": True})
        assert st == 200
        res = await s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic())
        assert not res.get("filled") and TOKEN not in s.sol.paper(OWNER)
        st, j = await call(OWNER, "/api/sol/action", {"action": "admin_pause", "on": False})
        await s.sol.on_swap(WALLET, swap("buy", 0.1, "s2"), time.monotonic())
        assert TOKEN in s.sol.paper(OWNER)
        st, j = await call(OWNER, "/api/sol/admin/users")
        assert j["totals"]["trades24"] == 1 and j["feed"][0]["symbol"] == "MEME" and j["leaders"][0]["copiers"] == 1
        assert "watcher" in j and j["totals"]["copying"] == 1
        st, j = await call(OWNER, "/api/sol/state")
        assert "watcher" in j and j["last_leader_move"]
    run(s, steps)


def test_reference_uses_trader_price_without_jupiter(tmp_path):
    """Portfel wzorcowy liczy po cenie tradera – nie zużywa limitu Jupitera."""
    s, _, _ = ready(tmp_path)
    lid = s.db.create_leader("K", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, False)
    before = s.sol.jup.calls
    asyncio.run(s.sol._reference(s.db.get_leader(lid), swap("buy", 0.1)))  # trader: 1 SOL → 1000 jednostek
    from copybot.db import ref_id
    bal = s.sol.paper(ref_id(lid))
    assert s.sol.jup.calls == before and bal[TOKEN] == 500  # 10% z 5 SOL = 0,5 SOL → 500 jednostek
    asyncio.run(s.sol._reference(s.db.get_leader(lid), swap("sell", 1.0, "s2")))
    assert TOKEN not in s.sol.paper(ref_id(lid)) and s.sol.jup.calls == before


def test_notify_retries_network_errors():
    from telegram.error import NetworkError
    from copybot.bot import make_notify
    bot = AsyncMock()
    bot.send_message.side_effect = [NetworkError("x"), None]
    app = SimpleNamespace(bot=bot, bot_data={})

    async def go():
        import copybot.bot as BM
        real = BM.asyncio.sleep
        BM.asyncio.sleep = AsyncMock()
        try:
            await (await make_notify(app))(1, "hej")
        finally:
            BM.asyncio.sleep = real
    asyncio.run(go())
    assert bot.send_message.call_count == 2


def _two_leaders(s):
    a = s.db.create_leader("A", "sol", OWNER, "active", wallet=WALLET)
    b = s.db.create_leader("B", "sol", OWNER, "active", wallet=W2)
    for lid in (a, b):
        s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1)
    return a, b


def test_sell_only_tokens_bought_for_that_trader(tmp_path):
    """Dwóch traderów kupiło ten sam token – sprzedaż jednego nie zrzuca pozycji kupionej za drugim."""
    s, _, _ = ready(tmp_path)
    _two_leaders(s)
    from dataclasses import replace
    sw_b = replace(swap("buy", 0.1, "b1"), wallet=W2)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "a1"), time.monotonic()))
    asyncio.run(s.sol.on_swap(W2, sw_b, time.monotonic()))
    assert s.sol.paper(OWNER)[TOKEN] == 100_000_000  # 2 × 0,05 SOL
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "a2"), time.monotonic()))  # A sprzedaje wszystko
    assert s.sol.paper(OWNER)[TOKEN] == 50_000_000  # część kupiona za B zostaje
    # ręczny zakup też nie jest sprzedawany przez tradera
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.05))
    sw_b2 = replace(swap("sell", 1.0, "b2"), wallet=W2)
    asyncio.run(s.sol.on_swap(W2, sw_b2, time.monotonic()))
    assert s.sol.paper(OWNER)[TOKEN] == 50_000_000


def test_filters_no_add_small_buys_and_flippers(tmp_path):
    s, _, sent = ready(tmp_path)
    a, _ = _two_leaders(s)
    s.db.update_user(OWNER, sol_no_add=1)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "x1"), time.monotonic()))
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "x2"), time.monotonic()))
    assert res["skipped"] == 1 and any("nie dokupuję" in t for _, t in sent)
    s.db.update_user(OWNER, sol_no_add=0, sol_min_lead=2)  # trader kupuje za 1 SOL < 2 SOL
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "x3"), time.monotonic()))
    assert res["skipped"] == 1 and any("mały zakup tradera" in t for _, t in sent)
    # flipper: 5 pozycji zamkniętych po 3 s w portfelu wzorcowym
    from copybot.db import ref_id
    for i in range(5):
        s.db._q("INSERT INTO sol_trades (tg_id, leader_id, side, mint, sol, tokens, status, paper, ts) VALUES "
                "(?, ?, 'buy', ?, 1, 100, 'filled', 1, ?)", (ref_id(a), a, f"M{i}", 1000 * i * 10))
        s.db._q("INSERT INTO sol_trades (tg_id, leader_id, side, mint, sol, tokens, status, paper, ts) VALUES "
                "(?, ?, 'sell', ?, 1, 100, 'filled', 1, ?)", (ref_id(a), a, f"M{i}", 1000 * i * 10 + 3000))
    assert s.sol.hold_stats(a)["median_s"] == 3 and s.sol.hold_stats(a)["n"] == 5
    s.db.update_user(OWNER, sol_min_lead=0, sol_min_hold=10)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "x4"), time.monotonic()))
    assert res["skipped"] == 1 and any("flipper" in t for _, t in sent)


def test_demo_fallback_at_trader_price_when_no_route(tmp_path):
    from copybot.sol.chain import ChainError
    s, _, sent = ready(tmp_path)
    _two_leaders(s)

    async def no_route(*a, **kw):
        raise ChainError("Jupiter: Could not find any route")
    s.sol.jup.quote = no_route
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    # trader: 1 SOL za 1000 jednostek → 0,05 SOL = 50 jednostek, −2% = 49
    assert res["filled"] == 1 and s.sol.paper(OWNER)[TOKEN] == 49
    assert "po cenie tradera" in s.sol.history(OWNER)[0]["info"]
    s.db.update_user(OWNER, sol_live=1)  # na prawdziwych SOL bez trasy – pomijamy
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "s2"), time.monotonic()))
    assert res.get("skipped") == 1 or res.get("error") == 1


def test_session_report_counts_tokens_bought_before_session(tmp_path):
    from copybot.sol import session
    s, ctx, _ = ready(tmp_path)
    _two_leaders(s)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "p1"), time.monotonic()))  # przed sesją
    asyncio.run(session.start(s, s.db.get_user(OWNER)))
    s.sol.jup.token_usd = 0.2
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "p2"), time.monotonic()))
    rep = asyncio.run(session.build(s, s.db.get_user(OWNER)))
    assert "Sprzedane tokeny: 1" in rep and "z zyskiem 1" in rep


def test_import_axiom_wallet_and_restore(tmp_path):
    from solders.keypair import Keypair
    from copybot.sol import ui as SU
    s, ctx, _ = ready(tmp_path)
    s.sol.wallet(OWNER)
    bot_pub = s.sol.wallet(OWNER)[0]
    kp = Keypair()
    # formaty: base58 (Axiom/Phantom) i lista 64 liczb (solana-keygen)
    assert str(s.sol.parse_secret(str(kp)).pubkey()) == str(kp.pubkey())
    assert str(s.sol.parse_secret(json.dumps(list(bytes(kp)))).pubkey()) == str(kp.pubkey())

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "import_wallet", "secret": "zły klucz"})
        assert st == 400 and "klucz prywatny" in j["error"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "import_wallet", "secret": str(kp)})
        assert st == 200 and j["confirm_needed"] and j["address"] == str(kp.pubkey())
        assert s.sol.wallet(OWNER)[0] == bot_pub  # bez potwierdzenia nic się nie zmienia
        st, j = await call(OWNER, "/api/sol/action", {"action": "import_wallet", "secret": str(kp),
                                                      "address": str(kp.pubkey()), "confirm": True})
        assert st == 200 and s.sol.wallet(OWNER)[0] == str(kp.pubkey())
        st, j = await call(OWNER, "/api/sol/deposit")
        assert j["source"] == "axiom" and j["previous"]["pubkey"] == bot_pub
        st, j = await call(OWNER, "/api/sol/action", {"action": "restore_wallet"})
        assert st == 200 and s.sol.wallet(OWNER)[0] == bot_pub
    run(s, steps)

    # w bocie: wiadomość z kluczem jest usuwana, potem potwierdzenie
    async def chat():
        assert await SU.imp_start(upd(OWNER, data="sx:imp"), ctx) == SU.IMP
        u = upd(OWNER, text=str(kp))
        await SU.imp_text(u, ctx)
        u.message.delete.assert_called()
        await SU.on_sol_button(upd(OWNER, data="sx:impok"), ctx)
    asyncio.run(chat())
    assert s.sol.wallet(OWNER)[0] == str(kp.pubkey())


def test_live_no_route_uses_pumpportal(tmp_path):
    from copybot.sol.chain import ChainError
    from copybot.sol.engine import Fill
    s, _, _ = ready(tmp_path)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, sol_live=1, sol_priority=300_000)

    async def no_route(*a, **kw):
        raise ChainError("Jupiter: Could not find any route")
    s.sol.jup.quote = no_route
    calls = {}

    async def fake_pump(u, side, mint, amount_raw, t0, t_recv, decimals):
        calls["args"] = (side, amount_raw, s.sol.priority(u))
        return Fill(amount_raw / 1e9, 1.0, "sig", 5, 1.0, note="przez Pump.fun")
    s.sol.pump = object()
    s.sol._live_pumpportal = fake_pump
    fill = asyncio.run(s.sol.execute(s.db.get_user(OWNER), "buy", TOKEN, 50_000_000, decimals=6))
    assert fill.note == "przez Pump.fun" and calls["args"] == ("buy", 50_000_000, 300_000)


def test_demo_start_real_costs_and_auto_settings(tmp_path):
    from copybot.sol.api import recommended
    from copybot.sol.engine import ATA_RENT
    s, _, _ = ready(tmp_path)
    s.sol.wallet(OWNER)

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "settings", "field": "sol_paper_start", "value": 0.6})
        st, j = await call(OWNER, "/api/sol/action", {"action": "reset_demo"})
        assert st == 200 and s.sol.paper(OWNER)[SOL_MINT] == 600_000_000
        st, j = await call(OWNER, "/api/sol/state")
        assert j["demo_start"] == 0.6 and abs(j["result"]) < 1e-9
        st, j = await call(OWNER, "/api/sol/action", {"action": "auto_settings"})
        u = s.db.get_user(OWNER)
        assert st == 200 and u["sol_buy_mode"] == "pct" and u["sol_pct"] == 4.7 and u["sol_max"] == 0.093
        assert u["sol_min_buy"] == 0.016 and u["sol_max_pos"] == 4 and u["sol_streak"] == 5 and u["sol_trail"] == 40
        assert u["sol_autocfg"] == 1 and j["text"].startswith("Saldo 0.600 SOL")
    run(s, steps)
    # depozyt za konto tokena: pobrany przy pierwszym zakupie, wraca po sprzedaży całości
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.047))
    after_buy = s.sol.paper(OWNER)[SOL_MINT]
    assert after_buy == 600_000_000 - 47_000_000 - s.sol.paper_fee(s.db.get_user(OWNER)) - ATA_RENT
    asyncio.run(s.sol.sell_manual(s.db.get_user(OWNER), TOKEN, 100))
    assert s.sol.paper(OWNER)[SOL_MINT] > after_buy + 47_000_000  # depozyt wrócił
    r = recommended(0.6, typical_buy=2.0, live=True)
    assert r["sol_pct"] == 1.8 and r["sol_day_limit"] == 0.15 and r["sol_priority"] == 200_000 and r["sol_turbo"] == 1
    assert recommended(50, 2.0)["sol_max_pos"] == 15 and recommended(0.15)["sol_min_buy"] <= 0.15 * 0.06


def test_research_scoring_and_list_tools(tmp_path):
    from copybot.sol import api
    from copybot.sol.parser import Swap
    from copybot.sol.research import label, summarize
    now = time.time()

    def sw(side, mint, sol, raw, frac=1.0):
        return Swap("x", WALLET, side, mint, raw, 6, "SOL", sol, frac, 0)
    good = summarize(WALLET, [(now - 900, sw("buy", "A", 1, 100)), (now - 600, sw("sell", "A", 1.5, 100)),
                              (now - 500, sw("buy", "B", 1, 100)), (now - 200, sw("sell", "B", 1.2, 100)),
                              (now - 150, sw("buy", "C", 1, 100)), (now - 100, sw("sell", "C", 0.9, 100))], 3)
    assert good["closed"] == 3 and abs(good["pnl"] - 0.6) < 1e-9 and label(good) == "🏆 dobry"
    flip = summarize(W2, [(now - 10, sw("buy", "A", 1, 100)), (now - 5, sw("sell", "A", 1.1, 100))], 3)
    assert label(flip) == "⚡ flipper" and flip["score"] < good["score"]
    s, _, _ = ready(tmp_path)
    s.db.save_wallets(OWNER, [(WALLET, "Dobry", None), (W2, "Flip", None),
                              ("Wa11et3333333333333333333333333333333333333", "Śpioch", None)], "t")
    s.db.set_research(WALLET, good)
    s.db.set_research(W2, flip)
    s.db.set_research("Wa11et3333333333333333333333333333333333333", summarize("x", [], 3))
    items = api.saved_list(s, OWNER)
    assert items[0]["name"] == "Dobry" and items[0]["label"] == "🏆 dobry"
    assert api.enable_best(s, OWNER) == ["Dobry"] and s.sol.watched_wallets() == {WALLET}
    assert api.remove_inactive(s, OWNER) == 1  # śpioch usunięty, flipper (aktywny) zostaje
    assert [i["name"] for i in api.saved_list(s, OWNER)] == ["Dobry", "Flip"]


def test_clear_notifications_with_pnl_buttons_and_preference(tmp_path):
    s, ctx, _ = ready(tmp_path)
    got = []

    async def rich_notify(uid, text, html=False, buttons=None):
        got.append((uid, text, html, buttons))
    s.sol.notify = rich_notify
    lid = s.db.create_leader("Kowal", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    uid, text, is_html, buttons = got[-1]
    assert is_html and "🟢 <b>KUPNO · MEME</b>" in text and "👤 Kowal kupił za 1 SOL" in text
    assert buttons[0][0] == ("📊 Token", f"tkn:{TOKEN}") and buttons[0][1][1] == f"tsn:100:{TOKEN}"
    s.sol.jup.token_usd = 0.2  # cena ×2
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "s2"), time.monotonic()))
    text = got[-1][1]
    assert "🔴 <b>SPRZEDAŻ · MEME</b>" in text and "💰 <b>Zysk +" in text
    assert s.sol.history(OWNER)[0]["pnl"] > 0  # wynik zapisany w historii
    # tylko wyniki: kupna nie przychodzą, sprzedaże tak
    s.db.update_user(OWNER, sol_notify="sells")
    n = len(got)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "b3"), time.monotonic()))
    assert len(got) == n
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "s4"), time.monotonic()))
    assert len(got) == n + 1 and "SPRZEDAŻ" in got[-1][1]

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/state")
        assert j["today"]["buys"] == 2 and j["today"]["sells"] == 2 and j["today"]["wins"] >= 1
        assert any(f["pnl"] is not None for f in j["feed"])
        st, j = await call(OWNER, "/api/sol/action", {"action": "settings", "field": "sol_notify", "value": "zle"})
        assert st == 400
    run(s, steps)
    # przycisk 📊 Token pod powiadomieniem wysyła NOWĄ wiadomość (powiadomienie zostaje)
    u = upd(OWNER, data=f"tkn:{TOKEN}")
    asyncio.run(AX.on_button(u, ctx))
    assert "MEME" in u.effective_chat.send_message.call_args.args[0]
    u.callback_query.edit_message_text.assert_not_called()


def test_consensus_boost_trailing_and_auto_rank(tmp_path):
    from dataclasses import replace
    from copybot.sol import api
    from copybot.sol.research import summarize
    s, ctx, sent = ready(tmp_path)
    a, b = _two_leaders(s)
    sw_b = lambda side, sig: replace(swap(side, 0.1, sig), wallet=W2)  # noqa: E731
    # tylko przy potwierdzeniu 2 traderów
    s.db.update_user(OWNER, sol_consensus=2)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "c1"), time.monotonic()))
    assert res["skipped"] == 1 and any("czekam na potwierdzenie" in t for _, t in sent)
    res = asyncio.run(s.sol.on_swap(W2, sw_b("buy", "c2"), time.monotonic()))
    assert res["filled"] == 1 and "sygnał zbiorowy" in s.sol.history(OWNER)[0]["info"]
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "c3"), time.monotonic()))  # 3. zakup – już kupione
    assert res["skipped"] == 1
    # zawsze kupuj, a przy potwierdzeniu dołóż do ×2
    s.sol.paper_reset(OWNER)
    s.sol._recent_buys.clear()
    s.db._q("DELETE FROM state WHERE key LIKE 'boost:%'")
    s.db.update_user(OWNER, sol_consensus=0, sol_boost=2, sol_no_add=1, sol_buy=0.05)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "d1"), time.monotonic()))
    first = s.sol.paper(OWNER)[TOKEN]
    asyncio.run(s.sol.on_swap(W2, sw_b("buy", "d2"), time.monotonic()))  # potwierdzenie → dokładka mimo „nie dokupuj”
    assert s.sol.paper(OWNER)[TOKEN] == 2 * first
    # ruchomy stop: ×3, potem spadek o >30% od szczytu → sprzedaż całości
    s.db.update_user(OWNER, sol_trail=30, sol_x2=0)
    jctx = SimpleNamespace(application=ctx.application, bot=AsyncMock())
    s.sol.jup.token_usd = 0.3
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN in s.sol.paper(OWNER)
    s.sol.jup.token_usd = 0.19
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN not in s.sol.paper(OWNER) and any("RUCHOMY STOP" in t for _, t in sent)
    # automatyczna ocena: tracący trader wyłączony, najlepszy włączony
    s.db.save_wallets(OWNER, [(WALLET, "A", None), (W2, "B", None), ("Wa11et3333333333333333333333333333333333333", "C", None)], "t")
    now = time.time()
    from copybot.sol.parser import Swap as SW

    def x(side, mint, sol):
        return (now - 100, SW("x", "w", side, mint, 100, 6, "SOL", sol, 1.0, 0))
    good = summarize("C", [x("buy", "A", 1), (now - 30, x("sell", "A", 2)[1]), x("buy", "B", 1), (now - 20, x("sell", "B", 1.5)[1]),
                           x("buy", "C", 1), (now - 10, x("sell", "C", 1.2)[1])], 3)
    bad = summarize("A", [x("buy", "A", 1), (now - 30, x("sell", "A", 0.5)[1]), x("buy", "B", 1), (now - 20, x("sell", "B", 0.4)[1]),
                          x("buy", "C", 1), (now - 10, x("sell", "C", 0.6)[1])], 3)
    s.db.set_research("Wa11et3333333333333333333333333333333333333", good)
    s.db.set_research(WALLET, bad)
    s.db.set_research(W2, good)
    off, on = api.apply_rank(s, OWNER)
    assert off == ["A"] and on == ["C"] and s.sol.watched_wallets() == {W2, "Wa11et3333333333333333333333333333333333333"}


def test_sell_retry_and_friendly_errors(tmp_path):
    from copybot.sol.chain import ChainError
    from copybot.sol.engine import Fill, friendly_error
    s, _, _ = ready(tmp_path)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, sol_live=1, sol_slippage_bps=1500)
    tries = []

    async def flaky(u, side, mint, amount, t_recv=None, decimals=None, fallback=None):
        tries.append(u["sol_slippage_bps"])
        if len(tries) < 3:
            raise ChainError("transakcja odrzucona przez sieć: {'InstructionError': [3, {'Custom': 6001}]}")
        return Fill(0.05, 1.0, "sig", 5, 1.0)

    async def bal(u, mint):
        return 1000
    s.sol.execute, s.sol.token_raw = flaky, bal
    import copybot.sol.engine as E
    real = E.asyncio.sleep
    E.asyncio.sleep = AsyncMock()
    try:
        fill = asyncio.run(s.sol.sell_with_retry(s.db.get_user(OWNER), TOKEN, 1000))
    finally:
        E.asyncio.sleep = real
    assert fill.sig == "sig" and tries == [1500, 3000, 4900]  # tolerancja rośnie przy ponowieniach
    u = s.db.get_user(OWNER)
    assert "cena uciekła" in friendly_error(ChainError("{'InstructionError': [3, {'Custom': 6001}]}"), u)
    assert "przeszedł z Pump.fun" in friendly_error(ChainError("{'InstructionError': [3, {'Custom': 6005}]}"), u)
    assert "limit zapytań" in friendly_error(ChainError("Jupiter swap: {'code': 429}"), u)


def test_nicknames(tmp_path):
    s, ctx, _ = ready(tmp_path)

    async def steps(call):
        st, j = await call(FRIEND, "/api/sol/action", {"action": "nickname", "uid": OWNER, "name": "Szef"})
        assert st == 400  # zwykły użytkownik nie zmienia nazw innym
        st, j = await call(FRIEND, "/api/sol/action", {"action": "nickname", "name": "  Ziomek   z   bloku "})
        assert st == 200 and s.db.get_user(FRIEND)["nickname"] == "Ziomek z bloku"
        st, j = await call(OWNER, "/api/sol/action", {"action": "nickname", "uid": FRIEND, "name": "Kumpel"})
        assert st == 200
        st, j = await call(OWNER, "/api/sol/admin/users")
        assert any(x["name"] == "Kumpel" for x in j["users"])
    run(s, steps)
    # w bocie: admin zmienia nazwę przez przycisk, każdy sobie przez /nazwa
    async def chat():
        u = upd(OWNER, data=f"adm:ren:{FRIEND}")
        assert await ADM.ren_start(u, ctx) == ADM.REN
        await ADM.ren_text(upd(OWNER, text="Bratan"), ctx)
        ctx.args = ["Kacper"]
        await ADM.cmd_nazwa(upd(OWNER, text="/nazwa Kacper"), ctx)
    asyncio.run(chat())
    assert s.db.get_user(FRIEND)["nickname"] == "Bratan" and s.db.get_user(OWNER)["nickname"] == "Kacper"


def test_group_chat(tmp_path):
    from copybot.sol import api
    s, ctx, sent = ready(tmp_path)
    s.db.update_user(OWNER, nickname="Kacper")
    api._CHAT_PING.clear()

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "chat_send", "text": "siema, patrzcie " + TOKEN})
        assert st == 200
        await asyncio.sleep(0.05)
        st, j = await call(OWNER, "/api/sol/action", {"action": "chat_send", "text": "spam"})
        assert st == 400  # za szybko
        st, j = await call(FRIEND, "/api/sol/chat")
        m = j["messages"][0]
        assert m["name"] == "Kacper" and m["role"] == "owner" and not m["mine"] and TOKEN in m["text"]
        st, j = await call(FRIEND, "/api/sol/action", {"action": "chat_send", "text": "<b>xd</b>"})
        st, j = await call(OWNER, f"/api/sol/chat?after={m['id']}")
        assert len(j["messages"]) == 1 and j["messages"][0]["text"] == "<b>xd</b>"  # tekst surowy, escapowany w UI
        st, j = await call(FRIEND, "/api/sol/action", {"action": "chat_delete", "id": m["id"]})
        assert st == 400  # cudzej nie usunie
        st, j = await call(OWNER, "/api/sol/action", {"action": "chat_delete", "id": m["id"]})
        st, j = await call(OWNER, "/api/sol/chat")
        assert [x["text"] for x in j["messages"]] == ["<b>xd</b>"]
        st, j = await call(OWNER, "/api/sol/state")
        assert j["chat_last_id"] >= 2
    run(s, steps)
    assert any(uid == FRIEND and "Kacper na czacie" in t for uid, t in sent)  # powiadomienie dla reszty

    async def chat_cmd():
        ctx.args = ["hej", "z", "telegrama"]
        await ADM.cmd_czat(upd(FRIEND, text="/czat hej z telegrama"), ctx)
    asyncio.run(chat_cmd())
    assert s.db.chat_list()[-1]["text"] == "hej z telegrama"


def test_turbo_jito_prequote_and_timings(tmp_path):
    from copybot.sol.engine import Fill  # noqa: F401
    s, _, _ = ready(tmp_path)
    lid = s.db.create_leader("K", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1, sol_turbo=1, sol_priority=200_000)
    quotes = []
    real_quote = s.sol.jup.quote

    async def q(*a, **kw):
        quotes.append(a)
        return await real_quote(*a, **kw)
    s.sol.jup.quote = q
    # ćwiczenia: turbo puszcza wycenę równolegle – jedna wycena, zakup działa
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    assert res["filled"] == 1 and len(quotes) == 1
    # prawdziwe SOL: napiwek Jito w budowie transakcji i wysyłka także przez Jito
    s.db.update_user(OWNER, sol_live=1)
    calls = {}

    async def swap_tx(quote, kp, prio, jito_tip=None):
        calls["tip"] = jito_tip
        return b"tx"

    async def send_and_confirm(tx, timeout=45, also=None):
        calls["also"] = also
        return "sig", 0.8

    class FakeJito:
        async def send(self, b):
            return "sig"
    s.sol.jup.swap, s.sol.rpc.send_and_confirm, s.sol.jito = swap_tx, send_and_confirm, FakeJito()

    async def bal(pk):
        return 5 * 10 ** 9
    s.sol.rpc.balance = bal
    fill = asyncio.run(s.sol.execute(s.db.get_user(OWNER), "buy", TOKEN, 50_000_000, decimals=6))
    assert calls["tip"] == 200_000 and calls["also"] and fill.note == "⚡ turbo"
    assert s.sol.timings[-1]["turbo"] and s.sol.timings[-1]["confirm"] == 800


def test_ranking_table(tmp_path):
    s, _, _ = ready(tmp_path)
    a, b = _two_leaders(s)
    s.db.save_wallets(OWNER, [(WALLET, "A", "👻"), (W2, "B", None)], "t")
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "r1"), time.monotonic()))
    s.sol.jup.token_usd = 0.2
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "r2"), time.monotonic()))

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/ranking")
        rows = {r["name"]: r for r in j["rows"]}
        assert st == 200 and rows["A"]["pnl_7d"] > 0 and rows["A"]["copies"] == 1 and rows["A"]["wins"] == 1
        assert rows["B"]["pnl_all"] == 0 and not j["live"]
    run(s, steps)


def test_live_events_and_gzip(tmp_path):
    s, _, _ = ready(tmp_path)
    lid = s.db.create_leader("K", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1)

    async def steps(call):
        st, snap = await call(OWNER, "/api/sol/live")
        assert st == 200 and "v" in snap

        async def later():
            await asyncio.sleep(0.2)
            await s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic())
        t0 = time.monotonic()
        task = asyncio.create_task(later())
        st, j = await call(OWNER, f"/api/sol/live?v={snap['v']}&c={snap['c']}")
        await task
        assert j["v"] > snap["v"] and time.monotonic() - t0 < 5  # obudzony od razu po kupnie
        s.db.chat_add(FRIEND, "hej")
        st, j2 = await call(OWNER, f"/api/sol/live?v={j['v']}&c={j['c']}")
        assert j2["c"] > j["c"]
    run(s, steps)

    async def gz():
        from aiohttp.test_utils import TestClient, TestServer
        from copybot.web import build_app
        client = TestClient(TestServer(build_app(s)))
        await client.start_server()
        try:
            r = await client.get("/app", headers={"Accept-Encoding": "gzip"})
            assert r.status == 200 and r.headers.get("Content-Encoding") == "gzip" and r.headers.get("ETag")
            r2 = await client.get("/app", headers={"If-None-Match": r.headers["ETag"]})
            assert r2.status == 304
        finally:
            await client.close()
    asyncio.run(gz())


def test_bulk_select_actions(tmp_path):
    s, _, _ = ready(tmp_path)
    w3 = "Wa11et3333333333333333333333333333333333333"
    s.db.save_wallets(OWNER, [(WALLET, "A", None), (W2, "B", None), (w3, "C", None)], "t")
    s.sol.wallet(OWNER)

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "bulk", "op": "on", "wallets": [WALLET, W2]})
        assert st == 200 and j["done"] == 2 and s.sol.watched_wallets() == {WALLET, W2}
        st, j = await call(OWNER, "/api/sol/action", {"action": "bulk", "op": "star", "wallets": [w3]})
        st, j = await call(OWNER, "/api/sol/action", {"action": "bulk", "op": "delete", "wallets": [WALLET, w3]})
        assert j["done"] == 2
        st, j = await call(OWNER, "/api/sol/saved")
        assert [i["name"] for i in j["items"]] == ["B"] and s.sol.watched_wallets() == {W2}
        st, j = await call(OWNER, "/api/sol/action", {"action": "bulk", "op": "off", "wallets": [W2]})
        assert s.sol.watched_wallets() == set()
        st, j = await call(OWNER, "/api/sol/action", {"action": "bulk", "op": "zle", "wallets": [W2]})
        assert st == 400
    run(s, steps)


def test_sensible_safeguards(tmp_path):
    from copybot.sol.risk import trap_reason
    fresh = {"liquidity": 6000, "holderCount": 40,
             "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 85}}
    assert trap_reason(fresh) is None  # świeży token z Pump.fun to nie pułapka
    assert "zamrozić" in trap_reason({**fresh, "audit": {"freezeAuthorityDisabled": False}})
    assert "prawie nie ma" in trap_reason({**fresh, "liquidity": 500})
    s, ctx, sent = ready(tmp_path)
    _two_leaders(s)
    # minimalna kwota zakupu
    s.db.update_user(OWNER, sol_buy=0.004, sol_min_buy=0.01)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "m1"), time.monotonic()))
    assert res["skipped"] == 1 and any("za mała kwota" in t for _, t in sent)
    # limit otwartych pozycji
    s.db.update_user(OWNER, sol_buy=0.05, sol_min_buy=0, sol_max_pos=1)
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), "Other1111111111111111111111111111111111111", 0.05))
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "m2"), time.monotonic()))
    assert res["skipped"] == 1 and any("otwartych pozycji" in t for _, t in sent)
    # przerwa po serii strat
    s.db.update_user(OWNER, sol_max_pos=0, sol_streak=2)
    for i in range(2):
        s.sol.record(OWNER, None, None, "sell", f"L{i}", 0.01, 1, "filled", "x", True, pnl=-0.01)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "m3"), time.monotonic()))
    assert res["skipped"] == 1 and any("przerwa po 2 stratach" in t for _, t in sent)
    # ruchomy stop przy zwrocie wkładu włącza się dopiero od ×2
    s.db.update_user(OWNER, sol_streak=0, sol_trail=40, sol_x2=1)
    s.sol.paper_reset(OWNER)
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.1))
    jctx = SimpleNamespace(application=ctx.application, bot=AsyncMock())
    s.sol.jup.token_usd = 0.18  # ×1,8 – szczyt
    asyncio.run(AX.tpsl_job(jctx))
    s.sol.jup.token_usd = 0.1  # spadek o 44% od szczytu, ale szczyt < ×2 → trzymamy
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN in s.sol.paper(OWNER)


def test_optimizer_on_reference_moves(tmp_path):
    from copybot.db import ref_id
    from copybot.sol import optimizer as OPT
    s, _, _ = ready(tmp_path)
    a, b = _two_leaders(s)
    t0 = int(time.time() * 1000) - 3600_000

    def ref(lid, side, mint, sol, tok, trader, dt):
        s.db._q("INSERT INTO sol_trades (tg_id, leader_id, side, mint, sol, tokens, status, info, paper, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'filled', ?, 1, ?)", (ref_id(lid), lid, side, mint, sol, tok, trader, t0 + dt))
    # A: kupuje dużo i trzyma minutami → zysk ×2;  B: flipper (5 s) ze stratą
    for i in range(4):
        ref(a, "buy", f"A{i}", 0.5, 100, "2", i * 600_000)
        ref(a, "sell", f"A{i}", 1.0, 100, "4", i * 600_000 + 300_000)
        ref(b, "buy", f"B{i}", 0.5, 100, "0.2", i * 600_000)
        ref(b, "sell", f"B{i}", 0.4, 100, "0.16", i * 600_000 + 5_000)
    res = OPT.optimize(s, s.db.get_user(OWNER), 5.0, [a, b], 1.0)
    assert res["events"] == 16 and res["results"][0]["pnl_pct"] > res["current"]["pnl_pct"]
    assert res["leaders"][0]["id"] == a and res["leaders"][0]["pnl"] > 0
    best = res["results"][0]["settings"]
    assert "sol_buy_mode" in best and best.get("sol_min_hold", 0) >= 0

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "optimize"})
        assert st == 200 and j["events"] == 16
        st, j = await call(OWNER, "/api/sol/action", {"action": "apply_settings", "settings": best})
        assert st == 200 and s.db.get_user(OWNER)["sol_buy_mode"] == best["sol_buy_mode"]
    run(s, steps)


def test_backup_csv_pin_health_report(tmp_path):
    import gzip as gz
    import sqlite3 as sq
    from copybot.sol import extras as EX
    s, ctx, sent = ready(tmp_path)
    s.sol.wallet(OWNER)
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.05))
    asyncio.run(s.sol.sell_manual(s.db.get_user(OWNER), TOKEN, 100))
    # kopia zapasowa: poprawna baza SQLite z portfelami
    raw = gz.decompress(EX.make_backup(s.db))
    p = tmp_path / "kopia.db"
    p.write_bytes(raw)
    assert sq.connect(p).execute("SELECT COUNT(*) FROM sol_wallets").fetchone()[0] == 1
    # CSV
    txt = EX.history_csv(s, OWNER).decode("utf-8-sig")
    assert "kupno" in txt and "sprzedaż" in txt and "ćwiczenia" in txt
    # raport dnia
    rep = EX.period_report(s, s.db.get_user(OWNER), 1)
    assert "RAPORT DNIA" in rep and "sprzedaże 1" in rep
    # alerty: zły klucz Jupitera
    s.sol.jup.auth_error = "401: bad key"
    assert any(k == "jup" for k, _ in EX.health_problems(s))

    # PIN: wypłata wymaga PIN-u
    async def steps(call):
        st, j = await call(OWNER, "/api/sol/action", {"action": "set_pin", "pin": "12a"})
        assert st == 400
        st, j = await call(OWNER, "/api/sol/action", {"action": "set_pin", "pin": "4821"})
        assert st == 200 and s.db.get_user(OWNER)["pin_hash"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "withdraw", "address": WALLET, "amount": "0.1",
                                                      "confirm": True, "pin": "0000"})
        assert st == 400 and "PIN" in j["error"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "set_pin", "pin": "", "old_pin": "4821"})
        assert s.db.get_user(OWNER)["pin_hash"] is None
    run(s, steps)
    u = s.db.get_user(OWNER)
    s.db.update_user(OWNER, pin_hash=EX.hash_pin("1234"))
    u = s.db.get_user(OWNER)
    assert not EX.unlocked(u) and EX.pin_ok(u, "1234") and not EX.pin_ok(u, "9999")


def test_token_filters_time_and_dev_exit_and_smart(tmp_path):
    from datetime import datetime, timedelta, timezone
    from copybot.sol import extras as EX
    s, ctx, sent = ready(tmp_path)
    a, b = _two_leaders(s)
    good = s.sol.jup.token_full

    async def fresh(mint):
        d = await good(mint)
        d["firstPool"] = {"createdAt": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}
        d["audit"] = dict(d["audit"], devBalancePercentage=10.0)
        return d
    s.sol.jup.token_full = fresh
    s.db.update_user(OWNER, sol_max_mcap=1_000_000)  # MEME ma $1,2M
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "f1"), time.monotonic()))
    assert res["skipped"] == 1 and any("wart już" in t for _, t in sent)
    s.db.update_user(OWNER, sol_max_mcap=0, sol_min_age=10)
    res = asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "f2"), time.monotonic()))
    assert res["skipped"] == 1 and any("ma dopiero" in t for _, t in sent)
    # zakup z zapamiętaniem udziału twórcy, potem twórca wyprzedaje → sprzedaż
    s.db.update_user(OWNER, sol_min_age=0, sol_dev_exit=1)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "f3"), time.monotonic()))
    assert TOKEN in s.sol.paper(OWNER)

    async def dumped(mint):
        d = await fresh(mint)
        d["audit"]["devBalancePercentage"] = 0.5
        return d
    s.sol.jup.token_full = dumped
    jctx = SimpleNamespace(application=ctx.application, bot=AsyncMock())
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN not in s.sol.paper(OWNER) and any("TWÓRCA UCIEKA" in t for _, t in sent)
    # wyjście po czasie: pozycja bez ruchu od 2 h
    s.sol.jup.token_full = good
    s.db.update_user(OWNER, sol_dev_exit=0, sol_time_exit=60)
    asyncio.run(AX.buy(s, s.db.get_user(OWNER), TOKEN, 0.05))
    s.db._q("UPDATE sol_trades SET ts = ts - 7200000 WHERE tg_id = ? AND mint = ?", (OWNER, TOKEN))
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN not in s.sol.paper(OWNER) and any("WYJŚCIE PO CZASIE" in t for _, t in sent)
    # inteligentne kwoty: A zarabia, B traci
    s.db.update_user(OWNER, sol_buy_mode="pct", sol_pct=10)
    for lid, p in ((a, 0.05), (b, -0.05)):
        for i in range(3):
            s.sol.record(OWNER, lid, None, "sell", f"S{lid}{i}", 0.1, 1, "filled", "x", True, pnl=p)
    s.db.save_wallets(OWNER, [(WALLET, "A", None), (W2, "B", None)], "t")
    ch = EX.smart_allocation(s, s.db.get_user(OWNER))
    assert any("⬆️ A" in c for c in ch) and any("⬇️ B" in c for c in ch)
    assert s.db.follow_row(OWNER, a)["sol_pct"] == 15 and s.db.follow_row(OWNER, b)["sol_pct"] == 5


def test_leaderboard(tmp_path):
    import json as js
    from copybot.sol import leaderboard as LB
    s, _, _ = ready(tmp_path)
    good = {"pnl": 12.5, "roi": 80, "win_rate": 0.6, "closed": 10, "swaps": 25, "hold_s": 300, "green_days": 5,
            "active_days": 6, "avg_buy": 1.2, "partial": False, "last": time.time()}
    flip = dict(good, pnl=3, roi=20, hold_s=8)
    for w, a, sc in ((WALLET, good, 90.0), (W2, flip, 10.0)):
        s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, ?, ?, 'kupował MEME')", (w, js.dumps(a), sc, int(time.time() * 1000)))
    assert [r["wallet"] for r in LB.top(s, 7)] == [WALLET, W2]
    assert LB.top(s, 7, "roi")[0]["roi"] == 80

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/leaderboard?period=7")
        assert st == 200 and j["rows"][0]["wallet"] == WALLET and not j["rows"][0]["in_list"]
        st, j = await call(OWNER, "/api/sol/action", {"action": "lb_add", "wallet": W2})
        st, j = await call(OWNER, "/api/sol/action", {"action": "lb_add", "wallet": WALLET, "copy": True})
        assert s.sol.watched_wallets() == {WALLET}
        st, j = await call(OWNER, "/api/sol/leaderboard?period=7")
        assert j["rows"][0]["status"] == "copy" and j["rows"][1]["in_list"]
        st, j = await call(FRIEND, "/api/sol/action", {"action": "lb_refresh"})
        assert st == 400  # tylko admin
    run(s, steps)


def test_leaderboard_world(tmp_path, monkeypatch):
    from copybot.sol import leaderboard as LB
    s, _, _ = ready(tmp_path)
    W3 = "So11111111111111111111111111111111111111112"
    st_top = {"traders": [
        {"wallet": WALLET, "period": {"realized": 52000, "roi": 140, "tradingDays": 6,
                                      "days": {"profitable": 5, "losing": 1, "maxSinglePnl": 9000}},
         "counts": {"trades": 400, "tokensTraded": 60}, "tokens": {"closed": 55}, "winRate": 61,
         "averages": {"buy": 300}, "timing": {"lastTrade": 1_776_000_000_000}, "identity": {"name": None}},
        {"wallet": W2, "period": {"realized": 90000, "roi": 30, "tradingDays": 5, "days": {"profitable": 2, "losing": 3}},
         "counts": {"trades": 20000}, "winRate": 50}]}  # 4000 transakcji / dzień → bot
    st_kols = {"traders": [{"wallet": W3, "period": {"realized": 8000, "tradingDays": 4},
                            "identity": {"name": "Ziomek", "twitter": "ziomek"}}]}
    vybe = {"data": [{"accountAddress": WALLET, "accountLabels": ["KOL"], "accountName": "Mistrz",
                      "metrics": {"realizedPnlUsd": 50000, "unrealizedPnlUsd": 10, "winRate": 60, "tradesVolumeUsd": 1,
                                  "tradesCount": 380, "sevenDayPnl": [[1, 5], [2, -1], [3, 7], [4, 2]]}}]}
    calls = []

    async def fake_get(session, url, params, headers):
        calls.append((url, params, headers))
        if "kols" in url:
            return st_kols
        return vybe if "vybenetwork" in url else st_top

    async def fake_prices(mints):
        return {LB.SOL_MINT: 200.0}

    async def no_holds(s_, wallets):
        return None
    monkeypatch.setattr(LB, "_get", fake_get)
    monkeypatch.setattr(LB, "check_holds", no_holds)
    s.sol.jup.prices = fake_prices
    s.sol.jup.session = None
    # bez kluczy – nic nie pobiera
    assert asyncio.run(LB.rebuild_world(s)) == 0 and not calls
    LB.set_key(s, "st", "st-key-123456789012")
    LB.set_key(s, "vybe", "vy-key-123456789012")
    assert LB.key_status(s) == {"st": "app", "vybe": "app"}
    assert asyncio.run(LB.rebuild_world(s)) == 9
    assert any(h.get("x-api-key") == "st-key-123456789012" for _, _, h in calls)
    assert any(h.get("X-API-KEY") == "vy-key-123456789012" for _, _, h in calls)
    rows = LB.top(s, 7, kind="world")
    by = {r["wallet"]: r for r in rows}
    assert rows[0]["wallet"] == WALLET  # stabilny, mniejszy zysk > bot z większym
    assert by[W2]["bot"] and not by[WALLET]["bot"]
    assert by[WALLET]["name"] == "Mistrz" and by[WALLET]["kol"] and by[WALLET]["pnl"] == 260
    assert by[WALLET]["series"] == [5, -1, 7, 2] and by[WALLET]["green_days"] == 5
    assert by[W3]["kol"] and by[W3]["name"] == "Ziomek"
    assert LB.top(s, 7, "green", kind="world")[0]["wallet"] == WALLET
    assert LB.top(s, 7, kind="chain") == []

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/leaderboard?period=30")
        assert st == 200 and j["kind"] == "world" and j["counts"]["world"] == 3 and j["keys"]["st"] == "app"
        st, j = await call(FRIEND, "/api/sol/leaderboard?period=30")
        assert j["keys"] is None and j["has_world"]
        st, j = await call(FRIEND, "/api/sol/action", {"action": "lb_keys", "st": "x" * 30})
        assert st == 400
        st, j = await call(OWNER, "/api/sol/action", {"action": "lb_keys", "vybe": "zły klucz"})
        assert st == 400
        LB.STATE["running"] = True  # nie uruchamiaj liczenia w teście
        st, j = await call(OWNER, "/api/sol/action", {"action": "lb_keys", "vybe": ""})
        LB.STATE["running"] = False
        assert st == 200 and j["keys"]["vybe"] is None
    run(s, steps)



def test_autocfg_plan_and_names(tmp_path):
    import json as js
    from copybot.db import ref_id
    from copybot.sol import autocfg as AC, leaderboard as LB
    s, _, _ = ready(tmp_path)
    # dwóch traderów: jeden kupuje po ~0,5 SOL, drugi po ~10 SOL → osobne %, ta sama zwykła pozycja u Ciebie
    a = s.db.create_leader("Portfel " + WALLET[:4] + "…" + WALLET[-4:], "sol", OWNER, "active", wallet=WALLET)
    b = s.db.create_leader("Wieloryb", "sol", OWNER, "active", wallet=W2)
    s.db.follow(OWNER, a, True)
    s.db.follow(OWNER, b, True)
    for lid, amt in ((a, 0.5), (b, 10.0)):
        for i in range(5):
            s.sol.record(ref_id(lid), lid, None, "buy", f"M{lid}{i}", amt, 1000, "filled", str(amt), True)
    tr = {t["lid"]: t for t in AC.trader_profiles(s, OWNER)}
    assert tr[a]["median"] == 0.5 and tr[b]["median"] == 10.0 and tr[a]["src"] == "7 dni jego zakupów"
    p = AC.plan(5.0, list(tr.values()), live=True)
    target = p["target"]
    assert abs(p["per_trader"][a] / 100 * 0.5 - target) / target < 0.06
    assert abs(p["per_trader"][b] / 100 * 10 - target) / target < 0.06
    v = p["values"]
    assert v["sol_max"] == AC.r2(target * 2) and v["sol_max_pos"] >= 4 and v["sol_min_buy"] * 0.025 >= p["fees"] * 0.9
    assert v["sol_max_pos"] * target <= 5.0 * 0.9  # nie wydaje więcej niż ma
    # małe saldo: mniej pozycji, ale zakup nie mniejszy niż minimum opłacalności
    small = AC.plan(0.08, list(tr.values()), live=True)
    assert small["target"] >= small["values"]["sol_min_buy"] * 0.99 and small["warn"]
    assert AC.plan(0.005, [], live=True)["values"]["sol_max_pos"] == 0
    # duże saldo: pojedynczy zakup ograniczony (memecoiny mają małą płynność)
    assert AC.plan(500, list(tr.values()), live=True)["values"]["sol_buy"] <= AC.MAX_ONE_BUY
    # zastosowanie: osobne % na kartach traderów, ręczna stała kwota zostaje
    s.db.set_follow_amount(OWNER, b, sol=0.2)
    u = s.db.get_user(OWNER)
    AC.apply(s, u, 5.0)
    assert s.db.follow_row(OWNER, a)["sol_pct"] == p["per_trader"][a] and s.db.follow_row(OWNER, b)["sol_buy"] == 0.2
    u = s.db.get_user(OWNER)
    assert AC.needs_update(u, 5.2, AC.trader_profiles(s, OWNER)) is None
    assert "saldo" in AC.needs_update(u, 6.0, AC.trader_profiles(s, OWNER))
    s.db.unfollow(OWNER, b)
    assert "lista" in AC.needs_update(u, 5.0, AC.trader_profiles(s, OWNER))
    # nazwy z rankingu
    assert LB.rank_name("win", 5, 7) == "🎯 Top 5 skuteczność 7d"
    assert LB.is_generic("🌍 9rXz…B2jJ") and LB.is_generic("Portfel 9rXz…B2jJ") and not LB.is_generic("Kowal")
    now = int(time.time() * 1000)
    for w, pnl, wr in ((WALLET, 50000, 0.9), (W2, 90000, 0.5)):
        st = {"kind": "world", "pnl_usd": pnl, "pnl": pnl / 200, "win_rate": wr, "src": ["X"], "green_days": 3, "active_days": 5}
        st["score"] = LB.world_score(st, 7)
        s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, ?, ?, '🌍 X')", (w, js.dumps(st), st["score"], now))
    s.db.save_wallets(OWNER, [(WALLET, "🌍 " + WALLET[:4] + "…" + WALLET[-4:], None), (W2, "Wieloryb", None)], "t")
    assert LB.auto_name(s, OWNER) == 1
    names = {r["wallet"]: r["name"] for r in s.db._q("SELECT wallet, name FROM sol_saved WHERE tg_id = ?", (OWNER,))}
    assert names[WALLET] == "🏆 Top 1 ocena 7d" and names[W2] == "Wieloryb"  # własnej nazwy nie rusza
    assert s.db.get_leader(a)["name"] == "🏆 Top 1 ocena 7d"

    async def steps(call):
        W3 = "So11111111111111111111111111111111111111112"
        st, j = await call(OWNER, "/api/sol/action", {"action": "lb_add", "wallet": W3, "rank": 20, "sort": "score",
                                                       "period": 30, "kol": "Cented"})
        assert st == 200, j
        n = s.db._q("SELECT name FROM sol_saved WHERE tg_id = ? AND wallet = ?", (OWNER, W3))[0]["name"]
        assert n == "⭐ Cented · 🏆 Top 20 ocena 30d"
        st, j = await call(OWNER, "/api/sol/action", {"action": "autocfg_preview", "balance": 2})
        assert st == 200 and "zwykła pozycja" in j["text"]
    run(s, steps)


def test_admin_copy_traders(tmp_path):
    s, _, _ = ready(tmp_path)
    a = s.db.create_leader("Kowal", "sol", FRIEND, "active", wallet=WALLET)
    b = s.db.create_leader("Wieloryb", "sol", FRIEND, "active", wallet=W2)
    s.db.follow(FRIEND, a, True)
    s.db.follow(FRIEND, b, False)

    async def steps(call):
        st, j = await call(FRIEND, "/api/sol/action", {"action": "admin_copy_traders", "uid": OWNER})
        assert st == 400  # tylko admin
        st, j = await call(OWNER, "/api/sol/admin/user/%d" % FRIEND)
        assert {w["wallet"] for w in j["wallets"]} == {WALLET, W2}
        st, j = await call(OWNER, "/api/sol/action", {"action": "admin_copy_traders", "uid": FRIEND, "copy": True})
        assert st == 200 and j["turned"] == 1 and s.sol.watched_wallets() >= {WALLET}
        assert s.db.follow_row(OWNER, a)["copy"] == 1 and not s.db.follow_row(OWNER, b)  # obserwowany nie skopiowany
        st, j = await call(OWNER, "/api/sol/action", {"action": "admin_copy_traders", "uid": FRIEND,
                                                       "only_copied": False})
        assert st == 200 and j["added"] == 1
        names = {r["wallet"]: r["name"] for r in s.db.saved_wallets(OWNER)}
        assert names[W2] == "Wieloryb"
    run(s, steps)


def test_name_everyone(tmp_path):
    import json as js
    from copybot.sol import leaderboard as LB
    s, _, _ = ready(tmp_path)
    now = int(time.time() * 1000)
    for w, pnl in ((WALLET, 50000), (W2, 90000)):
        st = {"kind": "world", "pnl_usd": pnl, "pnl": pnl / 200, "win_rate": .6, "src": ["X"], "name": "Cented" if w == W2 else None}
        st["score"] = LB.world_score(st, 7)
        s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, ?, ?, '🌍 X')", (w, js.dumps(st), st["score"], now))
    s.db.save_wallets(OWNER, [(WALLET, "Cupsey", None), (W2, "🌍 " + W2[:4] + "…" + W2[-4:], None)], "t")
    s.db.save_wallets(FRIEND, [(WALLET, "kumpel", None)], "t")
    lid = s.db.create_leader("Kowal", "sol", OWNER, "active", wallet=WALLET)
    assert LB.auto_name(s, everyone=True) >= 3
    names = {(r["tg_id"], r["wallet"]): r["name"] for r in s.db._q("SELECT tg_id, wallet, name FROM sol_saved")}
    assert names[(OWNER, W2)] == "⭐ Cented · 🏆 Top 1 ocena 7d"
    assert names[(OWNER, WALLET)].endswith(" · Cupsey") and "Top 2" in names[(OWNER, WALLET)]
    assert names[(FRIEND, WALLET)].endswith(" · kumpel")
    assert s.db.get_leader(lid)["name"].endswith(" · Kowal")
    # drugi raz nie dokleja kolejnych dopisków
    before = dict(names)
    LB.auto_name(s, everyone=True)
    after = {(r["tg_id"], r["wallet"]): r["name"] for r in s.db._q("SELECT tg_id, wallet, name FROM sol_saved")}
    assert after == before
    assert LB.custom_part("⭐ Cented · 🏆 Top 1 ocena 7d") is None


def test_late_sell_still_runs_and_day_limit_not_shrinking(tmp_path):
    import pytest
    from copybot.sol import autocfg as AC
    from copybot.sol.engine import Skip
    s, _, _ = ready(tmp_path)
    s.sol._fresh(time.monotonic() - 60, "sell")  # sprzedaż spóźniona o minutę – nie odrzucamy
    with pytest.raises(Skip):
        s.sol._fresh(time.monotonic() - 60, "buy")
    u = s.db.get_user(OWNER)
    AC.apply(s, u, 1.0)
    lim = s.db.get_user(OWNER)["sol_day_limit"]
    AC.apply(s, s.db.get_user(OWNER), 0.6)  # saldo spadło w ciągu dnia
    assert s.db.get_user(OWNER)["sol_day_limit"] == lim


def test_open_positions_count_only_real_holdings(tmp_path):
    s, _, _ = ready(tmp_path)
    u = s.db.get_user(OWNER)
    s.sol.wallet(OWNER)
    paper = not s.sol.is_live(u)
    for m in ("MINTA", "MINTB", "MINTC"):
        s.sol.record(OWNER, None, None, "buy", m, 0.1, 1000, "filled", "x", paper)
    # w portfelu został tylko A (B sprzedany ręcznie poza botem, C to martwy token bez trasy)
    s.sol.paper_apply(OWNER, {"MINTA": 1000, "MINTC": 5})
    s.sol._unvaluable = {"MINTC": time.monotonic()}
    assert s.sol.open_positions(OWNER, paper) == 3  # stary sposób – liczył wszystkie
    assert asyncio.run(s.sol.open_positions_now(u)) == 1


def test_pct_rounds_up_to_minimum_and_autocfg_trusts_real_buys(tmp_path):
    import pytest
    from copybot.db import ref_id
    from copybot.sol import autocfg as AC
    s, _, _ = ready(tmp_path)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, sol_buy_mode="pct", sol_pct=2, sol_min_buy=0.016, sol_max=1)
    u = s.db.get_user(OWNER)

    async def his(sw):
        return his.v
    s.sol.trader_sol = his
    his.v = 0.4  # 2% = 0.008 SOL → połowa minimum → kupuje za minimum
    assert asyncio.run(s.sol.buy_amount(u, swap("buy", 0.4, "x1"))) == 16_000_000
    his.v = 0.1  # 2% = 0.002 SOL → zakup testowy → pomija
    with pytest.raises(Exception):
        asyncio.run(s.sol.buy_amount(u, swap("buy", 0.1, "x2")))
    # „średni zakup” z rankingu 100 SOL nie wygrywa z prawdziwymi zakupami tradera
    import json as js
    lid = s.db.create_leader("🏆 Top 1 ocena 7d", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, 1, ?, '🌍 X')", (WALLET, js.dumps({"avg_buy": 100}), int(time.time() * 1000)))
    assert AC.trader_profiles(s, OWNER)[0]["median"] == AC.HINT_CAP
    s.sol.record(ref_id(lid), lid, None, "buy", "M1", 0.3, 1000, "filled", "0.3", True)
    p = AC.trader_profiles(s, OWNER)[0]
    assert p["median"] == 0.3 and "1 jego" in p["src"]


def test_leaderboard_activity(tmp_path):
    import json as js
    from copybot.sol import leaderboard as LB
    s, _, _ = ready(tmp_path)
    now = time.time()
    W3 = "So11111111111111111111111111111111111111112"
    base = {"kind": "world", "pnl_usd": 20000, "roi": 60, "win_rate": .55, "src": ["X"], "green_days": 4}
    rows = {WALLET: dict(base, active_days=7, trades=420, last=now - 600),        # codziennie, ~60/dzień, przed chwilą
            W2: dict(base, pnl_usd=60000, active_days=2, green_days=2, trades=12, last=now - 4 * 86400),  # więcej zysku, ale rzadko
            W3: dict(base, active_days=7, trades=7 * 3000, last=now - 60)}          # bot
    for w, a in rows.items():
        s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, 0, ?, '🌍 X')", (w, js.dumps(a), int(now * 1000)))
    act = {r["wallet"]: r for r in LB.top(s, 7, "active", kind="world")}
    assert LB.top(s, 7, "active", kind="world")[0]["wallet"] == WALLET
    assert act[W3]["activity"] == 0 and act[WALLET]["activity"] > 90 and act[WALLET]["tpd"] == 60
    assert LB.top(s, 7, "score", kind="world")[0]["wallet"] == WALLET  # aktywność liczy się też w ocenie
    assert LB.rank_name("active", 2, 7) == "🔥 Top 2 aktywność 7d" and LB.is_generic("🔥 Top 2 aktywność 7d")


def test_leaderboard_combined_sorts(tmp_path):
    import json as js
    from copybot.sol import leaderboard as LB
    s, _, _ = ready(tmp_path)
    now = time.time()
    W3 = "So11111111111111111111111111111111111111112"
    base = {"kind": "world", "roi": 50, "win_rate": .5, "src": ["X"], "green_days": 4}
    rows = {WALLET: dict(base, pnl_usd=200000, active_days=2, trades=10, last=now - 3 * 86400),  # najlepsza ocena, mało aktywny
            W2: dict(base, pnl_usd=3000, active_days=7, trades=500, last=now - 60),           # bardzo aktywny, mały zysk
            W3: dict(base, pnl_usd=250000, active_days=7, trades=600, last=now - 600)}        # najlepszy w obu
    for w, a in rows.items():
        s.db._q("INSERT INTO leaderboard VALUES (?, 7, ?, 0, ?, '🌍 X')", (w, js.dumps(a), int(now * 1000)))
    both = LB.top(s, 7, "pnl,active", kind="world")
    assert both[0]["wallet"] == W3 and both[0]["combo"] == 100.0 and "combo" not in LB.top(s, 7, "pnl")[0]
    assert LB.top(s, 7, "zle,,active")[0]["wallet"] == W3 and len(LB.top(s, 7, "zle")) == 3  # nieznane pomija
    assert LB.rank_name("score,active", 3, 7) == "🏆🔥 Top 3 ocena+aktywność 7d"
    assert LB.is_generic("🏆🔥 Top 3 ocena+aktywność 7d")

    async def steps(call):
        st, j = await call(OWNER, "/api/sol/leaderboard?period=7&sort=pnl,active")
        assert st == 200 and j["rows"][0]["wallet"] == W3
    run(s, steps)
