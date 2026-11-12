"""Make audit_logs append-only via triggers.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
               "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;")
    op.execute("CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs "
               "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete")
