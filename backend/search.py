import base64
import hashlib
import json
import re
import time


SEARCH_STATES = {
    "unreviewed",
    "in_review",
    "confirmed",
    "corrected",
    "dismissed",
}
SEARCH_STATUSES = {"ready", "suspect"}
SEARCH_SORTS = {"relevance", "recent"}
WORD_SANITIZER = re.compile(r"[^\w'-]+", re.UNICODE)


def quote_fts_term(value, prefix=False):
    words = [
        WORD_SANITIZER.sub("", word)
        for word in str(value or "").strip().split()
    ]
    cleaned = " ".join(word for word in words if word)
    if not cleaned:
        return ""
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"{"*" if prefix else ""}'


def safe_search_query(value):
    raw_tokens = re.findall(r'"[^"]*"|\S+', str(value or "").strip())
    output = []
    expecting_operand = True
    for raw_token in raw_tokens:
        upper = raw_token.upper()
        if upper in {"AND", "OR", "NOT"}:
            if expecting_operand or not output:
                continue
            output.append(upper)
            expecting_operand = True
            continue
        phrase = raw_token.startswith('"') and raw_token.endswith('"')
        token_value = raw_token[1:-1] if phrase else raw_token
        prefix = not phrase and token_value.endswith("*")
        if prefix:
            token_value = token_value[:-1]
        term = quote_fts_term(token_value, prefix=prefix)
        if not term:
            continue
        if not expecting_operand:
            output.append("AND")
        output.append(term)
        expecting_operand = False
    if output and output[-1] in {"AND", "OR", "NOT"}:
        output.pop()
    return " ".join(output)


def search_fingerprint(query, filters, sort):
    payload = json.dumps(
        {"query": query, "filters": filters, "sort": sort},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def encode_cursor(offset, fingerprint):
    payload = json.dumps(
        {"offset": offset, "fingerprint": fingerprint},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value, fingerprint):
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("fingerprint") != fingerprint:
            raise ValueError("cursor does not match this search")
        offset = int(payload["offset"])
        if offset < 0 or offset > 5_000_000:
            raise ValueError("cursor is outside the supported range")
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid search cursor") from exc


def build_search_filters(
    *,
    channel="",
    year=None,
    date_from="",
    date_to="",
    start_time="",
    end_time="",
    status="",
    review_state="",
    reviewer="",
    bookmarked=None,
    model="",
    include_suspect=False,
):
    clauses = ["t.status != 'blank'"]
    parameters = []
    normalized = {}
    if status:
        if status not in SEARCH_STATUSES:
            raise ValueError("invalid transcript status")
        if status == "suspect" and not include_suspect:
            clauses.append("t.status = 'ready'")
            normalized["status"] = "ready"
        else:
            clauses.append("t.status = ?")
            parameters.append(status)
            normalized["status"] = status
    elif not include_suspect:
        clauses.append("t.status = 'ready'")
    if channel:
        clauses.append("t.channel = ?")
        parameters.append(channel[:160])
        normalized["channel"] = channel[:160]
    if year is not None:
        if year < 1900 or year > 2100:
            raise ValueError("invalid recording year")
        clauses.append("t.recording_year = ?")
        parameters.append(year)
        normalized["year"] = year
    effective_time = "coalesce(t.recorded_at, t.timestamp)"
    for value, operator, name in (
        (date_from, ">=", "date_from"),
        (date_to, "<=", "date_to"),
    ):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"invalid {name.replace('_', ' ')}")
            clauses.append(f"substr({effective_time}, 1, 10) {operator} ?")
            parameters.append(value)
            normalized[name] = value
    if start_time and end_time and start_time > end_time:
        clauses.append(
            f"(substr({effective_time}, 12, 5) >= ? OR "
            f"substr({effective_time}, 12, 5) <= ?)"
        )
        parameters.extend([start_time, end_time])
    else:
        if start_time:
            clauses.append(f"substr({effective_time}, 12, 5) >= ?")
            parameters.append(start_time)
        if end_time:
            clauses.append(f"substr({effective_time}, 12, 5) <= ?")
            parameters.append(end_time)
    if start_time:
        normalized["start_time"] = start_time
    if end_time:
        normalized["end_time"] = end_time
    if review_state:
        if review_state not in SEARCH_STATES:
            raise ValueError("invalid review state")
        clauses.append("t.review_state = ?")
        parameters.append(review_state)
        normalized["review_state"] = review_state
    if reviewer:
        clauses.append("t.reviewed_by = ?")
        parameters.append(reviewer[:80])
        normalized["reviewer"] = reviewer[:80]
    if bookmarked is not None:
        clauses.append("t.bookmarked = ?")
        parameters.append(int(bookmarked))
        normalized["bookmarked"] = bool(bookmarked)
    if model:
        clauses.append("t.transcription_model = ?")
        parameters.append(model[:200])
        normalized["model"] = model[:200]
    return clauses, parameters, normalized


def search_transcripts(
    connection,
    *,
    query,
    limit=50,
    cursor="",
    sort="relevance",
    **filters,
):
    started_at = time.perf_counter()
    fts_query = safe_search_query(query)
    if not fts_query:
        raise ValueError("enter at least one searchable term")
    if sort not in SEARCH_SORTS:
        raise ValueError("invalid search sort")
    clauses, parameters, normalized_filters = build_search_filters(**filters)
    clauses.insert(0, "transcripts_fts MATCH ?")
    parameters.insert(0, fts_query)
    where_clause = " AND ".join(clauses)
    fingerprint = search_fingerprint(fts_query, normalized_filters, sort)
    offset = decode_cursor(cursor, fingerprint)
    result_limit = max(1, min(int(limit), 100))

    total = connection.execute(
        f"""
        SELECT count(*)
        FROM transcripts_fts
        JOIN transcripts t ON t.id = transcripts_fts.rowid
        WHERE {where_clause}
        """,
        parameters,
    ).fetchone()[0]

    order_by = (
        "rank_score ASC, t.id DESC"
        if sort == "relevance"
        else "coalesce(t.recorded_at, t.timestamp) DESC, t.id DESC"
    )
    rows = connection.execute(
        f"""
        SELECT
            t.id, t.timestamp, t.recorded_at, t.recording_year, t.channel,
            t.filename, t.transcript_text, t.quality_score, t.quality_reason,
            t.status, t.reviewed, t.review_state, t.reviewed_by,
            t.reviewed_at, t.review_resolution, t.version, t.bookmarked,
            t.notes, t.corrected_by, t.corrected_at,
            t.transcription_model, t.retry_status,
            snippet(transcripts_fts, 0, '⟦', '⟧', ' … ', 24) AS snippet,
            bm25(transcripts_fts, 8.0, 3.0, 1.0) AS rank_score
        FROM transcripts_fts
        JOIN transcripts t ON t.id = transcripts_fts.rowid
        WHERE {where_clause}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        [*parameters, result_limit, offset],
    ).fetchall()
    next_offset = offset + len(rows)
    return {
        "rows": rows,
        "count": total,
        "next_cursor": (
            encode_cursor(next_offset, fingerprint)
            if next_offset < total
            else None
        ),
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "parsed_query": fts_query,
        "sort": sort,
    }
