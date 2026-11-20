"""Advance R2 runs independently of browser connections, using existing leases."""

import asyncio
import logging
import time

from sqlalchemy import and_, exists, or_, select

from app.db.models import AgentInteraction, AgentRun, AgentScope
from app.exam_flow.types import FlowError

from .run_store import RunStore
from .runtime_types import RunBusy
from .store import ConversationNotFound

logger = logging.getLogger(__name__)


class StageAgentWorker:
    def __init__(self, resources, *, concurrency=4, clock=time.time):
        self.resources = resources
        self.store = RunStore(resources.factory, clock=clock)
        self.clock = clock
        self.concurrency = concurrency

    def runnable(self, excluded, limit):
        now = int(self.clock())
        expired_interaction = exists().where(
            AgentInteraction.run_id == AgentRun.run_id,
            AgentInteraction.status.in_(("pending", "expired")),
            AgentInteraction.expires_at <= now,
        )
        with self.resources.factory() as db:
            return db.execute(
                select(
                    AgentRun.exam_session_id, AgentRun.conversation_id, AgentRun.run_id
                )
                .join(AgentScope, AgentScope.scope_id == AgentRun.exam_session_id)
                .where(
                    AgentScope.kind == "phase",
                    AgentScope.status == "active",
                    AgentRun.run_id.not_in(excluded),
                    or_(
                        AgentRun.status == "queued",
                        and_(
                            AgentRun.status == "waiting_operation",
                            AgentRun.updated_at < now,
                        ),
                        and_(
                            AgentRun.status == "running",
                            or_(
                                AgentRun.lease_expires_at.is_(None),
                                AgentRun.lease_expires_at <= now,
                            ),
                        ),
                        and_(
                            AgentRun.status.in_(
                                ("waiting_approval", "waiting_user_input")
                            ),
                            expired_interaction,
                        ),
                    ),
                )
                .order_by(AgentRun.updated_at, AgentRun.run_id)
                .limit(limit)
            ).all()

    def _advance(self, scope, conversation, run):
        try:
            runtime = self.resources.runtime_for(scope)
            asyncio.run(runtime.advance(scope, conversation, run))
        except (RunBusy, ConversationNotFound):
            return
        except FlowError as error:
            if error.code in (
                "PHASE_CLOSED",
                "AUTHORIZATION_REVOKED",
                "CAPABILITY_DENIED",
            ):
                self.store.stop(scope, conversation, run, "expired", error.code)
            else:
                raise

    async def serve(self, stop):
        jobs = {}
        try:
            while not stop.is_set():
                for run, task in list(jobs.items()):
                    if task.done():
                        jobs.pop(run)
                        if task.exception() is not None:
                            logger.error(
                                "Agent run %s could not advance: %s",
                                run,
                                type(task.exception()).__name__,
                            )
                slots = self.concurrency - len(jobs)
                if slots:
                    try:
                        rows = await asyncio.to_thread(
                            self.runnable, tuple(jobs), slots
                        )
                    except Exception as error:
                        logger.warning(
                            "Agent queue could not be read: %s", type(error).__name__
                        )
                        rows = ()
                    if not stop.is_set():
                        for scope, conversation, run in rows:
                            jobs[run] = asyncio.create_task(
                                asyncio.to_thread(
                                    self._advance, scope, conversation, run
                                )
                            )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            await asyncio.gather(*jobs.values(), return_exceptions=True)
