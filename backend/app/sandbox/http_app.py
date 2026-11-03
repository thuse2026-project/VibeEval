"""Explicit opt-in application factory; no changes to shared default entrypoint."""


def create_app():
    from app.main import create_app as shared_create_app
    from app.sandbox.runtime import create_submission_sandbox
    from app.sandbox.terminal import DockerTerminalFactory
    from app.workspace.router import router
    from app.workspace.terminal import router as terminal_router

    runtime = create_submission_sandbox()
    app = shared_create_app(submission_sandbox=runtime)
    app.state.terminal_factory = DockerTerminalFactory(runtime.settings)
    app.include_router(router)
    app.include_router(terminal_router)
    return app
