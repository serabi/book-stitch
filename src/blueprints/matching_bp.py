"""Matching blueprint — suggestions, single match, batch match."""

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup

from src.blueprints.helpers import (
    _grimmory_label,
    any_grimmory_configured,
    attempt_hardcover_automatch,
    audiobook_matches_search,
    find_in_grimmory,
    get_abs_service,
    get_audiobook_author,
    get_audiobooks_conditionally,
    get_container,
    get_database_service,
    get_ebook_dir,
    get_kosync_id_for_ebook,
    get_manager,
    get_searchable_ebooks,
    serialize_suggestion,
)
from src.services.book_intake_service import BookIntakeService
from src.utils.logging_utils import sanitize_log_data
from src.utils.path_utils import sanitize_filename

logger = logging.getLogger(__name__)

_RECENT_ACTIVITY_CUTOFF = timedelta(days=30)

_AUDIOBOOK_REVIEW_SOURCES = frozenset({"abs", "grimmory"})
_EBOOK_REVIEW_SOURCES = frozenset({"grimmory", "kosync"})
_EBOOK_MATCH_SOURCES = frozenset({"grimmory", "kosync", "filesystem", "cwa", "abs_ebook"})
_AUDIO_MATCH_SOURCES = frozenset({"abs", "abs_audiobook", "grimmory"})

matching_bp = Blueprint("matching", __name__)


def _escape_template_value(value):
    return Markup.escape(value or "")


def _redirect_search_value():
    """Return a bounded same-route search value for redirects."""
    return (request.form.get("search") or "")[:200]


def _plain_error_response(message, status_code):
    return Response(message or "Request failed", status=status_code, mimetype="text/plain")


def _copy_book_merge_metadata(existing_book, overrides=None):
    return BookIntakeService._copy_book_merge_metadata(existing_book, overrides)


def _get_book_intake_service(container=None):
    container = container or get_container()
    return BookIntakeService(
        container=container,
        database_service=get_database_service(),
        abs_service=get_abs_service(),
        collection_name=current_app.config["ABS_COLLECTION_NAME"],
        books_dir=current_app.config.get("BOOKS_DIR", ""),
        epub_cache_dir=current_app.config.get("EPUB_CACHE_DIR", ""),
        find_in_grimmory=find_in_grimmory,
        get_kosync_id_for_ebook=get_kosync_id_for_ebook,
        attempt_hardcover_automatch=attempt_hardcover_automatch,
    )


def _create_book_mapping(
    container,
    abs_id,
    title,
    ebook_filename,
    duration,
    ebook_source_id=None,
    storyteller_uuid=None,
    storyteller_submit=False,
    author=None,
    subtitle=None,
):
    """Compatibility wrapper around the Book Intake Module."""
    result = _get_book_intake_service(container).map_audiobook_ebook(
        abs_id=abs_id,
        title=title,
        ebook_filename=ebook_filename,
        ebook_source_id=ebook_source_id,
        duration=duration,
        storyteller_uuid=storyteller_uuid,
        storyteller_submit=storyteller_submit,
        author=author,
        subtitle=subtitle,
    )
    return result.book, result.error


def _build_batch_queue_item(item):
    """Annotate queue entries with display-oriented fields without mutating session data."""
    ebook_label = item.get("ebook_display_name") or item.get("ebook_filename") or "Not selected"
    storyteller_selected = bool(item.get("storyteller_uuid"))
    storyteller_label = "Selected" if storyteller_selected else "None / Skip"

    if item.get("audio_only"):
        status_label = "Audio Only"
        status_kind = "audio-only"
    elif item.get("ebook_only"):
        status_label = "Ebook Only"
        status_kind = "ebook-only"
    elif item.get("abs_id") and item.get("ebook_filename"):
        status_label = "Ready"
        status_kind = "ready"
    else:
        status_label = "Incomplete"
        status_kind = "incomplete"

    return {
        **item,
        "ebook_label": ebook_label,
        "storyteller_label": storyteller_label,
        "storyteller_selected": storyteller_selected,
        "status_label": status_label,
        "status_kind": status_kind,
    }


def _build_batch_queue_view(queue):
    queue_items = [_build_batch_queue_item(item) for item in queue]
    return {
        "items": queue_items,
        "total_count": len(queue_items),
        "ready_count": sum(1 for item in queue_items if item["status_kind"] in {"ready", "audio-only", "ebook-only"}),
        "audio_only_count": sum(1 for item in queue_items if item["status_kind"] == "audio-only"),
        "ebook_only_count": sum(1 for item in queue_items if item["status_kind"] == "ebook-only"),
        "incomplete_count": sum(1 for item in queue_items if item["status_kind"] == "incomplete"),
    }


_SOURCE_LABELS = {
    "abs": "Audiobookshelf",
    "abs_audiobook": "Audiobookshelf",
    "bookfusion": "BookFusion",
    "cwa": "Calibre-Web Automated",
    "filesystem": "Local library",
    "grimmory": "Grimmory",
    "kosync": "KoSync",
    "storyteller": "Storyteller",
}


def _source_label(source, source_id=None):
    if source == "grimmory":
        identity = str(source_id or "")
        parts = identity.split(":")
        instance_id = parts[1] if identity.startswith("grimmory:") and len(parts) > 2 else parts[0]
        return _grimmory_label(instance_id) or _SOURCE_LABELS["grimmory"]
    return _SOURCE_LABELS.get(source, (source or "Unknown source").replace("_", " ").title())


def _media_format(item):
    media_format = getattr(item, "media_format", None) if not isinstance(item, dict) else item.get("media_format")
    if media_format in {"audiobook", "ebook"}:
        return media_format
    source = getattr(item, "source", None) if not isinstance(item, dict) else _match_source(item)
    return "audiobook" if source in {"abs", "abs_audiobook"} else "ebook"


