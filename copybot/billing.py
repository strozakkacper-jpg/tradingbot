"""Subskrypcje w Telegram Stars."""
from telegram import Bot, LabeledPrice

from .config import Config
from .db import now_ms

PERIOD_SECONDS = 30 * 24 * 3600  # Telegram obsługuje tylko okres 30 dni


def paywall_on(cfg: Config) -> bool:
    return cfg.sub_price_stars > 0


def needs_subscription(cfg: Config, paper: bool) -> bool:
    return paywall_on(cfg) and (cfg.sub_scope == "all" or not paper)


def has_access(cfg: Config, u, paper: bool | None = None) -> bool:
    """Czy użytkownik może kopiować w danym trybie (domyślnie: w swoim obecnym)."""
    paper = bool(u["paper"]) if paper is None else paper
    return not needs_subscription(cfg, paper) or (u["paid_until"] or 0) > now_ms()


async def invoice_link(bot: Bot, cfg: Config, tg_id: int) -> str:
    return await bot.create_invoice_link(
        title="Subskrypcja Copy Trading",
        description="Automatyczne kopiowanie transakcji lidera przez 30 dni. Odnawia się co miesiąc, "
                    "możesz anulować w każdej chwili w ustawieniach Telegrama.",
        payload=f"sub:{tg_id}",
        provider_token="",  # Telegram Stars
        currency="XTR",
        prices=[LabeledPrice("30 dni", cfg.sub_price_stars)],
        subscription_period=PERIOD_SECONDS,
    )
