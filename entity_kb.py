"""Generic entity knowledge-base / CRM engine — shared across projects (S65).

One reusable store for "persistent per-entity memory that tracks change over
time" — the pattern docs/HOA-KNOWLEDGE-BASE-PLAN.md calls for, generalized so
Bill's HOA leads, Aggie's OFFER listings, and Alyssa's PEDAGOGY sources can
all use the SAME engine instead of three copy-pasted ones. A caller picks a
`project` string (e.g. "hoa_leads_bill", "offer_listings", "pedagogy_sources")
and everything else — schema, storage, change history, querying — is shared.

Storage: one SQLite file per project at data/entity_kb/<project>.db (relative
to this file's directory, i.e. cirrus-digest's checkout root on each box).
Git-ignored — this is data, not code, same convention as the rest of
cirrus-repo's runtime state.

Two tables:
  - entities: one row per tracked thing (a community, a listing, a source).
    Current state lives in `state_json` (a flat dict of whatever fields the
    caller cares about — schema-free on purpose, since different projects
    track different attributes).
  - entity_events: append-only log of everything that changed on an entity,
    OR a qualitative signal noticed about it (a distress signal, an RFP, a
    management change) that isn't a field diff. This is what makes "what
    changed on this entity, and when" a first-class query instead of
    something reconstructed by re-reading state history by hand.

No project-specific logic here — HOA-specific field names, lead-qualification
rules, etc. belong in the calling script (e.g. a future hoa_research.py), not
in this module.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "entity_kb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    entity_type TEXT,
    lead_state TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    UNIQUE(project, slug)
);

CREATE TABLE IF NOT EXISTS entity_events (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    event_type TEXT NOT NULL,   -- 'field_change' | 'signal'
    field TEXT,                 -- field_change only
    old_value TEXT,             -- field_change only
    new_value TEXT,             -- field_change only
    signal_kind TEXT,           -- signal only (distress|rfp|mgmt-change|other, caller-defined)
    summary TEXT,               -- signal only
    source_url TEXT,
    confidence TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON entity_events(entity_id);
CREATE INDEX IF NOT EXISTS idx_events_project_time ON entity_events(project, occurred_at);
"""


def _db_path(project: str, db_path: str = None) -> Path:
    if db_path:
        return Path(db_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{project}.db"


def _connect(project: str, db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(project, db_path)))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_entity(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["state"] = json.loads(d.pop("state_json") or "{}")
    return d


def upsert_entity(project: str, slug: str, name: str, entity_type: str = None,
                   fields: dict = None, lead_state: str = None,
                   occurred_at: str = None, db_path: str = None) -> dict:
    """Create the entity if new, else diff `fields` (and lead_state) against
    the stored state and ledger a field_change event for anything that
    differs. Returns {"created": bool, "changed_fields": [...]}."""
    fields = fields or {}
    occurred_at = occurred_at or _now()
    conn = _connect(project, db_path)
    try:
        row = conn.execute(
            "SELECT * FROM entities WHERE project=? AND slug=?", (project, slug)
        ).fetchone()

        if row is None:
            state = dict(fields)
            conn.execute(
                "INSERT INTO entities (project, slug, name, entity_type, lead_state, "
                "state_json, first_seen, last_updated) VALUES (?,?,?,?,?,?,?,?)",
                (project, slug, name, entity_type, lead_state, json.dumps(state),
                 occurred_at, occurred_at),
            )
            conn.commit()
            return {"created": True, "changed_fields": list(state.keys())}

        entity_id = row["id"]
        state = json.loads(row["state_json"] or "{}")
        changed = []

        for key, new_val in fields.items():
            old_val = state.get(key)
            if old_val != new_val:
                conn.execute(
                    "INSERT INTO entity_events (project, entity_id, event_type, field, "
                    "old_value, new_value, occurred_at, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    (project, entity_id, "field_change", key,
                     json.dumps(old_val), json.dumps(new_val), occurred_at, _now()),
                )
                state[key] = new_val
                changed.append(key)

        new_lead_state = lead_state if lead_state is not None else row["lead_state"]
        if lead_state is not None and lead_state != row["lead_state"]:
            conn.execute(
                "INSERT INTO entity_events (project, entity_id, event_type, field, "
                "old_value, new_value, occurred_at, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                (project, entity_id, "field_change", "lead_state",
                 json.dumps(row["lead_state"]), json.dumps(lead_state), occurred_at, _now()),
            )
            changed.append("lead_state")

        conn.execute(
            "UPDATE entities SET name=?, entity_type=COALESCE(?, entity_type), "
            "lead_state=?, state_json=?, last_updated=? WHERE id=?",
            (name, entity_type, new_lead_state, json.dumps(state), occurred_at, entity_id),
        )
        conn.commit()
        return {"created": False, "changed_fields": changed}
    finally:
        conn.close()


