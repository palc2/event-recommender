import logging
import traceback
from abc import ABC, abstractmethod
from datetime import datetime
from uuid import uuid4

from event_scheduler.db import get_client
from event_scheduler.models import AgentRun

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: str = "base"

    def tick(self) -> None:
        client = get_client()
        run = AgentRun(
            run_id=uuid4(),
            agent_name=self.name,
            started_at=datetime.now(),
        )
        try:
            processed, failed = self._execute(client)
            run.items_processed = processed
            run.items_failed = failed
            run.completed_at = datetime.now()
            logger.info(
                "%s tick done: %d processed, %d failed",
                self.name, processed, failed,
            )
        except Exception as exc:
            run.error_message = traceback.format_exc()
            run.completed_at = datetime.now()
            logger.exception("%s tick failed: %s", self.name, exc)
        finally:
            client.insert(
                "agent_runs",
                [[
                    str(run.run_id),
                    run.agent_name,
                    run.started_at,
                    run.completed_at,
                    run.items_processed,
                    run.items_failed,
                    run.error_message,
                ]],
                column_names=[
                    "run_id", "agent_name", "started_at", "completed_at",
                    "items_processed", "items_failed", "error_message",
                ],
            )

    @abstractmethod
    def _execute(self, client) -> tuple[int, int]:
        """Run one tick of work. Returns (items_processed, items_failed)."""
        ...
