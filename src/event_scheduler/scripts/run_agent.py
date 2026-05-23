"""CLI to run a single agent tick manually."""
import argparse
import logging

from event_scheduler.agents import DeliveryAgent, IngestAgent, ParserAgent, RecommenderAgent
from event_scheduler.db import run_migrations

AGENTS = {
    "ingest": IngestAgent,
    "parser": ParserAgent,
    "recommender": RecommenderAgent,
    "delivery": DeliveryAgent,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="Run a single agent tick")
    parser.add_argument("agent", choices=AGENTS.keys(), help="Agent to run")
    parser.add_argument("--migrate", action="store_true", help="Run migrations first")
    args = parser.parse_args()

    if args.migrate:
        run_migrations()

    agent = AGENTS[args.agent]()
    agent.tick()


if __name__ == "__main__":
    main()
