"""
SQLAlchemy ORM models for PageKeeper database.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


def utc_now():
    return datetime.now(UTC)


class KosyncDocument(Base):
    """
    Model for raw KOSync documents (mirroring the official server's schema).
    This allows syncing unlinked documents between devices.
    """

    __tablename__ = "kosync_documents"

    document_hash = Column(String(32), primary_key=True)  # MD5 Hash from KOReader
    progress = Column(String(512), nullable=True)  # XPath / CFI
    percentage = Column(Numeric(10, 6), default=0)  # Decimal precision
    device = Column(String(128), nullable=True)
    device_id = Column(String(64), nullable=True)
    timestamp = Column(DateTime, nullable=True)

    # Bridge specific fields — linked_book_id is the canonical FK
    linked_abs_id = Column(String(255), nullable=True, index=True)
    linked_book_id = Column(Integer, ForeignKey("books.id"), nullable=True, index=True)
    first_seen = Column(DateTime, default=utc_now)
    last_updated = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Hash cache replacement fields
    filename = Column(String(500), nullable=True)
    source = Column(String(50), nullable=True)
    grimmory_id = Column(String(255), nullable=True, index=True)
    mtime = Column(Float, nullable=True)

    # Relationship to Book (optional)
    linked_book = relationship("Book", backref="kosync_documents", foreign_keys=[linked_book_id])

    def __init__(
        self,
        document_hash: str,
        progress: str = None,
        percentage: float = 0,
        device: str = None,
        device_id: str = None,
        timestamp: datetime = None,
        linked_abs_id: str = None,
        linked_book_id: int = None,
        filename: str = None,
        source: str = None,
        grimmory_id: str = None,
        mtime: float = None,
    ):
        self.document_hash = document_hash
        self.progress = progress
        self.percentage = percentage
        self.device = device
        self.device_id = device_id
        self.timestamp = timestamp
        self.linked_abs_id = linked_abs_id
        self.linked_book_id = linked_book_id
        self.filename = filename
        self.source = source
        self.grimmory_id = grimmory_id
        self.mtime = mtime
        self.first_seen = utc_now()
        self.last_updated = utc_now()

    def __repr__(self):
        return f"<KosyncDocument(hash='{self.document_hash}', pct={self.percentage})>"


class Book(Base):
    """
    Book model storing book metadata and mapping information.
    """

    __tablename__ = "books"

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), unique=True, nullable=True, index=True)
    title = Column(String(500))
    ebook_filename = Column(String(500), index=True)
    original_ebook_filename = Column(String(500))
    kosync_doc_id = Column(String(255), index=True)
    transcript_file = Column(String(500))
    status = Column(String(50), default="not_started", index=True)
    activity_flag = Column(sa.Boolean, default=False)
    duration = Column(Float)  # Duration in seconds from AudioBookShelf
    sync_mode = Column(String(20), default="audiobook")  # 'audiobook' or 'ebook_only'
    storyteller_uuid = Column(String(36), index=True, nullable=True)
    abs_ebook_item_id = Column(String(255), nullable=True)
    ebook_item_id = Column(String(255), nullable=True)
    custom_cover_url = Column(String(500), nullable=True)
    author = Column(String(500), nullable=True)
    subtitle = Column(String(500), nullable=True)
    title_override = Column(String(500), nullable=True)
    author_override = Column(String(500), nullable=True)
    grimmory_audio_source_id = Column(String(255), nullable=True, unique=True, index=True)

    # Reading tracker fields
    started_at = Column(String(10), nullable=True)  # YYYY-MM-DD
    finished_at = Column(String(10), nullable=True)  # YYYY-MM-DD
    rating = Column(Float, nullable=True)  # 0-5 (half-star increments)
    read_count = Column(Integer, default=1)

    # Relationships (all use book_id FK on child side)
    states = relationship("State", back_populates="book", cascade="all, delete-orphan", foreign_keys="State.book_id")
    jobs = relationship("Job", back_populates="book", cascade="all, delete-orphan", foreign_keys="Job.book_id")
    hardcover_details = relationship(
        "HardcoverDetails",
        back_populates="book",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="HardcoverDetails.book_id",
    )
    alignment = relationship(
        "BookAlignment",
        back_populates="book",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="BookAlignment.book_id",
    )
    reading_journals = relationship(
        "ReadingJournal", back_populates="book", cascade="all, delete-orphan", foreign_keys="ReadingJournal.book_id"
    )

    def __init__(
        self,
        abs_id: str = None,
        title: str = None,
        ebook_filename: str = None,
        original_ebook_filename: str = None,
        kosync_doc_id: str = None,
        transcript_file: str = None,
        status: str = "not_started",
        duration: float = None,
        sync_mode: str = "audiobook",
        storyteller_uuid: str = None,
        abs_ebook_item_id: str = None,
        ebook_item_id: str = None,
        custom_cover_url: str = None,
        author: str = None,
        subtitle: str = None,
        title_override: str = None,
        author_override: str = None,
        grimmory_audio_source_id: str = None,
        started_at: str = None,
        finished_at: str = None,
        rating: float = None,
        read_count: int = 1,
    ):
        self.abs_id = abs_id
        self.title = title
        self.ebook_filename = ebook_filename
        self.original_ebook_filename = original_ebook_filename
        self.kosync_doc_id = kosync_doc_id
        self.transcript_file = transcript_file
        self.status = status
        self.duration = duration
        self.sync_mode = sync_mode
        self.storyteller_uuid = storyteller_uuid
        self.abs_ebook_item_id = abs_ebook_item_id
        self.ebook_item_id = ebook_item_id
        self.custom_cover_url = custom_cover_url
        self.author = author
        self.subtitle = subtitle
        self.title_override = title_override
        self.author_override = author_override
        self.grimmory_audio_source_id = grimmory_audio_source_id
        self.started_at = started_at
        self.finished_at = finished_at
        self.rating = rating
        self.read_count = read_count

    @property
    def display_title(self):
        """Stored title plus override only (no Grimmory/ABS enrichment). Prefer reading APIs for UI strings."""
        return self.title_override or self.title or ""

    @property
    def display_author(self):
        """Stored author plus override only. Prefer reading APIs for detail hero author."""
        return self.author_override or self.author or ""

    def __repr__(self):
        return f"<Book(id={self.id}, abs_id='{self.abs_id}', title='{self.title}')>"


class ReadingJournal(Base):
    """Journal entries for reading activity — auto-generated on status transitions and user notes."""

    __tablename__ = "reading_journals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), nullable=False)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    event = Column(String(20), nullable=False)  # started|progress|finished|note|paused|resumed|dnf
    entry = Column(Text, nullable=True)  # freeform text note
    percentage = Column(Float, nullable=True)  # progress at time of entry
    created_at = Column(DateTime, default=utc_now)

    book = relationship("Book", back_populates="reading_journals", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id: str = None,
        event: str = None,
        entry: str = None,
        percentage: float = None,
        created_at=None,
        book_id: int = None,
    ):
        self.abs_id = abs_id or ""
        self.book_id = book_id
        self.event = event
        self.entry = entry
        self.percentage = percentage
        self.created_at = created_at or utc_now()

    def __repr__(self):
        return f"<ReadingJournal(id={self.id}, book_id={self.book_id}, event='{self.event}')>"


class ReadingGoal(Base):
    """Yearly reading goal — simple book count target."""

    __tablename__ = "reading_goals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, unique=True, nullable=False)
    target_books = Column(Integer, nullable=False)

    def __init__(self, year: int, target_books: int):
        self.year = year
        self.target_books = target_books

    def __repr__(self):
        return f"<ReadingGoal(year={self.year}, target={self.target_books})>"


class HardcoverDetails(Base):
    """
    HardcoverDetails model storing hardcover book matching information.
    """

    __tablename__ = "hardcover_details"

    id = Column(Integer, primary_key=True, autoincrement=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    abs_id = Column(String(255), nullable=True)
    hardcover_book_id = Column(String(255))
    hardcover_slug = Column(String(255))
    hardcover_edition_id = Column(String(255))
    hardcover_pages = Column(Integer)
    hardcover_audio_seconds = Column(Integer)
    isbn = Column(String(255))
    asin = Column(String(255))
    matched_by = Column(String(50))  # 'isbn', 'asin', 'title_author', 'title'
    hardcover_cover_url = Column(String(500), nullable=True)

    # Cached bidirectional sync columns
    hardcover_user_book_id = Column(Integer, nullable=True)
    hardcover_user_book_read_id = Column(Integer, nullable=True)
    hardcover_status_id = Column(Integer, nullable=True)
    hardcover_audio_edition_id = Column(String(255), nullable=True)

    # Relationship
    book = relationship("Book", back_populates="hardcover_details", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id: str = None,
        hardcover_book_id: str = None,
        hardcover_slug: str = None,
        hardcover_edition_id: str = None,
        hardcover_pages: int = None,
        hardcover_audio_seconds: int = None,
        isbn: str = None,
        asin: str = None,
        matched_by: str = None,
        hardcover_cover_url: str = None,
        hardcover_user_book_id: int = None,
        hardcover_user_book_read_id: int = None,
        hardcover_status_id: int = None,
        hardcover_audio_edition_id: str = None,
        book_id: int = None,
    ):
        self.abs_id = abs_id
        self.book_id = book_id
        self.hardcover_book_id = hardcover_book_id
        self.hardcover_slug = hardcover_slug
        self.hardcover_edition_id = hardcover_edition_id
        self.hardcover_pages = hardcover_pages
        self.hardcover_audio_seconds = hardcover_audio_seconds
        self.isbn = isbn
        self.asin = asin
        self.matched_by = matched_by
        self.hardcover_cover_url = hardcover_cover_url
        self.hardcover_user_book_id = hardcover_user_book_id
        self.hardcover_user_book_read_id = hardcover_user_book_read_id
        self.hardcover_status_id = hardcover_status_id
        self.hardcover_audio_edition_id = hardcover_audio_edition_id

    def __repr__(self):
        return f"<HardcoverDetails(book_id={self.book_id}, hardcover_book_id='{self.hardcover_book_id}')>"


class HardcoverSyncLog(Base):
    """Log of Hardcover sync actions for debugging and visibility."""

    __tablename__ = "hardcover_sync_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), nullable=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)
    book_title = Column(String(500), nullable=True)
    direction = Column(String(4), nullable=False)  # 'push' or 'pull'
    action = Column(String(30), nullable=False, index=True)
    detail = Column(Text, nullable=True)  # JSON blob
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, index=True)

    book = relationship("Book", backref="hardcover_sync_logs", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id=None,
        book_title=None,
        direction="push",
        action="",
        detail=None,
        success=True,
        error_message=None,
        created_at=None,
        book_id=None,
    ):
        self.abs_id = abs_id
        self.book_id = book_id
        self.book_title = book_title
        self.direction = direction
        self.action = action
        self.detail = detail
        self.success = success
        self.error_message = error_message
        self.created_at = created_at or utc_now()

    def __repr__(self):
        return f"<HardcoverSyncLog(id={self.id}, action='{self.action}', direction='{self.direction}')>"


class StorytellerSubmission(Base):
    """Tracks books submitted to Storyteller for narrated EPUB3 creation."""

    __tablename__ = "storyteller_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), nullable=False)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="queued")  # queued, processing, ready, failed
    submission_dir = Column(String(500), nullable=True)
    storyteller_uuid = Column(String(36), nullable=True)
    error = Column(Text, nullable=True)
    submitted_at = Column(DateTime, default=utc_now)
    last_checked_at = Column(DateTime, nullable=True)

    book = relationship("Book", backref="storyteller_submissions", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id: str = None,
        status: str = "queued",
        submission_dir: str = None,
        storyteller_uuid: str = None,
        error: str = None,
        book_id: int = None,
    ):
        self.abs_id = abs_id or ""
        self.book_id = book_id
        self.status = status
        self.submission_dir = submission_dir
        self.storyteller_uuid = storyteller_uuid
        self.error = error
        self.submitted_at = utc_now()

    def __repr__(self):
        return f"<StorytellerSubmission(id={self.id}, book_id={self.book_id}, status='{self.status}')>"


class State(Base):
    """
    State model storing sync state per book and client.
    """

    __tablename__ = "states"
    __table_args__ = (
        sa.Index(
            "uq_states_book_id_client_name",
            "book_id",
            "client_name",
            unique=True,
            sqlite_where=sa.text("book_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), nullable=False)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=True, index=True)
    client_name = Column(String(50), nullable=False)
    last_updated = Column(Float)
    percentage = Column(Float)
    timestamp = Column(Float)
    xpath = Column(Text)
    cfi = Column(Text)

    # Relationship
    book = relationship("Book", back_populates="states", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id: str = None,
        client_name: str = None,
        last_updated: float = None,
        percentage: float = None,
        timestamp: float = None,
        xpath: str = None,
        cfi: str = None,
        book_id: int = None,
    ):
        self.abs_id = abs_id or ""
        self.book_id = book_id
        self.client_name = client_name
        self.last_updated = last_updated
        self.percentage = percentage
        self.timestamp = timestamp
        self.xpath = xpath
        self.cfi = cfi

    def __repr__(self):
        return f"<State(book_id={self.book_id}, client='{self.client_name}', pct={self.percentage})>"


class Job(Base):
    """
    Job model storing job execution data for books.
    """

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), nullable=False)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True)
    last_attempt = Column(Float)
    retry_count = Column(Integer, default=0)
    last_error = Column(Text)
    progress = Column(Float, default=0.0)

    # Relationship
    book = relationship("Book", back_populates="jobs", foreign_keys=[book_id])

    def __init__(
        self,
        abs_id: str = None,
        last_attempt: float = None,
        retry_count: int = 0,
        last_error: str = None,
        progress: float = 0.0,
        book_id: int = None,
    ):
        self.abs_id = abs_id or ""
        self.book_id = book_id
        self.last_attempt = last_attempt
        self.retry_count = retry_count
        self.last_error = last_error
        self.progress = progress

    def __repr__(self):
        return f"<Job(book_id={self.book_id}, retries={self.retry_count})>"


class PendingSuggestion(Base):
    """
    Model for progress-triggered ebook suggestions.
    """

    __tablename__ = "pending_suggestions"
    __table_args__ = (
        sa.Index(
            "ix_pending_suggestions_source_id_source",
            "source_id",
            "source",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), default="abs")
    source_id = Column(String(255))
    title = Column(String(500))
    author = Column(String(500))
    cover_url = Column(String(500))
    matches_json = Column(Text)
    status = Column(String(20), default="pending", index=True)
    created_at = Column(DateTime, default=utc_now)

    def __init__(
        self,
        source_id: str,
        title: str,
        author: str = None,
        cover_url: str = None,
        matches_json: str = "[]",
        status: str = "pending",
        source: str = "abs",
    ):
        self.source = source
        self.source_id = source_id
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.matches_json = matches_json
        self.status = status
        self.created_at = utc_now()

    @property
    def matches(self):
        import json

        try:
            return json.loads(self.matches_json) if self.matches_json else []
        except json.JSONDecodeError:
            return []

    def __repr__(self):
        return f"<PendingSuggestion(id={self.id}, title='{self.title}', status='{self.status}')>"


class DetectedBook(Base):
    """Model for external books with real progress that are not yet in PageKeeper."""

    __tablename__ = "detected_books"
    __table_args__ = (UniqueConstraint("source_id", "source", name="uq_detected_books_source_id_source"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), default="abs", nullable=False)
    source_id = Column(String(255), nullable=False)
    title = Column(String(500), nullable=True)
    author = Column(String(500), nullable=True)
    cover_url = Column(String(500), nullable=True)
    progress_percentage = Column(Float, default=0.0, nullable=False)
    source_updated_at = Column(DateTime, nullable=True)
    first_detected_at = Column(DateTime, default=utc_now)
    last_seen_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    status = Column(String(20), default="detected")
    processing_token = Column(String(64), nullable=True)
    processing_started_at = Column(DateTime, nullable=True)
    matches_json = Column(Text, nullable=True)
    device = Column(String(128), nullable=True)
    ebook_filename = Column(String(500), nullable=True)
    media_format = Column(String(20), default="ebook", nullable=False)

    def __init__(
        self,
        source_id: str,
        title: str,
        progress_percentage: float,
        author: str = None,
        cover_url: str = None,
        status: str = "detected",
        source: str = "abs",
        matches_json: str = None,
        device: str = None,
        ebook_filename: str = None,
        source_updated_at=None,
        media_format: str = "ebook",
    ):
        self.source = source
        self.source_id = source_id
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.progress_percentage = progress_percentage
        self.status = status
        self.matches_json = matches_json
        self.device = device
        self.ebook_filename = ebook_filename
        self.source_updated_at = source_updated_at
        self.media_format = media_format
        self.first_detected_at = utc_now()
        self.last_seen_at = utc_now()

    @property
    def matches(self):
        import json

        try:
            return json.loads(self.matches_json) if self.matches_json else []
        except json.JSONDecodeError:
            return []

    def __repr__(self):
        return (
            f"<DetectedBook(id={self.id}, source='{self.source}', source_id='{self.source_id}', "
            f"status='{self.status}')>"
        )


class Setting(Base):
    """
    Setting model storing application configuration.
    """

    __tablename__ = "settings"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=True)

    def __init__(self, key: str, value: str = None):
        self.key = key
        self.value = value

    def __repr__(self):
        return f"<Setting(key='{self.key}', value='{self.value}')>"


class BookAlignment(Base):
    """
    Model for storing the computed alignment map for a book.
    Replaces legacy JSON files in transcripts/ directory.
    """

    __tablename__ = "book_alignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    abs_id = Column(String(255), nullable=True)
    alignment_map_json = Column(Text, nullable=False)  # JSON-encoded list of dicts or optimized structure
    source = Column(String(20), nullable=True)  # storyteller, smil, whisper
    last_updated = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship
    book = relationship("Book", back_populates="alignment", foreign_keys=[book_id])

    def __init__(self, abs_id: str = None, alignment_map_json: str = None, source: str = None, book_id: int = None):
        self.abs_id = abs_id
        self.book_id = book_id
        self.alignment_map_json = alignment_map_json
        self.source = source


class BookfusionHighlight(Base):
    """BookFusion highlight synced via the Obsidian API."""

    __tablename__ = "bookfusion_highlights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bookfusion_book_id = Column(String(255), nullable=False, index=True)
    highlight_id = Column(String(255), nullable=False, unique=True)
    book_title = Column(String(500))
    content = Column(Text, nullable=False)
    chapter_heading = Column(String(500))
    fetched_at = Column(DateTime, default=utc_now)
    highlighted_at = Column(DateTime, nullable=True, index=True)
    quote_text = Column(Text, nullable=True)
    matched_book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)

    matched_book = relationship("Book", foreign_keys=[matched_book_id])

    def __init__(
        self,
        bookfusion_book_id: str,
        highlight_id: str,
        content: str,
        book_title: str = None,
        chapter_heading: str = None,
        highlighted_at=None,
        quote_text: str = None,
        matched_book_id: int = None,
    ):
        self.bookfusion_book_id = bookfusion_book_id
        self.highlight_id = highlight_id
        self.content = content
        self.book_title = book_title
        self.chapter_heading = chapter_heading
        self.fetched_at = utc_now()
        self.highlighted_at = highlighted_at
        self.quote_text = quote_text
        self.matched_book_id = matched_book_id

    def __repr__(self):
        return f"<BookfusionHighlight(id={self.id}, book='{self.book_title}')>"


class BookfusionBook(Base):
    """BookFusion library catalog entry synced via the Obsidian API."""

    __tablename__ = "bookfusion_books"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bookfusion_id = Column(String(255), unique=True, nullable=False)
    title = Column(String(500))
    authors = Column(String(500))
    filename = Column(String(500))
    frontmatter = Column(Text)
    tags = Column(String(500))
    series = Column(String(500))
    highlight_count = Column(Integer, default=0, nullable=False, server_default="0")
    matched_book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)
    hidden = Column(Boolean, default=False, nullable=False, server_default="0")
    fetched_at = Column(DateTime, default=utc_now)
    last_updated = Column(DateTime, default=utc_now, onupdate=utc_now)

    matched_book = relationship("Book", foreign_keys=[matched_book_id])

    def __init__(
        self,
        bookfusion_id: str,
        title: str = None,
        authors: str = None,
        filename: str = None,
        frontmatter: str = None,
        tags: str = None,
        series: str = None,
        highlight_count: int = 0,
        matched_book_id: int = None,
        hidden: bool = False,
    ):
        self.bookfusion_id = bookfusion_id
        self.title = title
        self.authors = authors
        self.filename = filename
        self.frontmatter = frontmatter
        self.tags = tags
        self.series = series
        self.highlight_count = highlight_count
        self.matched_book_id = matched_book_id
        self.hidden = hidden
        self.fetched_at = utc_now()
        self.last_updated = utc_now()

    def __repr__(self):
        return f"<BookfusionBook(id={self.id}, title='{self.title}')>"


class TbrItem(Base):
    """A book on the user's To Be Read list. Separate from the Book table —
    a TBR item may not be owned yet."""

    __tablename__ = "tbr_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    author = Column(String(500), nullable=True)
    cover_url = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    priority = Column(Integer, default=0)
    added_at = Column(DateTime, default=utc_now)

    # Hardcover link (if sourced from or synced to HC)
    hardcover_book_id = Column(Integer, nullable=True, index=True)
    hardcover_slug = Column(String(255), nullable=True)

    # Source tracking: 'manual', 'open_library', 'hardcover_search', 'hardcover_wtr', 'hardcover_list'
    source = Column(String(50), default="manual")

    # Open Library link (if sourced from OL search)
    ol_work_key = Column(String(255), nullable=True)
    isbn = Column(String(20), nullable=True)

    # If imported from a Hardcover custom list
    hardcover_list_id = Column(Integer, nullable=True)
    hardcover_list_name = Column(String(500), nullable=True)

    # Enrichment metadata (populated from Hardcover, Open Library, or manual entry)
    description = Column(Text, nullable=True)
    page_count = Column(Integer, nullable=True)
    rating = Column(Float, nullable=True)
    ratings_count = Column(Integer, nullable=True)
    release_year = Column(Integer, nullable=True)
    genres = Column(Text, nullable=True)  # JSON-encoded list of strings
    subtitle = Column(String(500), nullable=True)

    # Link to owned Book (set when matched/acquired)
    book_abs_id = Column(String(255), nullable=True, index=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)
    book = relationship("Book", foreign_keys=[book_id])

    def __init__(
        self,
        title: str,
        author: str = None,
        cover_url: str = None,
        notes: str = None,
        priority: int = 0,
        source: str = "manual",
        hardcover_book_id: int = None,
        hardcover_slug: str = None,
        ol_work_key: str = None,
        isbn: str = None,
        hardcover_list_id: int = None,
        hardcover_list_name: str = None,
        book_abs_id: str = None,
        book_id: int = None,
        description: str = None,
        page_count: int = None,
        rating: float = None,
        ratings_count: int = None,
        release_year: int = None,
        genres: str = None,
        subtitle: str = None,
    ):
        self.title = title
        self.author = author
        self.cover_url = cover_url
        self.notes = notes
        self.priority = priority
        self.source = source
        self.hardcover_book_id = hardcover_book_id
        self.hardcover_slug = hardcover_slug
        self.ol_work_key = ol_work_key
        self.isbn = isbn
        self.hardcover_list_id = hardcover_list_id
        self.hardcover_list_name = hardcover_list_name
        self.book_abs_id = book_abs_id
        self.book_id = book_id
        self.description = description
        self.page_count = page_count
        self.rating = rating
        self.ratings_count = ratings_count
        self.release_year = release_year
        self.genres = genres
        self.subtitle = subtitle
        self.added_at = utc_now()

    def __repr__(self):
        return f"<TbrItem(id={self.id}, title='{self.title}')>"


class GrimmoryBook(Base):
    """
    Model for caching Grimmory search results, replacing local JSON cache.
    """

    __tablename__ = "grimmory_books"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(String(50), nullable=False, default="default")
    remote_book_id = Column(String(255), nullable=True)
    remote_file_id = Column(String(255), nullable=True)
    filename = Column(String(500), nullable=False)
    title = Column(String(500))
    authors = Column(String(500))
    raw_metadata = Column(Text)
    last_updated = Column(DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        sa.Index(
            "uq_grimmory_remote_file",
            "server_id",
            "remote_book_id",
            "remote_file_id",
            unique=True,
            sqlite_where=sa.text("remote_book_id IS NOT NULL AND remote_file_id IS NOT NULL"),
        ),
    )

    @property
    def raw_metadata_dict(self):
        import json

        try:
            return json.loads(self.raw_metadata) if self.raw_metadata else {}
        except json.JSONDecodeError:
            return {}

    def __init__(
        self,
        filename: str,
        title: str | None = None,
        authors: str | None = None,
        raw_metadata: str | None = None,
        server_id: str = "default",
        remote_book_id: str | None = None,
        remote_file_id: str | None = None,
    ):
        self.server_id = server_id
        self.remote_book_id = str(remote_book_id) if remote_book_id is not None else None
        self.remote_file_id = str(remote_file_id) if remote_file_id is not None else None
        self.filename = filename
        self.title = title
        self.authors = authors
        self.raw_metadata = raw_metadata


# Database configuration
class DatabaseManager:
    """
    Database manager handling SQLAlchemy engine and session management.
    """

    def __init__(self, db_path: str):
        import os

        self.db_path = os.path.abspath(db_path)
        # Increase timeout to reduce lock errors, enable WAL mode for concurrency, allow multi-thread access
        # Using 4 slashes guarantees an absolute path in SQLAlchemy
        self.engine = create_engine(
            f"sqlite:///{self.db_path}", echo=False, connect_args={"timeout": 30, "check_same_thread": False}
        )

        from sqlalchemy import event

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine)

        # Note: Schema creation is handled by Alembic migrations
        # No longer calling Base.metadata.create_all() here

    def get_session(self):
        """Get a new database session."""
        return self.SessionLocal()

    def close(self):
        """Close the database engine."""
        self.engine.dispose()
