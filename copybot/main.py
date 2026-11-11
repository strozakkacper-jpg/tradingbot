import asyncio
import datetime as dt
import logging
import warnings
from zoneinfo import ZoneInfo

from telegram import BotCommand, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

import aiohttp

from . import demo, mirror, traders
from .sol import admin as sol_admin, autocfg as sol_autocfg, autopilot as sol_autopilot, axiom, extras as sol_extras, leaderboard as sol_leaderboard, ui as sol_ui
from .sol.chain import Jito, Jupiter, PumpPortal, Rpc
from .sol.engine import SolEngine
from .sol.watcher import SolWatcher
from .live import live_job
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
    svc.bot = app.bot

    # --- Solana: kopiowanie portfeli (Axiom, Pump.fun…) – wspólna sesja HTTP = stałe połączenia, mniej ms ---
    if cfg.sol_enabled:
        session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20),
                                        connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300,
                                                                       keepalive_timeout=60))
        rpc = Rpc(cfg.sol_rpc, session, cfg.sol_rpc_rps)
        svc.sol = SolEngine(cfg, db, vault, rpc, Jupiter(cfg.jup_base, cfg.jupiter_api_key, session), engine.notify)
        sol_watcher = SolWatcher(cfg.sol_ws, cfg.sol_ws_enhanced, rpc, session, svc.sol.watched_wallets,
                                 svc.sol.on_swap)
        svc.sol.watcher = sol_watcher
        svc.sol.pump = PumpPortal(session)
        svc.sol.jito = Jito(cfg.jito_url, session) if cfg.jito_url else None
        app.bot_data.update(sol_session=session, sol_task=asyncio.create_task(sol_watcher.run()))
        log.info("Solana: RPC %s · Jupiter %s · %s", "Helius" if cfg.helius_api_key else "publiczny (wolniejszy)",
                 "z kluczem" if cfg.jupiter_api_key else "lite (bez klucza)",
                 "Enhanced WebSocket" if cfg.helius_api_key else "logsSubscribe")

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
    app.job_queue.run_repeating(live_job, interval=cfg.live_seconds, first=20)
    if cfg.sol_enabled:
        app.job_queue.run_repeating(axiom.tpsl_job, interval=60, first=45)
        app.job_queue.run_repeating(axiom.sol_equity_job, interval=15 * 60, first=120)
        app.job_queue.run_repeating(axiom.auto_rank_job, interval=6 * 3600, first=600)
        tz = ZoneInfo(cfg.timezone)
        app.job_queue.run_repeating(sol_extras.health_job, interval=300, first=240)
        app.job_queue.run_daily(sol_extras.backup_job, dt.time(4, 7, tzinfo=tz))
        app.job_queue.run_daily(sol_extras.report_job, dt.time(21, 3, tzinfo=tz))
        app.job_queue.run_daily(sol_extras.smart_job, dt.time(6, 11, tzinfo=tz))
        app.job_queue.run_repeating(sol_leaderboard.leaderboard_job, interval=6 * 3600, first=600)
        app.job_queue.run_repeating(sol_autocfg.autocfg_job, interval=30 * 60, first=180)
        app.job_queue.run_once(sol_leaderboard.name_everyone_once, 60)
        app.job_queue.run_repeating(sol_autopilot.autopilot_job, interval=30 * 60, first=1500)
    app.job_queue.run_daily(daily_report_job, dt.time(cfg.daily_report_hour, 0, tzinfo=ZoneInfo(cfg.timezone)))
    if cfg.webapp_url:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("📱 Aplikacja", WebAppInfo(f"{cfg.webapp_url}/app")))
    log.info("Giełdy: %s · traderzy aktywni: %d", ", ".join(cfg.exchanges), len(db.leaders("active")))
    await app.bot.set_my_commands([
        BotCommand("menu", "Pulpit"), BotCommand("portfel", "Dodaj portfel do kopiowania"),
        BotCommand("raport", "Raport sesji"),
        BotCommand("traderzy", "Traderzy"), BotCommand("dodaj", "Dodaj tradera"),
        BotCommand("sygnal", "Wyślij sygnał"), BotCommand("kup", "Kup (demo)"),
        BotCommand("sprzedaj", "Sprzedaj (demo)"), BotCommand("polacz", "Podłącz giełdę"),
        BotCommand("axiom", "Podłącz portfel z Axiom"), BotCommand("nazwa", "Ustaw swoją nazwę"),
        BotCommand("czat", "Czat grupowy"), BotCommand("eksport", "Historia transakcji (CSV)"),
        BotCommand("ustawpin", "Ustaw PIN do wypłat"), BotCommand("pin", "Odblokuj wypłatę PIN-em"),
        BotCommand("backup", "Kopia zapasowa (właściciel)"),
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
    if task := app.bot_data.get("sol_task"):
        task.cancel()
    if session := app.bot_data.get("sol_session"):
        await session.close()
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
    demo.register(app)
    sol_ui.register(app)
    axiom.register(app)
    sol_admin.register(app)
    sol_extras.register(app)
    register(app)
    axiom.register_paste(app)  # wklejony adres portfela/tokena – po wszystkich rozmowach
    app.run_polling(drop_pending_updates=False, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
