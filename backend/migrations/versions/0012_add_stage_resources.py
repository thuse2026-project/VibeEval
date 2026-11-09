"""Stage workspaces, file revisions and phase-scoped Agent ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stage_workspaces",
        sa.Column("workspace_id", sa.String(128), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("exam_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(128),
            sa.ForeignKey("exam_flow_attempts.attempt_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_id", sa.String(128), nullable=False, unique=True),
        sa.Column("spec_json", JSONB(), nullable=False),
        sa.Column("sandbox_resource_key", sa.String(64), nullable=False, unique=True),
        sa.Column("sandbox_image_ref", sa.String(512), nullable=False),
        sa.Column("sandbox_backend_ref", sa.String(128), nullable=False),
        sa.Column("sandbox_id", sa.String(255)),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_phase_id", sa.String(128)),
        sa.Column("permission_epoch", sa.BigInteger(), nullable=False),
        sa.Column("artifact_json", JSONB()),
    )
    op.create_table(
        "stage_file_revisions",
        sa.Column(
            "workspace_id",
            sa.String(128),
            sa.ForeignKey("stage_workspaces.workspace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.String(500), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
    )
    op.create_table(
        "agent_scopes",
        sa.Column("scope_id", sa.String(128), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("exam_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("phase_id", sa.String(128), unique=True),
        sa.Column(
            "workspace_id",
            sa.String(128),
            sa.ForeignKey("stage_workspaces.workspace_id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("permission_epoch", sa.BigInteger(), nullable=False),
    )
    op.execute(
        "INSERT INTO agent_scopes (scope_id,session_id,kind,status,permission_epoch) SELECT session_id,session_id,'legacy','active',0 FROM exam_sessions"
    )
    op.execute(
        "CREATE FUNCTION vibeeval_legacy_agent_scope() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO agent_scopes(scope_id,session_id,kind,status,permission_epoch) VALUES (NEW.session_id,NEW.session_id,'legacy','active',0); RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER exam_session_agent_scope AFTER INSERT ON exam_sessions FOR EACH ROW EXECUTE FUNCTION vibeeval_legacy_agent_scope()"
    )
    for table in ("agent_conversations", "agent_materials"):
        op.drop_constraint(
            f"fk_{table}_exam_session_id_exam_sessions", table, type_="foreignkey"
        )
        op.create_foreign_key(
            f"fk_{table}_exam_session_id_agent_scopes",
            table,
            "agent_scopes",
            ["exam_session_id"],
            ["scope_id"],
            ondelete="CASCADE",
        )


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_scopes WHERE kind='phase') THEN RAISE EXCEPTION 'Archive phase Agent scopes before downgrade'; END IF; END $$"
    )
    for table in ("agent_conversations", "agent_materials"):
        op.drop_constraint(
            f"fk_{table}_exam_session_id_agent_scopes", table, type_="foreignkey"
        )
        op.create_foreign_key(
            f"fk_{table}_exam_session_id_exam_sessions",
            table,
            "exam_sessions",
            ["exam_session_id"],
            ["session_id"],
            ondelete="CASCADE",
        )
    op.execute("DROP TRIGGER exam_session_agent_scope ON exam_sessions")
    op.execute("DROP FUNCTION vibeeval_legacy_agent_scope()")
    op.drop_table("agent_scopes")
    op.drop_table("stage_file_revisions")
    op.drop_table("stage_workspaces")
