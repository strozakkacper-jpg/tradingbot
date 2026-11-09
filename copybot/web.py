"""Serwer panelu (Telegram Mini App): strona + API zabezpieczone podpisem Telegrama."""
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

from .billing import has_access, paywall_on
from .config import exchange_name
from .engine import user_exchange
from .reports import leader_report, ranking, user_report
from .stats import downsample

log = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).parent / "webapp"


def verify_init_data(init_data: str, bot_token: str, max_age: int = 24 * 3600) -> dict | None:
    """Sprawdza podpis initData z Telegram WebApp. Zwraca dane użytkownika albo None."""
    data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(data.get("auth_date", 0)) > max_age:
        return None
    try:
        return json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        return None


def build_app(s) -> web.Application:
    async def index(_):
        page = "sol.html" if getattr(s, "sol", None) else "index.html"
        return web.FileResponse(WEBAPP_DIR / page, headers={"Cache-Control": "no-cache"})

    async def index_cex(_):
        return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    def auth(request: web.Request):
        tg = verify_init_data(request.headers.get("X-Init-Data", ""), s.cfg.bot_token)
        if not tg or "id" not in tg:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "Otwórz panel z poziomu bota w Telegramie."}),
                                       content_type="application/json")
        u = s.db.get_user(int(tg["id"]))
        if not u or not u["accepted"] or not u["approved"]:
            raise web.HTTPForbidden(text=json.dumps({"error": "Najpierw napisz /start do bota."}),
                                    content_type="application/json")
        return u

    async def api_data(request: web.Request):
        u = auth(request)
        try:
            me = await user_report(s, u)
        except Exception as e:  # noqa: BLE001
            log.exception("Panel: portfel %s", u["tg_id"])
            me = {"error": f"Nie udało się pobrać salda: {str(e)[:120]}"}
        copied = s.db.copied_leaders(u["tg_id"])
        mine = s.db.follows_of(u["tg_id"])
        lead = copied[0] if copied else None
        if lead:
            try:
                leader = dict(await leader_report(s, lead["id"]), name=lead["name"], bio=lead["bio"],
                              source=lead["source"],
                              exchange=exchange_name(user_exchange(s.cfg, lead)) if lead["source"] == "api" else None)
            except Exception:  # noqa: BLE001
                log.exception("Panel: trader")
                leader = {"error": "Brak danych o traderze."}
        else:
            leader = {"error": "Nie kopiujesz jeszcze żadnego tradera – zobacz zakładkę Ranking."}
        try:
            board = [{"id": ld["id"], "name": ld["name"], "bio": ld["bio"], "total": r["total"], "d30": r["d30"],
                      "max_dd": r["max_dd"], "win_rate": r["win_rate"], "closed": r["closed"], "copying": c[1],
                      "mine": mine.get(ld["id"]) is True, "spark": downsample(r["series"], 40)}
                     for ld, r, c in await ranking(s)]
        except Exception:  # noqa: BLE001
            log.exception("Panel: ranking")
            board = []
        return web.json_response({
            "quote": s.cfg.quote, "exchange": exchange_name(user_exchange(s.cfg, u)), "me": me, "leader": leader, "ranking": board,
            "subscription": {"required": paywall_on(s.cfg), "ok": has_access(s.cfg, u),
                             "paid_until": u["paid_until"], "price": s.cfg.sub_price_stars},
        })

    async def api_follow(request: web.Request):
        u = auth(request)
        try:
            lid = int((await request.json())["leader_id"])
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Złe zapytanie"}, status=400)
        lead = s.db.get_leader(lid)
        if not lead or lead["status"] != "active":
            return web.json_response({"error": "Ten trader jest niedostępny"}, status=404)
        s.db.follow(u["tg_id"], lid, True)
        return web.json_response({"ok": True, "name": lead["name"], "active": bool(u["active"])})

    async def root(_):
        raise web.HTTPFound("/app")

    async def health(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/app", index)
    app.router.add_get("/app/cex", index_cex)
    if getattr(s, "sol", None):
        from .sol import api as sol_api
        sol_api.routes(s, app, auth)
    app.router.add_get("/api/data", api_data)
    app.router.add_post("/api/follow", api_follow)
    app.router.add_get("/health", health)
    return app


async def start_web(s) -> web.AppRunner:
    runner = web.AppRunner(build_app(s), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, s.cfg.web_host, s.cfg.web_port).start()
    log.info("Panel: http://%s:%d/app (publicznie: %s/app)", s.cfg.web_host, s.cfg.web_port, s.cfg.webapp_url or "—")
    return runner