def _explicit_media_format(item):
    media_format = getattr(item, "media_format", None) if not isinstance(item, dict) else item.get("media_format")
    return media_format if media_format in {"audiobook", "ebook"} else None


def _source_format(source, media_format=None):
    return "Audiobook" if media_format == "audiobook" or (not media_format and source in {"abs", "abs_audiobook"}) else "Ebook"


def _candidate_source_id(match):
    return (
        match.get("source_key")
        or match.get("abs_id")
        or match.get("storyteller_uuid")
        or match.get("id")
        or match.get("filename")
        or (match.get("bookfusion_ids") or [None])[0]
    )


def _detected_identity(detected):
    return detected.source, str(detected.source_id)


def _match_source(match):
    return match.get("source_family") or match.get("source")


def _match_identity(match):
    source = _match_source(match)
    if source == "abs_audiobook":
        source = "abs"
    source_key = str(match.get("source_key") or "")
    if source_key.startswith(f"{source}:"):
        return source, source_key.split(":", 1)[1]
    if source == "abs" and match.get("abs_id"):
        return source, str(match["abs_id"])
    if source == "storyteller" and match.get("storyteller_uuid"):
        return source, str(match["storyteller_uuid"])
    candidate_id = _candidate_source_id(match)
    if source and candidate_id:
        return source, str(candidate_id)
    return None


def _supported_review_matches(detected):
    return [match for match in detected.matches or [] if _is_supported_review_match(detected, match)]


def _is_supported_review_match(detected, match):
    source_format = _explicit_media_format(detected)
    match_source = _match_source(match)
    match_format = _explicit_media_format(match)
    return (
        detected.source in _AUDIOBOOK_REVIEW_SOURCES
        and source_format == "audiobook"
        and match_format == "ebook"
        and match_source in _EBOOK_MATCH_SOURCES
    ) or (
        detected.source in _EBOOK_REVIEW_SOURCES
        and source_format == "ebook"
        and match_format == "audiobook"
        and match_source in _AUDIO_MATCH_SOURCES
    )


def _manual_companion_matches(container, detected):
    provider = getattr(container, "suggestion_service", None)
    if not callable(provider):
        return []
    try:
        matches = provider().find_companion_candidates(
            detected.title or "",
            detected.author or "",
            _explicit_media_format(detected),
        )
    except Exception as exc:
        logger.warning("Manual companion search failed: %s", exc)
        return []
    if not isinstance(matches, list):
        return []
    return [match for match in matches if _is_supported_review_match(detected, match)]


def _pairing_review_url(detected, match=None):
    match = match or {}
    candidate_source = _match_source(match)
    params = {
        "search": detected.title or "",
        "detected_id": detected.id,
        "detected_source": detected.source,
        "detected_source_id": detected.source_id,
    }
    candidate_id = _candidate_source_id(match)
    if candidate_source:
        params["candidate_source"] = candidate_source
    if candidate_id:
        params["candidate_source_id"] = candidate_id
    detected_format = _explicit_media_format(detected)
    match_format = _explicit_media_format(match)
    if detected_format == "audiobook":
        params["audio_source"] = detected.source
        params["audio_source_id"] = detected.source_id
    elif match_format == "audiobook":
        audio_identity = _match_identity(match)
        if audio_identity:
            params["audio_source"], params["audio_source_id"] = audio_identity
    abs_id = match.get("abs_id") or (detected.source_id if detected.source == "abs" else None)
    if abs_id:
        params["abs_id"] = abs_id
    return url_for("matching.match", **params)


def _activity_timestamp(detected):
    source_updated_at = getattr(detected, "source_updated_at", None)
    if source_updated_at and source_updated_at.tzinfo is None:
        return source_updated_at.replace(tzinfo=UTC)
    return source_updated_at