def add_signal(project: str, slug: str, kind: str, summary: str,
                source_url: str = None, confidence: str = None,
                occurred_at: str = None, name: str = None,
                db_path: str = None) -> None:
    """Ledger a qualitative signal about an entity (auto-creates a bare
    entity if `slug` doesn't exist yet -- pass `name` in that case)."""
    occurred_at = occurred_at or _now()
    conn = _connect(project, db_path)
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE project=? AND slug=?", (project, slug)
        ).fetchone()
        if row is None:
            now = _now()
            cur = conn.execute(
                "INSERT INTO entities (project, slug, name, state_json, first_seen, "
                "last_updated) VALUES (?,?,?,?,?,?)",
                (project, slug, name or slug, "{}", now, now),
            )
            entity_id = cur.lastrowid
        else:
            entity_id = row["id"]

        conn.execute(
            "INSERT INTO entity_events (project, entity_id, event_type, signal_kind, "
            "summary, source_url, confidence, occurred_at, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (project, entity_id, "signal", kind, summary, source_url, confidence,
             occurred_at, _now()),
        )
        conn.execute("UPDATE entities SET last_updated=? WHERE id=?", (_now(), entity_id))
        conn.commit()
    finally:
        conn.close()


def get_entity(project: str, slug: str, db_path: str = None) -> dict:
    conn = _connect(project, db_path)
    try:
        row = conn.execute(
            "SELECT * FROM entities WHERE project=? AND slug=?", (project, slug)
        ).fetchone()
        return _row_to_entity(row) if row else None
    finally:
        conn.close()


