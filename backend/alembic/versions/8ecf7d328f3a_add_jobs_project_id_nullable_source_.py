"""add jobs.project_id, nullable source_video_id, segment_code

Revision ID: 8ecf7d328f3a
Revises: c01adcb2c1a8
Create Date: 2026-10-10 15:53:45.018299

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ecf7d328f3a"
down_revision: str | None = "c01adcb2c1a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dialogue_lines", sa.Column("segment_code", sa.String(), nullable=True))

    # jobs.project_id: added nullable first, backfilled, then made NOT NULL —
    # cannot add a NOT NULL column directly to a table with existing rows (see
    # backend/AGENTS.md, same reasoning already applied to jobs.timeline_edits
    # in c01adcb2c1a8_add_timeline_edits_and_exported_video_.py). Backfilled
    # via the existing source_video_id -> SourceVideo.project_id path, which
    # is valid for every job that predates this migration (every job created
    # before specs/003-script-first-ingestion always had a source_video_id).
    op.add_column("jobs", sa.Column("project_id", sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE jobs
        SET project_id = source_videos.project_id
        FROM source_videos
        WHERE source_videos.id = jobs.source_video_id
        """
    )
    op.alter_column("jobs", "project_id", existing_type=sa.UUID(), nullable=False)

    op.create_index("idx_jobs_project", "jobs", ["project_id"], unique=False)
    op.create_foreign_key("fk_jobs_project_id_projects", "jobs", "projects", ["project_id"], ["id"])

    # NULL now means "script-first job, no video attached yet"
    # (specs/003-script-first-ingestion/spec.md) — every pre-existing job
    # keeps its real source_video_id untouched; this only loosens the
    # constraint for new rows going forward.
    op.alter_column("jobs", "source_video_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    # NOTE: this will fail if any job created after the upgrade has
    # source_video_id=NULL (a script-first job with no video attached yet) —
    # downgrading past the point where NULL became valid data is not
    # guaranteed to be safe, same as any schema change that loosens a
    # constraint new rows then rely on.
    op.alter_column("jobs", "source_video_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint("fk_jobs_project_id_projects", "jobs", type_="foreignkey")
    op.drop_index("idx_jobs_project", table_name="jobs")
    op.drop_column("jobs", "project_id")
    op.drop_column("dialogue_lines", "segment_code")
