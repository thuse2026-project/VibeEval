"""Server integration helpers; callers provide their questions and resource adapter.

Import these functions from your deployment assembly. No sample exam is authored,
no frontend is started, and no adapter is silently replaced with a simulation.
"""

from pathlib import Path
from threading import Event

from app.db.session import get_session_factory
from app.exam_flow import ExamDefinition, ExamFlowService, PostgresFlowStore
from app.exams.postgres import PostgresEntryStore
from app.main import create_app


def build_stage_app(
    questions,
    specs,
    *,
    factory=None,
    settings=None,
    client=None,
    snapshots=None,
    sandbox=None,
    accounts=None,
):
    """Complete stage resource assembly. Model secrets remain in server settings."""
    import os

    from app.stage_resources import StageResources, StageSandboxRuntime

    factory = factory or get_session_factory()
    if sandbox is None:
        sandbox = StageSandboxRuntime(
            backend_ref=os.environ["SANDBOX_BACKEND_REF"],
            docker_url=os.environ.get(
                "SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock"
            ),
        )
    resources = StageResources(
        factory, specs, sandbox, snapshots=snapshots, settings=settings, client=client
    )
    return build_app(questions, resources, factory=factory, accounts=accounts)


def build_app(questions, resources, *, factory=None, accounts=None):
    factory = factory or get_session_factory()
    flow = ExamFlowService(PostgresFlowStore(factory), questions, resources)
    return create_app(
        accounts=accounts, entry_store=PostgresEntryStore(factory), exam_flow=flow
    )


def attach_definition(flow: ExamFlowService, session_id: str, config_path: str | Path):
    """Trusted provisioner calls this after admission, before any legacy start."""
    definition = ExamDefinition.model_validate_json(
        Path(config_path).read_text(encoding="utf-8-sig")
    )
    return flow.attach(session_id, definition)


def maintain(flow: ExamFlowService, stop: Event, *, interval_seconds: float = 1):
    """Run in the host's supervised maintenance process, not a request thread."""
    if not 0 < interval_seconds <= 60:
        raise ValueError("maintenance interval must be between 0 and 60 seconds")
    while not stop.is_set():
        result = flow.run_due()
        if result["failures"]:
            # Let the process supervisor surface/restart a failed sweep; no
            # exception/credential text from resource adapters is logged here.
            raise RuntimeError("Some examination flows require maintenance retry")
        stop.wait(interval_seconds)