def list_entities(project: str, lead_state: str = None, entity_type: str = None,
                   db_path: str = None) -> list:
    conn = _connect(project, db_path)
    try:
        query = "SELECT * FROM entities WHERE project=?"
        params = [project]
        if lead_state:
            query += " AND lead_state=?"
            params.append(lead_state)
        if entity_type:
            query += " AND entity_type=?"
            params.append(entity_type)
        query += " ORDER BY last_updated DESC"
        return [_row_to_entity(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_events(project: str, slug: str = None, since: str = None,
                event_type: str = None, db_path: str = None) -> list:
    """Events for a recap -- one entity's history if `slug` given, else the
    whole project's event stream (e.g. for a weekly digest)."""
    conn = _connect(project, db_path)
    try:
        query = (
            "SELECT e.*, en.slug, en.name FROM entity_events e "
            "JOIN entities en ON en.id = e.entity_id WHERE e.project=?"
        )
        params = [project]
        if slug:
            query += " AND en.slug=?"
            params.append(slug)
        if since:
            query += " AND e.occurred_at >= ?"
            params.append(since)
        if event_type:
            query += " AND e.event_type=?"
            params.append(event_type)
        query += " ORDER BY e.occurred_at DESC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def project_counts(project: str, db_path: str = None) -> dict:
    """Read-only summary: entity count by lead_state. Used by ops/diagnostic
    tooling to verify a project's KB without dumping the whole thing."""
    conn = _connect(project, db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE project=?", (project,)
        ).fetchone()[0]
        by_state = conn.execute(
            "SELECT COALESCE(lead_state, '(none)') AS state, COUNT(*) AS n "
            "FROM entities WHERE project=? GROUP BY lead_state ORDER BY n DESC",
            (project,),
        ).fetchall()
        events_total = conn.execute(
            "SELECT COUNT(*) FROM entity_events WHERE project=?", (project,)
        ).fetchone()[0]
        return {
            "total_entities": total,
            "by_lead_state": {r["state"]: r["n"] for r in by_state},
            "total_events": events_total,
        }
    finally:
        conn.close()


def _normalize(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search_entities(project: str, query_text: str, db_path: str = None,
                     limit: int = 5) -> list:
    """Simple substring/word-overlap search over entity names+slugs within a
    project -- not fuzzy/typo-tolerant, just enough for "what do you have on
    X" style lookups where the caller names the entity close to verbatim.
    Returns entities ranked best-match-first."""
    q = _normalize(query_text)
    q_words = {w for w in q.split() if len(w) > 3}
    scored = []
    for e in list_entities(project, db_path=db_path):
        name_norm = _normalize(e["name"])
        slug_norm = e["slug"].replace("-", " ")
        if name_norm and name_norm in q:
            scored.append((100 + len(name_norm), e))
            continue
        if slug_norm and slug_norm in q:
            scored.append((100 + len(slug_norm), e))
            continue
        overlap = q_words & set(name_norm.split())
        if len(overlap) >= 2:
            scored.append((len(overlap), e))
    scored.sort(key=lambda t: -t[0])
    return [e for _, e in scored[:limit]]


def recap_text(project: str, slug: str, db_path: str = None,
                max_events: int = 8) -> str:
    """Human-readable summary of one entity's current state + recent
    history -- for a client-facing recap reply, not a raw data dump.
    Empty string if the entity doesn't exist."""
    e = get_entity(project, slug, db_path=db_path)
    if not e:
        return ""

    lines = [e["name"]]
    if e.get("lead_state"):
        lines.append(f"Status: {e['lead_state']}")

    state = e.get("state", {})
    # Only the fields most likely to matter to a human reader -- state_json
    # can hold arbitrary caller-defined keys, we don't dump all of them.
    for key in ("county", "type", "mgmt_status", "current_mgmt_co",
                "board_contact", "board_email", "website", "opportunity_tier"):
        val = state.get(key)
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    events = get_events(project, slug=slug, db_path=db_path)[:max_events]
    if events:
        lines.append("")
        lines.append("Recent history:")
        for ev in events:
            date = (ev.get("occurred_at") or "")[:10]
            if ev["event_type"] == "signal":
                src = f" (source: {ev['source_url']})" if ev.get("source_url") else ""
                lines.append(f"- {date}: {ev.get('summary', '')}{src}")
            else:
                try:
                    old = json.loads(ev["old_value"]) if ev.get("old_value") else None
                    new = json.loads(ev["new_value"]) if ev.get("new_value") else None
                except (TypeError, json.JSONDecodeError):
                    old, new = ev.get("old_value"), ev.get("new_value")
                lines.append(f"- {date}: {ev['field']} changed from '{old}' to '{new}'")

    if e.get("last_updated"):
        lines.append("")
        lines.append(f"(Last updated in our records: {e['last_updated']})")

    return "\n".join(lines)


def selftest() -> bool:
    """Exercises the module against a throwaway in-memory-equivalent temp
    file. No effect on any real project's data."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    project = "selftest"
    checks = []

    try:
        r = upsert_entity(project, "acme-hoa", "Acme HOA", entity_type="hoa",
                           fields={"county": "Kent", "mgmt_co": "Acme PM"},
                           lead_state="cold", db_path=path)
        checks.append(("PASS", "create returns created=True",
                        r["created"] is True and set(r["changed_fields"]) == {"county", "mgmt_co"}))

        r2 = upsert_entity(project, "acme-hoa", "Acme HOA",
                            fields={"county": "Kent", "mgmt_co": "Beta PM"},
                            lead_state="warm", db_path=path)
        checks.append(("PASS", "update detects only changed fields",
                        r2["created"] is False and set(r2["changed_fields"]) == {"mgmt_co", "lead_state"}))

        e = get_entity(project, "acme-hoa", db_path=path)
        checks.append(("PASS", "state reflects latest values",
                        e["state"]["mgmt_co"] == "Beta PM" and e["lead_state"] == "warm"))

        add_signal(project, "acme-hoa", "distress", "Pool closed all season",
                    source_url="https://example.com", confidence="high", db_path=path)
        events = get_events(project, slug="acme-hoa", db_path=path)
        checks.append(("PASS", "field changes + signal both ledgered",
                        len(events) == 3 and any(ev["event_type"] == "signal" for ev in events)))

        counts = project_counts(project, db_path=path)
        checks.append(("PASS", "project_counts matches",
                        counts["total_entities"] == 1 and counts["total_events"] == 3))

        add_signal(project, "brand-new-hoa", "rfp", "Board issued an RFP",
                    name="Brand New HOA", db_path=path)
        checks.append(("PASS", "add_signal auto-creates missing entity",
                        get_entity(project, "brand-new-hoa", db_path=path) is not None))

        upsert_entity(project, "mermaid-run", "Mermaid Run Condominium Association",
                      fields={"county": "New Castle"}, db_path=path)
        hits = search_entities(project, "what do you have on Mermaid Run?", db_path=path)
        checks.append(("PASS", "search_entities matches a name substring in the question",
                        len(hits) == 1 and hits[0]["slug"] == "mermaid-run"))
        no_hits = search_entities(project, "what's the weather like today", db_path=path)
        checks.append(("PASS", "search_entities returns nothing for an unrelated question",
                        len(no_hits) == 0))

        recap = recap_text(project, "acme-hoa", db_path=path)
        checks.append(("PASS", "recap_text includes name, state, and signal history",
                        "Acme HOA" in recap and "Beta PM" in recap and "Pool closed" in recap))
        checks.append(("PASS", "recap_text is empty for an unknown entity",
                        recap_text(project, "does-not-exist", db_path=path) == ""))

        all_ok = all(ok for _, _, ok in checks)
        for status, desc, ok in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        return all_ok
    finally:
        if os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    print(__doc__)
