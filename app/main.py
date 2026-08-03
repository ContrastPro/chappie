import logging

from app.agent.trader import TraderAgent
from app.core.config import get_settings
from app.core.logger import configure_logging
from app.market.binance_provider import BinanceMarketProvider


def main() -> None:
    settings = get_settings()

    configure_logging(
        log_level=settings.log_level,
        log_directory=settings.log_directory,
    )

    logger = logging.getLogger(__name__)

    print("=" * 44)
    print("          CHAPPIE TRADER CORE")
    print("=" * 44)

    logger.info("Application initialization started")

    market_provider = BinanceMarketProvider(
        base_url=settings.binance_market_base_url,
    )

    agent = TraderAgent(
        settings=settings,
        market_provider=market_provider,
    )

    try:
        agent.start()
    except Exception:
        logger.exception("Fatal error during agent startup")
        raise
    finally:
        if agent.is_running:
            agent.stop()

        market_provider.close()

    logger.info("Application finished successfully")


if __name__ == "__main__":
    main()