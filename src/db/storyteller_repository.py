"""Repository for Storyteller submission tracking."""

import logging

from sqlalchemy import func

from .base_repository import BaseRepository
from .models import StorytellerSubmission

logger = logging.getLogger(__name__)


class StorytellerRepository(BaseRepository):
    def save_storyteller_submission(self, submission):
        """Save a storyteller submission. Supersedes any existing active submission for the same book."""
        with self.get_session() as session:
            # Use book_id if available, else fall back to abs_id
            if submission.book_id:
                filt = StorytellerSubmission.book_id == submission.book_id
            else:
                filt = StorytellerSubmission.abs_id == submission.abs_id
            session.query(StorytellerSubmission).filter(
                filt,
                StorytellerSubmission.status.in_(["queued", "processing"]),
            ).update({StorytellerSubmission.status: "superseded"}, synchronize_session=False)
            session.add(submission)
            session.flush()
            session.refresh(submission)
            session.expunge(submission)
            return submission

    def get_active_storyteller_submission_by_book_id(self, book_id):
        with self.get_session() as session:
            query = (
                session.query(StorytellerSubmission)
                .filter(
                    StorytellerSubmission.book_id == book_id,
                    StorytellerSubmission.status.in_(["queued", "processing"]),
                )
                .order_by(StorytellerSubmission.submitted_at.desc())
            )
            return self._query_and_expunge(session, query, one=True)

    def update_storyteller_submission_status(
        self, submission_id, status, last_checked_at=None, storyteller_uuid=None, submission_dir=None
    ):
        """Update an existing submission's status without creating a new record."""
        with self.get_session() as session:
            sub = session.query(StorytellerSubmission).filter(StorytellerSubmission.id == submission_id).first()
            if sub:
                sub.status = status
                if last_checked_at is not None:
                    sub.last_checked_at = last_checked_at
                if storyteller_uuid is not None:
                    sub.storyteller_uuid = storyteller_uuid
                if submission_dir is not None:
                    sub.submission_dir = submission_dir

    def get_storyteller_submission_by_book_id(self, book_id):
        with self.get_session() as session:
            query = (
                session.query(StorytellerSubmission)
                .filter(StorytellerSubmission.book_id == book_id)
                .order_by(StorytellerSubmission.submitted_at.desc())
            )
            return self._query_and_expunge(session, query, one=True)

    def get_all_storyteller_submissions_latest(self):
        """Get the most recent submission per book (for dashboard bulk display).

        Returns a dict of {book_id: StorytellerSubmission}.
        """
        with self.get_session() as session:
            latest = (
                session.query(
                    StorytellerSubmission.book_id,
                    func.max(StorytellerSubmission.submitted_at).label("max_ts"),
                )
                .filter(StorytellerSubmission.book_id.isnot(None))
                .group_by(StorytellerSubmission.book_id)
                .subquery()
            )

            query = (
                session.query(StorytellerSubmission)
                .join(
                    latest,
                    (StorytellerSubmission.book_id == latest.c.book_id)
                    & (StorytellerSubmission.submitted_at == latest.c.max_ts),
                )
            )
            rows = self._query_and_expunge(session, query, one=False)

            result = {}
            for sub in rows:
                result[sub.book_id] = sub
            return result
