import asyncio
import datetime as dt
import logging
import warnings
from zoneinfo import ZoneInfo

from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

from . import mirror, traders
from .bot import Services, make_notify, register
from .config import Config
from .db import DB
from .engine import CopyEngine, Markets
from .jobs import daily_report_job, snapshot_job
from .security import Vault
from .watchers import Watchers
from .web import start_web

warnings.filterwarnings("ignore", category=PTBUserWarning)
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("copybot")


def first_leader_factory(cfg: Config, db: DB, vault: Vault):
    """Migracja z wersji 1: lider z .env staje się traderem #1."""
    def make() -> int:
        owner = db.get_state("owner")
        if cfg.leader_enabled:
            return db.create_leader("Trader #1", "api", int(owner) if owner else None, "active",
                                    api_key=vault.enc(cfg.leader_api_key), api_secret=vault.enc(cfg.leader_api_secret),
                                    api_password=vault.enc(cfg.leader_api_password or None),
                                    exchange=cfg.exchange_id)
        return db.create_leader("Sygnały admina", "manual", int(owner) if owner else None, "active",
                                bio="Sygnały wysyłane ręcznie przez administratora.")
    return make


async def post_init(app: Application):
    cfg: Config = app.bot_data["cfg"]
    db = DB(cfg.db_path)
    vault = Vault(cfg.encryption_key)
    if (lid := db.migrate_v2(first_leader_factory(cfg, db, vault))) is not None:
        log.info("Utworzono tradera #%s (migracja z wersji 1)", lid)
    markets = Markets(cfg)
    engine = CopyEngine(cfg, db, vault, markets, await make_notify(app))

    async def on_orders(leader_id: int, pairs):
        if db.get_state("paused") == "1":
            return
        for order, fraction in pairs:
            res = await engine.copy(order, fraction, leader_id)
            log.info("Trader #%s %s %s (%.2f%%): %s", leader_id, order.side, order.symbol, fraction * 100, dict(res))

    watchers = Watchers(cfg, db, vault, markets, on_orders)
    svc = app.bot_data["svc"] = Services(cfg, db, vault, markets, engine, watchers)

    async def after_trade(leader_id: int):  # lustro: wyrównaj portfele zaraz po ruchu tradera
        for u in db.mirror_followers(leader_id):
            asyncio.create_task(mirror.sync(svc, u, "ruch tradera"))
    engine.after_trade = after_trade
    # serwer www (i /health dla hostingu) startuje od razu – zanim zaczniemy łączyć się z giełdami
    try:
        app.bot_data["web"] = await start_web(svc)
    except OSError:
        log.exception("Nie uruchomiono serwera panelu na porcie %d", cfg.web_port)
    try:
        await asyncio.wait_for(markets.load(), 30)
    except Exception:  # noqa: BLE001 – giełda chwilowo niedostępna; spróbujemy przy pierwszym użyciu
        log.warning("Nie udało się pobrać listy rynków z %s – spróbuję przy pierwszym użyciu", cfg.exchange_id)
    for lead in db.leaders("active"):
        db.ensure_reference(lead["id"], cfg.paper_start, cfg.quote)
    watchers.sync()

    app.job_queue.run_repeating(snapshot_job, interval=cfg.snapshot_minutes * 60, first=60)
    app.job_queue.run_repeating(mirror_job, interval=cfg.mirror_minutes * 60, first=90)
    app.job_queue.run_daily(daily_report_job, dt.time(cfg.daily_report_hour, 0, tzinfo=ZoneInfo(cfg.timezone)))
    if cfg.webapp_url:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Panel", WebAppInfo(f"{cfg.webapp_url}/app")))
    log.info("Giełdy: %s · traderzy aktywni: %d", ", ".join(cfg.exchanges), len(db.leaders("active")))
    await app.bot.set_my_commands([
        BotCommand("menu", "Pulpit"), BotCommand("traderzy", "Traderzy"), BotCommand("dodaj", "Dodaj tradera"),
        BotCommand("sygnal", "Wyślij sygnał"), BotCommand("polacz", "Podłącz giełdę"),
        BotCommand("pomoc", "Jak to działa"),
    ])


async def mirror_job(ctx):
    """Lustro: regularna kontrola zgodności (nadrabia wpłaty, przerwy, pominięte ruchy)."""
    s: Services = ctx.application.bot_data["svc"]
    if s.db.get_state("paused") == "1":
        return
    for u in s.db.mirror_followers():
        try:
            await mirror.sync(s, u, "kontrola okresowa")
        except Exception:  # noqa: BLE001
            log.exception("Lustro: kontrola %s", u["tg_id"])


async def post_shutdown(app: Application):
    if web := app.bot_data.get("web"):
        await web.cleanup()
    s: Services | None = app.bot_data.get("svc")
    if s:
        if s.watchers:
            await s.watchers.close()
        await s.market.close()


def main():
    cfg = Config.from_env()
    app = (Application.builder().token(cfg.bot_token)
           # wielu użytkowników naraz: wolna giełda jednej osoby nie blokuje reszty
           .concurrent_updates(32)
           .connection_pool_size(64).pool_timeout(15)
           .connect_timeout(10).read_timeout(20).write_timeout(20)
           .get_updates_read_timeout(30).get_updates_connect_timeout(15).get_updates_pool_timeout(15)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data["cfg"] = cfg
    traders.register(app)  # najpierw – handlery bota łapią wszystkie pozostałe przyciski
    register(app)
    app.run_polling(drop_pending_updates=False, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
