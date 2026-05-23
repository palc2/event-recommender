import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from event_scheduler.agents import DeliveryAgent, IngestAgent, ParserAgent, RecommenderAgent
from event_scheduler.db import run_migrations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def tick_ingest():
    IngestAgent().tick()


def tick_parser():
    ParserAgent().tick()


def tick_recommender():
    RecommenderAgent().tick()


def tick_delivery():
    DeliveryAgent().tick()


def main():
    logger.info("Running ClickHouse migrations...")
    run_migrations()
    logger.info("Migrations complete.")

    scheduler = BlockingScheduler()
    scheduler.add_job(tick_ingest, "interval", hours=1, id="ingest")
    scheduler.add_job(tick_parser, "interval", minutes=15, id="parser")
    scheduler.add_job(tick_recommender, "interval", minutes=15, id="recommender")
    scheduler.add_job(tick_delivery, "interval", minutes=15, id="delivery")

    def shutdown(signum, frame):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Scheduler started. Agents: ingest(1h), parser(15m), recommender(15m), delivery(15m)")
    scheduler.start()


if __name__ == "__main__":
    main()
