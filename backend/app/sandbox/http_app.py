"""Explicit opt-in application factory; no changes to shared default entrypoint."""


def create_app():
    from app.main import create_app as shared_create_app
    from app.sandbox.runtime import create_submission_sandbox
    from app.workspace.router import router

    app = shared_create_app(submission_sandbox=create_submission_sandbox())
    app.include_router(router)
    return app
