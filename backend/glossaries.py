import json
from datetime import datetime

from backend.database import connect


def normalize_terms(values):
    terms = []
    seen = set()
    for value in values or []:
        term = " ".join(str(value).strip().split())[:80]
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms[:100]


def glossary_row_to_dict(row):
    return {
        "channel": row["channel"],
        "terms": json.loads(row["terms_json"] or "[]"),
        "language": row["language"] or "en",
        "version": row["version"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def list_glossaries():
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT * FROM channel_glossaries ORDER BY lower(channel)"
        ).fetchall()
    return [glossary_row_to_dict(row) for row in rows]


def get_glossary(channel):
    with connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM channel_glossaries WHERE channel = ?",
            (channel,),
        ).fetchone()
    return glossary_row_to_dict(row) if row else None


def save_glossary(channel, terms, language, username, version=None):
    channel = " ".join(str(channel).strip().split())[:160]
    if not channel:
        raise ValueError("Channel is required")
    language = str(language or "en").strip().lower()[:12]
    normalized = normalize_terms(terms)
    now = datetime.now().isoformat()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT version FROM channel_glossaries WHERE channel = ?",
            (channel,),
        ).fetchone()
        if existing is not None and version is not None and version != existing["version"]:
            raise RuntimeError("Glossary changed in another session")
        next_version = (existing["version"] + 1) if existing else 1
        connection.execute(
            """
            INSERT INTO channel_glossaries(
                channel, terms_json, language, version, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                terms_json = excluded.terms_json,
                language = excluded.language,
                version = excluded.version,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (
                channel,
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                language,
                next_version,
                username,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM channel_glossaries WHERE channel = ?",
            (channel,),
        ).fetchone()
        connection.commit()
    return glossary_row_to_dict(row)


def prompt_for_channel(channel):
    glossary = get_glossary(channel)
    if not glossary or not glossary["terms"]:
        return None, "en"
    return ", ".join(glossary["terms"]), glossary["language"]