def _serialize_detected_activity(detected):
    source_updated_at = _activity_timestamp(detected)
    try:
        display_timezone = ZoneInfo(os.environ.get("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        display_timezone = UTC
    media_format = _media_format(detected)
    return {
        "source": detected.source,
        "source_id": detected.source_id,
        "identity": {"source": detected.source, "source_id": detected.source_id},
        "source_label": _source_label(detected.source, detected.source_id),
        "media_format": media_format,
        "format": _source_format(detected.source, media_format),
        "progress": round((detected.progress_percentage or 0) * 100),
        "activity_at": (
            source_updated_at.astimezone(display_timezone).strftime("%b %-d, %Y at %-I:%M %p %Z")
            if source_updated_at
            else None
        ),
        "activity_current": bool(
            source_updated_at and source_updated_at >= datetime.now(UTC) - _RECENT_ACTIVITY_CUTOFF
        ),
        "device": detected.device,
    }


def _serialize_detected_pairing(detected, members=None):
    members = sorted(
        members or [detected],
        key=lambda member: _activity_timestamp(member) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    detected = members[0]
    activities = [_serialize_detected_activity(member) for member in members]

    review_detected = next((member for member in members if _supported_review_matches(member)), None)
    if review_detected is None:
        review_detected = next(
            (
                member
                for member in members
                if _explicit_media_format(member) == "audiobook" and member.source in _AUDIOBOOK_REVIEW_SOURCES
                or (_explicit_media_format(member) == "ebook" and member.source in _EBOOK_REVIEW_SOURCES)
            ),
            detected,
        )
    supported_matches = _supported_review_matches(review_detected)
    top_match = supported_matches[0] if supported_matches else None
    review_supported = bool(_supported_review_matches(review_detected))
    companion_matches = {}
    for member in members:
        member_format = _explicit_media_format(member)
        for match in member.matches or []:
            identity = _match_identity(match)
            match_format = _explicit_media_format(match)
            if identity and member_format and match_format and match_format != member_format:
                companion_matches.setdefault(identity, _serialize_detected_match(match))

    activity = activities[0]
    source = detected.source or "unknown"
    return {
        "id": detected.id,
        "source": detected.source,
        "source_id": detected.source_id,
        "title": detected.title or "Untitled book",
        "author": detected.author or "Unknown author",
        "cover_url": detected.cover_url,
        "source_label": _source_label(source, detected.source_id),
        "media_format": activity["media_format"],
        "format": activity["format"],
        "progress": activity["progress"],
        "activity_at": activity["activity_at"],
        "activity_current": activity["activity_current"],
        "device": detected.device,
        "identities": [activity["identity"] for activity in activities],
        "activities": activities,
        "activity_sort": _activity_timestamp(detected) or datetime.min.replace(tzinfo=UTC),
        "source_summary": ", ".join(activity["source_label"] for activity in activities),
        "companions": list(companion_matches.values()),
        "review_url": _pairing_review_url(review_detected, top_match),
        "find_companion_url": _pairing_review_url(review_detected),
        "review_supported": review_supported,
        "top_match": _serialize_detected_match(top_match) if top_match else None,
        "alternatives": [_serialize_detected_match(match) for match in supported_matches[1:]],
    }


def _serialize_detected_match(match):
    source = _match_source(match) or "unknown"
    identity = _match_identity(match)
    media_format = _media_format(match)
    return {
        "title": match.get("title") or match.get("filename") or "Untitled candidate",
        "author": match.get("author") or "Unknown author",
        "source_label": _source_label(source, _candidate_source_id(match)),
        "source": identity[0] if identity else source,
        "source_id": identity[1] if identity else None,
        "identity": {"source": identity[0], "source_id": identity[1]} if identity else None,
        "media_format": media_format,
        "format": _source_format(source, media_format),
        "confidence": "Strong" if match.get("confidence") == "high" else "Possible",
    }


def _group_detected_pairings(detected_books):
    """Group active detections only through explicit high-confidence opposite-format edges."""
    books = list(detected_books)
    by_identity = {_detected_identity(book): index for index, book in enumerate(books)}
    parents = list(range(len(books)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for index, book in enumerate(books):
        book_format = _explicit_media_format(book)
        if not book_format:
            continue
        for match in book.matches or []:
            match_format = _explicit_media_format(match)
            if match.get("confidence") != "high" or not match_format or match_format == book_format:
                continue
            target_index = by_identity.get(_match_identity(match))
            if target_index is None or _explicit_media_format(books[target_index]) != match_format:
                continue
            union(index, target_index)

    grouped = {}
    for index, book in enumerate(books):
        grouped.setdefault(find(index), []).append(book)
    return sorted(
        (_serialize_detected_pairing(members[0], members) for members in grouped.values()),
        key=lambda pairing: pairing["activity_sort"],
        reverse=True,
    )


_PAIRING_REVIEW_FIELDS = (
    "detected_id",
    "detected_source",
    "detected_source_id",
    "candidate_source",
    "candidate_source_id",
    "audio_source",
    "audio_source_id",
)


def _pairing_review_values(source):
    return {name: str(source.get(name, "") or "").strip() for name in _PAIRING_REVIEW_FIELDS}


def _load_pairing_review(container, database_service, values, method="GET"):
    if not any(values.values()):
        return None, None, 200, False
    if not all(values[name] for name in _PAIRING_REVIEW_FIELDS[:3]):
        return None, "This match link is incomplete.", 400, True
    if bool(values["candidate_source"]) != bool(values["candidate_source_id"]):
        values = {**values, "candidate_source": "", "candidate_source_id": ""}

    try:
        detected_id = int(values["detected_id"])
    except ValueError:
        return None, "This match link is invalid.", 400, True

    detected = database_service.get_detected_book(
        values["detected_source_id"], source=values["detected_source"]
    )
    if not detected or getattr(detected, "id", None) != detected_id:
        return None, "This detected book is no longer available.", 409, True
    detected_format = _explicit_media_format(detected)
    supported_detected = (detected_format == "audiobook" and detected.source in _AUDIOBOOK_REVIEW_SOURCES) or (
        detected_format == "ebook" and detected.source in _EBOOK_REVIEW_SOURCES
    )
    if not supported_detected:
        return None, "This source cannot yet be paired with the current write path.", 409, True

    status = getattr(detected, "status", "detected")
    if status != "detected" and method != "POST":
        message = "This match has already been completed." if status == "resolved" else "This match is not active."
        return None, message, 409, True
    if status not in {"detected", "processing", "resolved"}:
        return None, "This match is not active.", 409, True

    candidate = None
    warning = None
    if values["candidate_source"]:
        candidate = next(
            (
                match
                for match in (detected.matches or [])
                if _match_source(match) == values["candidate_source"]
                and str(_candidate_source_id(match) or "") == values["candidate_source_id"]
            ),
            None,
        )
        if not candidate:
            candidate = next(
                (
                    match
                    for match in _manual_companion_matches(container, detected)
                    if _match_source(match) == values["candidate_source"]
                    and str(_candidate_source_id(match) or "") == values["candidate_source_id"]
                ),
                None,
            )
        if not candidate:
            values = {**values, "candidate_source": "", "candidate_source_id": ""}
            warning = "The previous recommendation is no longer available. Choose a companion manually."

    if candidate and not _is_supported_review_match(detected, candidate):
        candidate = None
        values = {**values, "candidate_source": "", "candidate_source_id": ""}
        warning = "The previous recommendation is no longer available. Choose a companion manually."

    audio_identity = None
    if detected_format == "audiobook":
        audio_identity = _detected_identity(detected)
    elif candidate and _explicit_media_format(candidate) == "audiobook":
        audio_identity = _match_identity(candidate)
    values = {
        **values,
        "audio_source": audio_identity[0] if audio_identity else "",
        "audio_source_id": audio_identity[1] if audio_identity else "",
    }

    return {"detected": detected, "candidate": candidate, **values}, warning, 200, False


def _review_defaults(review):
    if not review:
        return {"audiobook_id": "", "ebook_filename": "", "ebook_source_id": ""}

    detected = review["detected"]
    candidate = review["candidate"] or {}
    defaults = {"audiobook_id": "", "ebook_filename": "", "ebook_source_id": ""}

    if _explicit_media_format(detected) == "audiobook" and detected.source == "abs":
        defaults["audiobook_id"] = detected.source_id
    elif _explicit_media_format(detected) == "ebook":
        defaults["ebook_filename"] = detected.ebook_filename or ""

    candidate_source = _match_source(candidate)
    if _explicit_media_format(candidate) == "audiobook" and candidate_source in {"abs", "abs_audiobook"}:
        defaults["audiobook_id"] = candidate.get("abs_id") or ""
    elif _explicit_media_format(candidate) == "ebook":
        defaults["ebook_filename"] = candidate.get("filename") or defaults["ebook_filename"]
        if candidate_source == "grimmory":
            defaults["ebook_source_id"] = candidate.get("id") or ""

    return defaults


def _progress_sources_configured(container):
    try:
        if get_abs_service().is_available():
            return True
    except Exception:
        pass
    for client in (container.storyteller_client(), container.grimmory_client_group()):
        try:
            if client.is_configured():
                return True
        except Exception:
            pass
    try:
        return any(
            name.lower() == "kosync" and client.is_configured() for name, client in container.sync_clients().items()
        )
    except (AttributeError, TypeError):
        return False


def _render_terminal_review_error(message, status_code):
    return render_template("match_review_error.html", pairing_error=message), status_code


def _expected_review_kosync_id(review, ebook_filename):
    if not review:
        return None
    detected = review["detected"]
    if detected.source == "kosync" and ebook_filename == detected.ebook_filename:
        return detected.source_id
    candidate = review.get("candidate") or {}
    candidate_source = _match_source(candidate)
    if candidate_source == "kosync" and ebook_filename == candidate.get("filename"):
        source_key = str(_candidate_source_id(candidate) or "")
        return source_key.split(":", 1)[1] if source_key.startswith("kosync:") else source_key
    return None


def _exact_review_editions(container, review):
    """Resolve the stored opposite-format edge to live editions; form values are never authority."""
    detected = review["detected"]
    candidate = review.get("candidate")
    if not candidate:
        return None, "Choose a different companion to continue."

    detected_format = _explicit_media_format(detected)
    candidate_format = _explicit_media_format(candidate)
    if not detected_format or not candidate_format or detected_format == candidate_format:
        return None, "The recommended companion no longer has a valid format identity."

    audio_item = detected if detected_format == "audiobook" else candidate
    ebook_item = detected if detected_format == "ebook" else candidate
    audio_identity = _detected_identity(audio_item) if audio_item is detected else _match_identity(audio_item)
    if not audio_identity or audio_identity[0] not in _AUDIOBOOK_REVIEW_SOURCES:
        return None, "The recommended audiobook identity is no longer supported."

    if audio_identity[0] == "abs":
        audio = next((item for item in get_audiobooks_conditionally() if item["id"] == audio_identity[1]), None)
    else:
        audio = container.grimmory_client_group().find_audiobook_by_source_id(audio_identity[1])
    if not audio:
        return None, "The selected audiobook edition is no longer available."

    ebook_filename = (
        detected.ebook_filename if ebook_item is detected else candidate.get("filename")
    )
    if not ebook_filename:
        return None, "The selected ebook edition is no longer available."
    ebooks = [ebook for ebook in get_searchable_ebooks(ebook_filename) if ebook.name == ebook_filename]
    ebook_source_id = None
    ebook_source = detected.source if ebook_item is detected else _match_source(candidate)
    if ebook_source == "grimmory":
        expected_id = candidate.get("id") if ebook_item is candidate else detected.source_id
        ebook = next((item for item in ebooks if str(item.grimmory_id or "") == str(expected_id)), None)
        if ebook:
            ebook_source_id = str(ebook.grimmory_id)
    else:
        ebook = ebooks[0] if ebooks else None
    if not ebook:
        return None, "The selected ebook edition is no longer available."

    expected_kosync_id = _expected_review_kosync_id(review, ebook_filename)
    if expected_kosync_id:
        grimmory_book, grimmory_client = find_in_grimmory(ebook_filename, ebook_source_id)
        book_id = grimmory_book.get("id") if grimmory_book else None
        file_id = (
            grimmory_book.get("bookFileId")
            if grimmory_book and grimmory_book.get("isPrimary") is False
            else None
        )
        actual_kosync_id = get_kosync_id_for_ebook(
            ebook_filename,
            book_id,
            bl_client=grimmory_client,
            grimmory_file_id=file_id,
        )
        if actual_kosync_id != expected_kosync_id:
            return None, "The selected ebook no longer matches the detected edition."

    return {
        "audio_source": audio_identity[0],
        "audio_source_id": audio_identity[1],
        "audio": audio,
        "ebook": ebook,
        "ebook_filename": ebook_filename,
        "ebook_source_id": ebook_source_id,
    }, None


def _render_match_page(
    container,
    manager,
    database_service,
    values,
    review=None,
    error=None,
    status_code=200,
    combine_conflict=None,
):
    defaults = _review_defaults(review)
    review_editions = None
    manual_companions = []
    review_started = None
    if review:
        review_started = {
            "title": review["detected"].title or "Untitled book",
            "format": _source_format(review["detected"].source, _explicit_media_format(review["detected"])),
            "source_label": _source_label(review["detected"].source, review["detected"].source_id),
        }
        review_editions, edition_error = _exact_review_editions(container, review)
        if edition_error and not error:
            error = edition_error
            status_code = 409 if review.get("candidate") else 200
        if edition_error:
            review = {**review, "candidate": None, "candidate_source": "", "candidate_source_id": ""}
        if not review_editions:
            manual_companions = [
                {
                    **_serialize_detected_match(candidate),
                    "review_url": _pairing_review_url(review["detected"], candidate),
                }
                for candidate in _manual_companion_matches(container, review["detected"])
            ]
    search = str(values.get("search") or (review["detected"].title if review else "") or "").strip().lower()
    attach_to = str(values.get("attach_to", "") or "").strip()
    link_to = str(values.get("link_to", "") or "").strip()
    selected_abs_id = str(values.get("audiobook_id") or values.get("abs_id") or defaults["audiobook_id"]).strip()
    selected_ebook_filename = sanitize_filename(values.get("ebook_filename") or defaults["ebook_filename"])
    selected_ebook_source_id = str(values.get("ebook_source_id") or defaults["ebook_source_id"]).strip()
    if review_editions:
        selected_abs_id = review_editions["audio_source_id"] if review_editions["audio_source"] == "abs" else ""
        selected_ebook_filename = review_editions["ebook_filename"]
        selected_ebook_source_id = review_editions["ebook_source_id"] or ""
    attach_title = ""
    link_title = ""

    if attach_to:
        attach_book = database_service.get_book_by_ref(attach_to)
        if attach_book:
            attach_title = attach_book.title or attach_to

    if link_to:
        link_book = database_service.get_book_by_ref(link_to)
        if link_book:
            link_title = link_book.title or link_to

    abs_service = get_abs_service()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        if not attach_to:
            audiobooks = get_audiobooks_conditionally()
            audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
            for ab in audiobooks:
                ab["cover_url"] = abs_service.get_cover_proxy_url(ab["id"])

        if not link_to:
            ebooks = get_searchable_ebooks(search)

            if container.storyteller_client().is_configured():
                try:
                    storyteller_books = container.storyteller_client().search_books(search)
                except Exception as e:
                    logger.warning(f"Storyteller search failed in match route: {e}")

    if selected_abs_id and not attach_to and not any(ab["id"] == selected_abs_id for ab in audiobooks):
        selected = next((ab for ab in get_audiobooks_conditionally() if ab["id"] == selected_abs_id), None)
        if selected:
            selected["cover_url"] = abs_service.get_cover_proxy_url(selected_abs_id)
            audiobooks.insert(0, selected)

    candidate = review["candidate"] if review else None
    candidate_abs_id = candidate.get("abs_id") if candidate else None
    if candidate_abs_id and not attach_to and not any(ab["id"] == candidate_abs_id for ab in audiobooks):
        candidate_ab = next((ab for ab in get_audiobooks_conditionally() if ab["id"] == candidate_abs_id), None)
        if candidate_ab:
            candidate_ab["cover_url"] = abs_service.get_cover_proxy_url(candidate_abs_id)
            audiobooks.append(candidate_ab)

    if selected_ebook_filename and not link_to:
        exact_results = get_searchable_ebooks(selected_ebook_filename)
        seen = {(eb.name, str(eb.source_id or "")) for eb in ebooks}
        ebooks.extend(eb for eb in exact_results if (eb.name, str(eb.source_id or "")) not in seen)

    candidate_filename = candidate.get("filename") if candidate else None
    if candidate_filename and candidate_filename != selected_ebook_filename and not link_to:
        exact_results = get_searchable_ebooks(candidate_filename)
        seen = {(eb.name, str(eb.source_id or "")) for eb in ebooks}
        ebooks.extend(eb for eb in exact_results if (eb.name, str(eb.source_id or "")) not in seen)

    detected = review["detected"] if review else None
    if detected and detected.source == "grimmory" and selected_ebook_filename == detected.ebook_filename:
        instance_id = detected.source_id.split(":", 1)[0]
        exact = next(
            (
                eb
                for eb in ebooks
                if eb.name == detected.ebook_filename and str(eb.grimmory_id or "").startswith(f"{instance_id}:")
            ),
            None,
        )
        if exact and not selected_ebook_source_id:
            selected_ebook_source_id = str(exact.grimmory_id)

    selected_ebook = next(
        (
            eb
            for eb in ebooks
            if eb.name == selected_ebook_filename
            and (not selected_ebook_source_id or str(eb.grimmory_id or "") == selected_ebook_source_id)
        ),
        None,
    )
    for ebook in ebooks:
        ebook.is_selected = ebook is selected_ebook

    if review_editions:
        audio = review_editions["audio"]
        review_editions["audio_title"] = (
            manager.get_audiobook_title(audio)
            if review_editions["audio_source"] == "abs"
            else audio.get("title") or detected.title
        )
        review_editions["audio_source_label"] = _source_label(
            review_editions["audio_source"], review_editions["audio_source_id"]
        )
        review_editions["ebook_title"] = review_editions["ebook"].title or detected.title
        review_editions["ebook_source_label"] = _source_label(
            detected.source if _explicit_media_format(detected) == "ebook" else _match_source(candidate),
            detected.source_id if _explicit_media_format(detected) == "ebook" else _candidate_source_id(candidate),
        )
        review_editions["started_format"] = _explicit_media_format(detected)

    storyteller_submit_available = False
    try:
        provider = getattr(container, "storyteller_submission_service", None)
        storyteller_submit_available = bool(provider and provider().is_available())
    except Exception as e:
        logger.debug("Storyteller submission availability check failed: %s", e)

    storyteller_force_mode = os.environ.get("STORYTELLER_FORCE_MODE", "false").lower() == "true"
    storyteller_configured = container.storyteller_client().is_configured()
    abs_configured = abs_service.is_available()
    cwa_provider = getattr(container, "cwa_client", None)
    has_abs_ebooks = getattr(abs_service, "has_ebook_libraries", lambda: False)
    has_ebook_sources = (
        any_grimmory_configured()
        or bool(cwa_provider and cwa_provider().is_configured())
        or has_abs_ebooks()
        or get_ebook_dir().exists()
    )

    library_abs_ids = set()
    library_ebook_filenames = set()
    if search:
        all_books = database_service.get_all_books()
        library_abs_ids = {b.abs_id for b in all_books}
        library_ebook_filenames = {b.ebook_filename for b in all_books if b.ebook_filename}
        library_ebook_filenames |= {b.original_ebook_filename for b in all_books if b.original_ebook_filename}

    expected_kosync_id = _expected_review_kosync_id(review, selected_ebook_filename)
    if not combine_conflict and selected_abs_id and selected_ebook_filename and not error:
        inspection = _get_book_intake_service(container).inspect_audiobook_ebook(
            abs_id=selected_abs_id,
            ebook_filename=selected_ebook_filename,
            ebook_source_id=selected_ebook_source_id or None,
            expected_ebook_kosync_id=expected_kosync_id,
        )
        if inspection.conflict_code == "combine_required":
            combine_conflict = inspection

    pairing_values = _pairing_review_values(review if review else values)
    page = render_template(
        "match.html",
        audiobooks=audiobooks,
        ebooks=ebooks,
        storyteller_books=storyteller_books,
        search=_escape_template_value(search),
        get_title=manager.get_audiobook_title,
        attach_to=_escape_template_value(attach_to),
        attach_title=_escape_template_value(attach_title),
        link_to=_escape_template_value(link_to),
        link_title=_escape_template_value(link_title),
        preselect_abs_id=_escape_template_value(selected_abs_id),
        selected_ebook_filename=_escape_template_value(selected_ebook_filename),
        selected_ebook_source_id=_escape_template_value(selected_ebook_source_id),
        storyteller_submit_available=storyteller_submit_available,
        storyteller_force_mode=storyteller_force_mode,
        storyteller_configured=storyteller_configured,
        library_abs_ids=library_abs_ids,
        library_ebook_filenames=library_ebook_filenames,
        abs_configured=abs_configured,
        has_ebook_sources=has_ebook_sources,
        pairing_review=review,
        ebook_review_sources=_EBOOK_REVIEW_SOURCES,
        pairing_values=pairing_values,
        pairing_error=error,
        review_editions=review_editions,
        manual_companions=manual_companions,
        review_started=review_started,
        combine_conflict=combine_conflict,
    )
    return (page, status_code) if status_code != 200 else page


def _post_pairing_review(container, manager, database_service, review):
    values = request.form

    def render_error(message, status_code=400, combine_conflict=None):
        return _render_match_page(
            container,
            manager,
            database_service,
            values,
            review=review,
            error=message,
            status_code=status_code,
            combine_conflict=combine_conflict,
        )

    detected = review["detected"]
    editions, edition_error = _exact_review_editions(container, review)
    if edition_error:
        return render_error(edition_error, 409)

    ebook_filename = editions["ebook_filename"]
    ebook_source_id = editions["ebook_source_id"]
    intake = _get_book_intake_service(container)
    if editions["audio_source"] == "grimmory":
        result = intake.link_grimmory_audiobook_ebook(
            audio_source_id=editions["audio_source_id"],
            ebook_filename=ebook_filename,
            ebook_source_id=ebook_source_id,
            expected_ebook_kosync_id=_expected_review_kosync_id(review, ebook_filename),
            detected_source=detected.source,
            detected_source_id=detected.source_id,
        )
    else:
        selected_ab = editions["audio"]
        metadata = selected_ab.get("media", {}).get("metadata", {})
        result = intake.map_audiobook_ebook(
            abs_id=editions["audio_source_id"],
            title=manager.get_audiobook_title(selected_ab),
            ebook_filename=ebook_filename,
            ebook_source_id=ebook_source_id,
            duration=manager.get_duration(selected_ab),
            storyteller_uuid=None,
            storyteller_submit=False,
            author=get_audiobook_author(selected_ab),
            subtitle=metadata.get("subtitle") or None,
            detected_source=detected.source,
            detected_source_id=detected.source_id,
            expected_ebook_kosync_id=_expected_review_kosync_id(review, ebook_filename),
            confirm_combine=values.get("confirm_combine") == "1",
            confirmed_merge_book_id=values.get("combine_book_id"),
        )
    if result.error:
        return render_error(result.error, result.status_code, combine_conflict=result)
    return redirect(url_for("matching.suggestions"))


@matching_bp.route("/suggestions")
def suggestions():
    """Currently Reading pairing inbox, with the legacy catalog behind Advanced."""
    container = get_container()
    database_service = get_database_service()
    library_view = request.args.get("view") == "library"

    if not library_view:
        active_detected = database_service.get_active_detected_books()
        if not isinstance(active_detected, (list, tuple)):
            active_detected = []
        pairings = _group_detected_pairings(active_detected)
        total_detected = database_service.get_detected_book_count()
        return render_template(
            "suggestions.html",
            library_view=False,
            pairings=pairings,
            pairing_count=len(pairings),
            has_detected_history=isinstance(total_detected, int) and total_detected > 0,
            progress_sources_configured=_progress_sources_configured(container),
        )

    raw_suggestions = database_service.get_all_actionable_suggestions()
    suggestions_list = [serialize_suggestion(s) for s in raw_suggestions if s.matches]
    visible_count = sum(1 for s in suggestions_list if not s.get("hidden"))
    hidden_count = sum(1 for s in suggestions_list if s.get("hidden"))
    suggestions_enabled = current_app.config.get("SUGGESTIONS_ENABLED", False)
    bookfusion_enabled = container.bookfusion_client().is_configured()
    bookfusion_catalog_count = len(database_service.get_bookfusion_books()) if bookfusion_enabled else 0
    initial_search = request.args.get("search", "").strip()
    selected_source_id = request.args.get("source_id", "").strip()
    return render_template(
        "suggestions.html",
        library_view=True,
        suggestions=suggestions_list,
        visible_count=visible_count,
        hidden_count=hidden_count,
        suggestions_enabled=suggestions_enabled,
        bookfusion_enabled=bookfusion_enabled,
        bookfusion_catalog_count=bookfusion_catalog_count,
        suggestions_data=suggestions_list,
        initial_search=_escape_template_value(initial_search),
        selected_source_id=_escape_template_value(selected_source_id),
    )


@matching_bp.route("/match", methods=["GET", "POST"])
def match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()
    values = request.form if request.method == "POST" else request.args
    review_values = _pairing_review_values(values)
    pairing_review, review_error, review_status, terminal_error = _load_pairing_review(
        container, database_service, review_values, request.method
    )

    if terminal_error:
        return _render_terminal_review_error(review_error, review_status)

    if review_error and not pairing_review:
        return _render_match_page(
            container,
            manager,
            database_service,
            values,
            error=review_error,
            status_code=review_status,
        )

    if request.method == "POST":
        if pairing_review:
            return _post_pairing_review(container, manager, database_service, pairing_review)

        action = request.form.get("action", "")
        intake_service = _get_book_intake_service(container)

        # --- Audio-only import (no ebook required) ---
        if action == "audio_only":
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            abs_id = request.form.get("audiobook_id")
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            intake_service.import_audio_only(
                abs_id=abs_id,
                title=manager.get_audiobook_title(selected_ab),
                duration=manager.get_duration(selected_ab),
                author=get_audiobook_author(selected_ab),
                subtitle=selected_ab.get("media", {}).get("metadata", {}).get("subtitle") or None,
            )
            return redirect(url_for("dashboard.index"))

        # --- Ebook-only import (no audiobook required) ---
        if action == "ebook_only":
            ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
            ebook_source_id = request.form.get("ebook_source_id") or None
            ebook_display_name = request.form.get("ebook_display_name", "")
            storyteller_uuid = request.form.get("storyteller_uuid") or None
            storyteller_title = request.form.get("storyteller_title", "")

            if not ebook_filename and not storyteller_uuid:
                return "An ebook or Storyteller selection is required", 400

            result = intake_service.import_ebook_only(
                ebook_filename=ebook_filename,
                ebook_source_id=ebook_source_id,
                ebook_display_name=ebook_display_name,
                storyteller_uuid=storyteller_uuid,
                storyteller_title=storyteller_title,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Attach ebook to audio-only book ---
        if action == "attach_ebook":
            attach_abs_id = request.form.get("attach_abs_id")
            ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
            ebook_source_id = request.form.get("ebook_source_id") or None
            result = intake_service.attach_ebook(
                abs_id=attach_abs_id,
                ebook_filename=ebook_filename,
                ebook_source_id=ebook_source_id,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Attach audiobook to ebook-only book ---
        if action == "attach_audiobook":
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            link_book_id = request.form.get("link_book_id")
            abs_id = request.form.get("audiobook_id")
            if not link_book_id or not abs_id:
                return "Missing book ID or audiobook ID", 400
            book = database_service.get_book_by_ref(link_book_id)
            if not book:
                return "Book not found", 404
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            result = intake_service.attach_audiobook(
                source_book_id=link_book_id,
                abs_id=abs_id,
                title=manager.get_audiobook_title(selected_ab),
                duration=manager.get_duration(selected_ab),
                author=get_audiobook_author(selected_ab),
                subtitle=selected_ab.get("media", {}).get("metadata", {}).get("subtitle") or None,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Standard flow (requires audiobook) ---
        abs_service = get_abs_service()
        abs_id = request.form.get("audiobook_id")
        ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
        ebook_source_id = request.form.get("ebook_source_id") or None
        storyteller_uuid = request.form.get("storyteller_uuid")
        storyteller_submit = request.form.get("storyteller_submit")

        if not ebook_filename:
            return "An ebook selection is required for audiobook + ebook matching", 400

        audiobooks = abs_service.get_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
        if not selected_ab:
            return "Audiobook not found", 404

        _ab_meta = selected_ab.get("media", {}).get("metadata", {})
        result = intake_service.map_audiobook_ebook(
            abs_id=abs_id,
            title=manager.get_audiobook_title(selected_ab),
            ebook_filename=ebook_filename,
            ebook_source_id=ebook_source_id,
            duration=manager.get_duration(selected_ab),
            storyteller_uuid=storyteller_uuid,
            storyteller_submit=bool(storyteller_submit),
            author=get_audiobook_author(selected_ab),
            subtitle=_ab_meta.get("subtitle") or None,
            confirm_combine=request.form.get("confirm_combine") == "1",
            confirmed_merge_book_id=request.form.get("combine_book_id"),
        )
        if result.error:
            return _render_match_page(
                container,
                manager,
                database_service,
                request.form,
                error=result.error,
                status_code=result.status_code,
                combine_conflict=result,
            )

        return redirect(url_for("dashboard.index"))

    return _render_match_page(
        container,
        manager,
        database_service,
        request.args,
        review=pairing_review,
        error=review_error,
        status_code=review_status,
    )


@matching_bp.route("/batch-match", methods=["GET", "POST"])
def batch_match():
    container = get_container()
    manager = get_manager()

    abs_service = get_abs_service()

    if request.method == "POST":
        action = request.form.get("action")
        intake_service = _get_book_intake_service(container)
        if action == "add_to_queue":
            session.setdefault("queue", [])
            abs_id = request.form.get("audiobook_id") or ""
            ebook_filename = sanitize_filename(request.form.get("ebook_filename", "")) or ""
            ebook_source_id = request.form.get("ebook_source_id", "")
            ebook_display_name = request.form.get("ebook_display_name", ebook_filename)
            storyteller_uuid = request.form.get("storyteller_uuid", "")

            if not abs_id and not ebook_filename and not storyteller_uuid:
                return redirect(url_for("matching.batch_match", search=_redirect_search_value()))

            # Resolve audiobook metadata if present
            selected_ab = None
            if abs_id:
                audiobooks = abs_service.get_audiobooks()
                selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
                if not selected_ab:
                    return redirect(url_for("matching.batch_match", search=_redirect_search_value()))

            # Dedup key: audiobook, qualified ebook source, then legacy filename.
            queue_key = abs_id or ebook_source_id or ebook_filename
            if not any(item.get("queue_key") == queue_key for item in session["queue"]):
                is_ebook_only = not abs_id and (ebook_filename or storyteller_uuid)
                is_audio_only = abs_id and not ebook_filename and not storyteller_uuid
                title = (
                    manager.get_audiobook_title(selected_ab)
                    if selected_ab
                    else ebook_display_name or Path(ebook_filename).stem
                    if ebook_filename
                    else "Storyteller Book"
                )
                _ab_meta = (selected_ab or {}).get("media", {}).get("metadata", {})
                session["queue"].append(
                    {
                        "queue_key": queue_key,
                        "abs_id": abs_id,
                        "title": title,
                        "ebook_filename": ebook_filename,
                        "ebook_source_id": ebook_source_id,
                        "ebook_display_name": ebook_display_name,
                        "storyteller_uuid": storyteller_uuid,
                        "storyteller_submit": bool(request.form.get("storyteller_submit")),
                        "duration": manager.get_duration(selected_ab) if selected_ab else 0,
                        "cover_url": abs_service.get_cover_proxy_url(abs_id) if abs_id else None,
                        "audio_only": is_audio_only,
                        "ebook_only": is_ebook_only,
                        "author": get_audiobook_author(selected_ab) if selected_ab else None,
                        "subtitle": _ab_meta.get("subtitle") or None,
                    }
                )
                session.modified = True
            return redirect(url_for("matching.batch_match", search=_redirect_search_value()))
        elif action == "remove_from_queue":
            remove_key = request.form.get("queue_key") or request.form.get("abs_id")
            session["queue"] = [
                item for item in session.get("queue", []) if item.get("queue_key", item.get("abs_id")) != remove_key
            ]
            session.modified = True
            return redirect(url_for("matching.batch_match"))
        elif action == "clear_queue":
            session["queue"] = []
            session.modified = True
            return redirect(url_for("matching.batch_match"))
        elif action == "process_queue":
            failed_items = []
            for item in session.get("queue", []):
                item_label = item.get("ebook_display_name") or item.get("ebook_filename") or item.get("abs_id")
                try:
                    if item.get("audio_only"):
                        intake_service.import_audio_only(
                            abs_id=item["abs_id"],
                            title=item["title"],
                            duration=item["duration"],
                            author=item.get("author"),
                            subtitle=item.get("subtitle"),
                        )
                        continue

                    if item.get("ebook_only"):
                        result = intake_service.import_ebook_only(
                            ebook_filename=item["ebook_filename"],
                            ebook_source_id=item.get("ebook_source_id") or None,
                            ebook_display_name=item.get("ebook_display_name") or "",
                            storyteller_uuid=item.get("storyteller_uuid") or None,
                            storyteller_title=item.get("title", "Storyteller Book"),
                        )
                        if result.error:
                            failed_items.append(item.get("ebook_display_name") or item["ebook_filename"])
                        continue

                    if not item.get("ebook_filename"):
                        failed_items.append(item_label)
                        continue

                    _book, error = _create_book_mapping(
                        container,
                        abs_id=item["abs_id"],
                        title=item["title"],
                        ebook_filename=item["ebook_filename"],
                        ebook_source_id=item.get("ebook_source_id") or None,
                        duration=item["duration"],
                        storyteller_uuid=item.get("storyteller_uuid", ""),
                        storyteller_submit=bool(item.get("storyteller_submit")),
                        author=item.get("author"),
                        subtitle=item.get("subtitle"),
                    )
                    if error:
                        failed_items.append(item.get("ebook_display_name") or item["ebook_filename"])

                except Exception as e:
                    logger.error(f"Failed to process queue item '{sanitize_log_data(item_label)}': {e}")
                    failed_items.append(item_label)

            if failed_items:
                names = ", ".join(failed_items)
                flash(f"Could not compute KOSync ID for: {names}", "warning")
            session["queue"] = []
            session.modified = True
            return redirect(url_for("dashboard.index"))

    # GET request
    search = request.args.get("search", "").strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks:
            ab["cover_url"] = abs_service.get_cover_proxy_url(ab["id"])

        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

        if container.storyteller_client().is_configured():
            try:
                storyteller_books = container.storyteller_client().search_books(search)
            except Exception as e:
                logger.warning(f"Storyteller search failed in batch_match route: {e}")

    storyteller_submit_available = False
    try:
        st_sub_svc = container.storyteller_submission_service()
        storyteller_submit_available = st_sub_svc.is_available()
    except Exception as e:
        logger.debug("Storyteller submission availability check failed: %s", e)

    storyteller_force_mode = os.environ.get("STORYTELLER_FORCE_MODE", "false").lower() == "true"
    storyteller_configured = container.storyteller_client().is_configured()

    abs_configured = abs_service.is_available()
    has_ebook_sources = (
        any_grimmory_configured()
        or container.cwa_client().is_configured()
        or abs_service.has_ebook_libraries()
        or get_ebook_dir().exists()
    )

    queue_view = _build_batch_queue_view(session.get("queue", []))
    return render_template(
        "batch_match.html",
        audiobooks=audiobooks,
        ebooks=ebooks,
        storyteller_books=storyteller_books,
        queue=queue_view["items"],
        queue_summary=queue_view,
        search=_escape_template_value(search),
        get_title=manager.get_audiobook_title,
        storyteller_submit_available=storyteller_submit_available,
        storyteller_force_mode=storyteller_force_mode,
        storyteller_configured=storyteller_configured,
        abs_configured=abs_configured,
        has_ebook_sources=has_ebook_sources,
    )
