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
    assert res["skipped"] == 1 and any("🛡 ryzykowny token" in t and "zamrozić" in t for _, t in sent)
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
    assert s.sol.paper(OWNER)[TOKEN] == 250_000_000 and any("Zwrot wkładu" in t for _, t in sent)
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
