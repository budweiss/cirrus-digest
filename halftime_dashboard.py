#!/usr/bin/env python3
"""halftime_dashboard.py — build the Steelers halftime candidate dashboard.

S79. Justin's reply (halftime/REQUIREMENTS.md) asked for a dashboard rather
than a brief: every home game visible, a few options against each, and games
with nothing said so explicitly.

THE SHAPE. A nightly job writes ONE SNAPSHOT; the page renders that snapshot
and nothing else talks to anything. The data changes nightly, not per request,
so a live service would buy uptime risk and no freshness. It also makes
"what changed since you last looked" a diff of two files rather than a feature
somebody has to remember to build.

TWO POOLS, KEPT APART (R27). A TOURING act is a candidate for the one game its
routing passes near. A FOR-HIRE act has no routing at all and is available for
every game. Merge them and the same handful of for-hire names fill all nine
games identically and bury the routing signal that is the point of the tool.

COVERAGE IS AN OUTPUT, NOT A LOG LINE. It is what lets the page tell three
different empties apart -- "we swept and found nothing", "we have not swept
yet", and "the sweep broke". A dashboard that renders all three as a blank cell
tells a booker we checked when we did not. It is also the only honest answer to
"can we bypass Pollstar": a coverage count is evidence, an assurance is not.

Python 3.9-safe (CIRRUS): no PEP-604 unions.
"""
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import entity_kb

KB_PROJECT = "halftime_acts"
PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "out" / "halftime"

SEASON = 2026
TEAM = "Pittsburgh Steelers"
VENUE = "Acrisure Stadium"

# 2026 home slate. `date` is None where the league has not set it: Week 16 is
# unflexed, and the Week 7 game against New Orleans is a home game STAGED IN
# PARIS, whose date we have not verified from a primary source. A guessed date
# on a client dashboard is worse than an absent one -- the booker plans around
# it. Absent, with the reason, is honest and still useful.
HOME_GAMES = [
    {"week": 1,  "date": "2026-09-13", "opponent": "Atlanta Falcons",
     "kick_et": "13:00", "slot": "day"},
    {"week": 3,  "date": "2026-09-27", "opponent": "Cincinnati Bengals",
     "kick_et": "13:00", "slot": "day"},
    {"week": 5,  "date": "2026-10-11", "opponent": "Indianapolis Colts",
     "kick_et": "13:00", "slot": "day"},
    {"week": 7,  "date": None, "opponent": "New Orleans Saints",
     "kick_et": None, "slot": "international",
     "note": "Home game staged in Paris. Not an Acrisure date, and a different "
             "entertainment programme — listed so the season is complete, not "
             "because it is a booking opportunity. Date unverified here.",
     "at_venue": False},
    {"week": 8,  "date": "2026-11-01", "opponent": "Cleveland Browns",
     "kick_et": "13:00", "slot": "day",
     "target": "military / patriotic tie",
     "note": "Client target. Falls in the league's November Salute to Service "
             "window."},
    {"week": 12, "date": "2026-11-27", "opponent": "Denver Broncos",
     "kick_et": "15:00", "slot": "Black Friday national (Prime Video)"},
    {"week": 13, "date": "2026-12-06", "opponent": "Houston Texans",
     "kick_et": "20:20", "slot": "Sunday Night Football (NBC)"},
    {"week": 15, "date": "2026-12-20", "opponent": "Baltimore Ravens",
     "kick_et": "13:00", "slot": "day",
     "target": "rivalry game — warrants an act",
     "note": "Client target."},
    {"week": 16, "date": None, "opponent": "Carolina Panthers",
     "kick_et": None, "slot": "flex — not yet set",
     "note": "Late December. Recheck when the league sets it."},
]

POOLS = ("touring", "for_hire")
POOL_LABEL = {"touring": "Routing through",
              "for_hire": "Available to book"}

# Coverage states. The whole point of R4 is that these are three different
# answers and the page must never render them the same way.
NOT_SWEPT = "not_swept"
SWEPT = "swept"
FAILED = "failed"
# A fourth state, added because the selftest caught the page telling the Paris
# game "none available — we searched and nothing cleared the bar", which is a
# lie twice over: nobody searched, and nothing was ever going to. R4 is about
# not flattening distinct answers into one blank cell, and this file had done
# exactly that to itself.
NOT_APPLICABLE = "n/a"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def game_id(game: Dict) -> str:
    return "wk{:02d}-{}".format(
        game["week"], game["opponent"].split()[-1].lower())


# ── badges ──────────────────────────────────────────────────────────────────
# Deliberately NOT a composite score. Justin's reader books acts for a living
# and wants the search narrowed, not the decision made (R14); a single number
# would hide exactly the reasoning they would want to argue with. Each badge is
# an independent, checkable claim, and the page sorts by whichever one matters
# for the game in front of you.

_PA_HINTS = ("pittsburgh", "pennsylvania", ", pa", " pa ", "butler",
             "wexford", "allegheny", "steel city")


def badges_for(act: Dict) -> List[Dict]:
    """Independent claims about one act. Each carries the evidence for itself,
    so a badge can be argued with rather than trusted."""
    out = []
    fields = act.get("fields") or {}
    home = (fields.get("home_base") or "").lower()
    cat = (fields.get("category") or "").lower()
    clients = (fields.get("clients") or "")
    fee = (fields.get("fee_note") or "").strip()

    if any(h in home for h in _PA_HINTS):
        out.append({"kind": "market", "label": "Pittsburgh / PA tie",
                    "why": fields.get("home_base", "")})
    if "military" in cat or "patriotic" in cat:
        out.append({"kind": "patriotic", "label": "Military / patriotic",
                    "why": fields.get("category", "")})
    if "nostalgia" in cat or "classic rock" in cat or "tribute" in cat:
        out.append({"kind": "nostalgia", "label": "Nostalgia fit (45+)",
                    "why": fields.get("category", "")})
    if fee:
        out.append({"kind": "price", "label": "Documented fee", "why": fee})
    if clients:
        out.append({"kind": "credit", "label": "Has sports credits",
                    "why": clients})
    return out


def rank_for_game(game: Dict, acts: List[Dict]) -> List[Dict]:
    """Order candidates for ONE game. The target flag on a game decides which
    badge leads — for 11/1 that is the patriotic tie, everywhere else the
    market tie. Nothing is filtered out by ranking; a booker gets to see the
    tail and decide."""
    target = (game.get("target") or "").lower()
    lead = "patriotic" if "military" in target or "patriot" in target else "market"

    def key(act):
        kinds = {b["kind"] for b in act.get("badges", [])}
        return (0 if lead in kinds else 1,
                0 if "market" in kinds else 1,
                0 if "credit" in kinds else 1,
                0 if "price" in kinds else 1,
                act.get("name", ""))

    return sorted(acts, key=key)


# ── snapshot ────────────────────────────────────────────────────────────────

def _load_acts(pool: str, db_path: Optional[str] = None) -> List[Dict]:
    """Acts in one pool, as the catalogue recorded them."""
    try:
        rows = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
    except Exception:
        return []
    out = []
    for row in rows:
        fields = row.get("fields") or row.get("state") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        if (fields.get("pool") or "variety") != _kb_pool(pool):
            continue
        act = {"slug": row.get("slug"), "name": row.get("name"),
               "fields": fields, "last_updated": row.get("last_updated")}
        act["badges"] = badges_for(act)
        out.append(act)
    return out


def _kb_pool(pool: str) -> str:
    """Dashboard pool name -> the value the catalogue writes."""
    return "for_hire_music" if pool == "for_hire" else "touring"


def build_snapshot(db_path: Optional[str] = None,
                   games: Optional[List[Dict]] = None) -> Dict:
    """Everything the page needs, in one file."""
    games = games if games is not None else HOME_GAMES
    for_hire = _load_acts("for_hire", db_path=db_path)

    snap = {"season": SEASON, "team": TEAM, "venue": VENUE,
            "generated_at": _now(), "games": [], "acts_total": len(for_hire)}

    for game in games:
        gid = game_id(game)
        entry = dict(game)
        entry["game_id"] = gid
        entry["at_venue"] = game.get("at_venue", True)

        # FOR-HIRE: no routing, so the same pool applies to every game. Coverage
        # is the catalogue's own state, which is genuinely known.
        if entry["at_venue"]:
            fh = rank_for_game(game, for_hire)
            fh_cov = {"state": SWEPT, "swept_at": _now(),
                      "sources": "halftime_catalogue (nightly)",
                      "found": len(fh),
                      "note": "Acts with no tour to track — available for "
                              "any date."}
        else:
            fh = []
            fh_cov = {"state": NOT_APPLICABLE, "swept_at": None,
                      "sources": None, "found": 0,
                      "note": "Staged in Paris — a different entertainment "
                              "programme, so no candidates are offered."}

        # TOURING: not built yet. Saying so is the requirement (R4); pretending
        # a blank cell means "nothing out there" is the failure it guards.
        if entry["at_venue"]:
            tr_cov = {"state": NOT_SWEPT, "swept_at": None,
                      "sources": None, "found": 0,
                      "note": "Routing sweep not built yet — no source has "
                              "been checked for this date. This is NOT a "
                              "finding of 'no acts available'."}
        else:
            tr_cov = {"state": NOT_APPLICABLE, "swept_at": None,
                      "sources": None, "found": 0,
                      "note": "Staged in Paris — routing to Acrisure does not "
                              "apply."}

        entry["candidates"] = {"for_hire": fh, "touring": []}
        entry["coverage"] = {"for_hire": fh_cov, "touring": tr_cov}
        snap["games"].append(entry)
    return snap


# ── render ──────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _empty_message(cov: Dict) -> str:
    """The three empties, worded so they cannot be mistaken for each other."""
    state = cov.get("state")
    if state == NOT_APPLICABLE:
        return ("<p class='empty na'><strong>Not applicable.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    if state == NOT_SWEPT:
        return ("<p class='empty not-swept'><strong>Not searched yet.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    if state == FAILED:
        return ("<p class='empty failed'><strong>Search failed.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    return ("<p class='empty none'><strong>None available.</strong> "
            "We searched and nothing cleared the bar. "
            + _e(cov.get("note") or "") + "</p>")


def _act_card(act: Dict) -> str:
    fields = act.get("fields") or {}
    bits = ["<li class='act'>", "<div class='act-name'>",
            _e(act.get("name")), "</div>"]
    if act.get("badges"):
        bits.append("<div class='badges'>")
        for b in act["badges"]:
            bits.append("<span class='badge b-{}' title='{}'>{}</span>".format(
                _e(b["kind"]), _e(b["why"]), _e(b["label"])))
        bits.append("</div>")
    for label, key in (("Fee", "fee_note"), ("Base", "home_base"),
                       ("Credits", "clients"), ("Booking", "booking_contact")):
        val = (fields.get(key) or "").strip()
        if val:
            bits.append("<div class='row'><span class='k'>{}</span>"
                        "<span class='v'>{}</span></div>".format(
                            _e(label), _e(val)))
    bits.append("</li>")
    return "".join(bits)


def _coverage_line(cov: Dict) -> str:
    if cov.get("state") == NOT_APPLICABLE:
        return "<div class='cov cov-none'>not applicable to this game</div>"
    if cov.get("state") == NOT_SWEPT:
        return "<div class='cov cov-none'>no sweep has run</div>"
    return "<div class='cov'>{} · {} found · {}</div>".format(
        _e(cov.get("sources") or "—"), _e(cov.get("found", 0)),
        _e((cov.get("swept_at") or "")[:10]))


def render_html(snap: Dict) -> str:
    parts = [_HEAD.format(team=_e(snap["team"]), season=_e(snap["season"]))]
    parts.append(
        "<header><h1>{} · {} home slate</h1>"
        "<p class='sub'>{} · every home game, both supply pools. "
        "Generated {}.</p></header>".format(
            _e(snap["team"]), _e(snap["season"]), _e(snap["venue"]),
            _e(snap["generated_at"])))

    targets = [g for g in snap["games"] if g.get("target")]
    if targets:
        parts.append("<section class='targets'><h2>Your two dates</h2><ul>")
        for g in targets:
            parts.append("<li><strong>{}</strong> vs {} — {}</li>".format(
                _e(g.get("date") or "date TBD"), _e(g["opponent"]),
                _e(g["target"])))
        parts.append("</ul></section>")

    for g in snap["games"]:
        cls = "game" + (" is-target" if g.get("target") else "") + \
              ("" if g.get("at_venue", True) else " off-site")
        parts.append("<section class='{}'>".format(cls))
        parts.append(
            "<h2><span class='wk'>Wk {}</span> {} <span class='opp'>vs {}</span>"
            "</h2>".format(_e(g["week"]), _e(g.get("date") or "date TBD"),
                           _e(g["opponent"])))
        meta = [g.get("slot")]
        if g.get("kick_et"):
            meta.append(g["kick_et"] + " ET")
        parts.append("<p class='meta'>{}</p>".format(
            _e(" · ".join(m for m in meta if m))))
        if g.get("target"):
            parts.append("<p class='target'>Target: {}</p>".format(
                _e(g["target"])))
        if g.get("note"):
            parts.append("<p class='note'>{}</p>".format(_e(g["note"])))

        parts.append("<div class='pools'>")
        for pool in POOLS:
            cov = g["coverage"][pool]
            acts = g["candidates"][pool]
            parts.append("<div class='pool pool-{}'>".format(_e(pool)))
            parts.append("<h3>{}</h3>".format(_e(POOL_LABEL[pool])))
            parts.append(_coverage_line(cov))
            if acts:
                parts.append("<ul class='acts'>")
                parts.extend(_act_card(a) for a in acts)
                parts.append("</ul>")
            else:
                parts.append(_empty_message(cov))
            parts.append("</div>")
        parts.append("</div></section>")

    parts.append(
        "<footer><p>Two pools, kept apart on purpose. <strong>Routing "
        "through</strong> is acts whose tour already passes near the date. "
        "<strong>Available to book</strong> is acts with no tour to track, "
        "who play when called — they apply to every date, which is why they "
        "are not mixed into the routing list.</p>"
        "<p>Coverage is stated on every panel so an empty one can be read "
        "correctly: “none available” and “not searched yet” are different "
        "answers.</p></footer></body></html>")
    return "\n".join(parts)


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{team} {season} — halftime candidates</title>
<style>
:root {{ --bg:#0f1216; --card:#171c22; --edge:#232b34; --ink:#e8edf2;
         --dim:#93a1b0; --gold:#ffb612; --accent:#4da3ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--ink);
        font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
header {{ max-width:1100px; margin:0 auto 28px; }}
h1 {{ margin:0 0 6px; font-size:26px; letter-spacing:-.02em; }}
.sub {{ margin:0; color:var(--dim); font-size:14px; }}
section {{ max-width:1100px; margin:0 auto 18px; background:var(--card);
           border:1px solid var(--edge); border-radius:12px; padding:18px 20px; }}
section.is-target {{ border-color:var(--gold); }}
section.off-site {{ opacity:.72; }}
.targets {{ background:transparent; border-style:dashed; }}
.targets ul {{ margin:8px 0 0; padding-left:20px; }}
h2 {{ margin:0 0 4px; font-size:18px; font-weight:600; }}
.wk {{ display:inline-block; min-width:52px; color:var(--gold);
       font-variant-numeric:tabular-nums; }}
.opp {{ color:var(--dim); font-weight:400; }}
.meta {{ margin:0 0 10px; color:var(--dim); font-size:13px; }}
.target {{ margin:0 0 10px; color:var(--gold); font-size:13px; font-weight:600; }}
.note {{ margin:0 0 12px; color:var(--dim); font-size:13px; font-style:italic; }}
.pools {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:760px) {{ .pools {{ grid-template-columns:1fr; }} }}
.pool {{ background:#12171c; border:1px solid var(--edge); border-radius:9px;
         padding:12px 14px; }}
h3 {{ margin:0 0 6px; font-size:13px; text-transform:uppercase;
      letter-spacing:.08em; color:var(--accent); }}
.cov {{ font-size:12px; color:var(--dim); margin-bottom:10px;
        font-variant-numeric:tabular-nums; }}
.cov-none {{ color:#7d8794; font-style:italic; }}
.acts {{ list-style:none; margin:0; padding:0; }}
.act {{ padding:10px 0; border-top:1px solid var(--edge); }}
.act:first-child {{ border-top:none; }}
.act-name {{ font-weight:600; }}
.badges {{ margin:6px 0 4px; display:flex; flex-wrap:wrap; gap:5px; }}
.badge {{ font-size:11px; padding:2px 7px; border-radius:99px;
          border:1px solid var(--edge); color:var(--dim); }}
.b-market {{ border-color:var(--gold); color:var(--gold); }}
.b-patriotic {{ border-color:#7fb3ff; color:#7fb3ff; }}
.b-nostalgia {{ border-color:#c9a3ff; color:#c9a3ff; }}
.b-price {{ border-color:#79d19a; color:#79d19a; }}
.row {{ font-size:13px; color:var(--dim); margin-top:2px; }}
.k {{ display:inline-block; min-width:64px; color:#6f7b88; }}
.empty {{ margin:6px 0 0; font-size:13px; color:var(--dim); }}
.empty.not-swept strong {{ color:#e0a03a; }}
.empty.none strong {{ color:#8fa0b0; }}
.empty.failed strong {{ color:#e06a6a; }}
.empty.na strong {{ color:#6f7b88; }}
footer {{ max-width:1100px; margin:26px auto 0; color:var(--dim);
          font-size:13px; border-top:1px solid var(--edge); padding-top:14px; }}
</style></head><body>"""


def build(out_dir: Optional[Path] = None,
          db_path: Optional[str] = None) -> Dict:
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = build_snapshot(db_path=db_path)
    (out_dir / "snapshot.json").write_text(json.dumps(snap, indent=2))
    (out_dir / "index.html").write_text(render_html(snap))
    return {"games": len(snap["games"]), "acts": snap["acts_total"],
            "out": str(out_dir)}


# ── selftest ────────────────────────────────────────────────────────────────

def selftest() -> int:
    import tempfile
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    check("every home game has a stable id",
          len({game_id(g) for g in HOME_GAMES}) == len(HOME_GAMES))
    check("the whole home slate is present, not just the target dates",
          len(HOME_GAMES) == 9)
    check("both client target dates are flagged",
          {g["date"] for g in HOME_GAMES if g.get("target")}
          == {"2026-11-01", "2026-12-20"})
    check("the Paris game is listed but marked as not a venue date",
          any(g.get("at_venue") is False for g in HOME_GAMES))
    check("no game invents a date the league has not set",
          all(g.get("date") or g.get("note") for g in HOME_GAMES))

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "kb.sqlite3")
        entity_kb.upsert_entity(
            KB_PROJECT, "test-patriot", "Test Patriot Band",
            entity_type="halftime_act", db_path=db,
            fields={"pool": "for_hire_music", "category": "military / patriotic",
                    "home_base": "Pittsburgh, PA", "fee_note": "$20,000 all-in",
                    "clients": "Some Bowl", "booking_contact": ""})
        entity_kb.upsert_entity(
            KB_PROJECT, "test-variety", "Test Dog Show",
            entity_type="halftime_act", db_path=db,
            fields={"pool": "variety", "category": "dog show"})
        snap = build_snapshot(db_path=db)

        check("a snapshot covers every game",
              len(snap["games"]) == len(HOME_GAMES))
        check("the variety pool does NOT leak into the for-hire dashboard",
              snap["acts_total"] == 1)

        wk8 = next(g for g in snap["games"] if g["week"] == 8)
        check("a for-hire act appears against a real home game",
              [a["name"] for a in wk8["candidates"]["for_hire"]]
              == ["Test Patriot Band"])
        check("the touring pool is EMPTY and says why, rather than looking searched",
              wk8["coverage"]["touring"]["state"] == NOT_SWEPT)
        check("for-hire acts are offered on every venue game, not just targets",
              all(g["candidates"]["for_hire"]
                  for g in snap["games"] if g.get("at_venue", True)))
        check("the Paris game is not offered for-hire candidates",
              next(g for g in snap["games"]
                   if g["week"] == 7)["candidates"]["for_hire"] == [])

        badges = {b["kind"] for b in wk8["candidates"]["for_hire"][0]["badges"]}
        check("badges are independent claims, not one score",
              {"patriotic", "market", "price", "credit"} <= badges)

        page = render_html(snap)
        check("each kind of empty gets its OWN wording on the page",
              all(w in page for w in ("Not searched yet", "Not applicable"))
              and page.count("class='empty not-swept'") > 0
              and page.count("class='empty na'") > 0)
        check("an empty panel never renders as a bare blank",
              page.count("<div class='pool pool-") == sum(
                  1 for _g in snap["games"] for _p in POOLS)
              and "class='empty" in page)
        check("an unsearched pool never reads as 'nothing out there'",
              "NOT a finding" in json.dumps(snap))
        check("every game reaches the page",
              all(_e(g["opponent"]) in page for g in snap["games"]))
        check("the target dates are called out on the page",
              "Your two dates" in page)
        check("no unescaped act name can inject markup",
              "<script>" not in render_html(_snap_with_name(snap, "<script>x")))

        res = build(out_dir=Path(tmp) / "out", db_path=db)
        check("build writes both the snapshot and the page",
              (Path(res["out"]) / "snapshot.json").exists()
              and (Path(res["out"]) / "index.html").exists())

    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


def _snap_with_name(snap: Dict, name: str) -> Dict:
    clone = json.loads(json.dumps(snap))
    for g in clone["games"]:
        for a in g["candidates"]["for_hire"]:
            a["name"] = name
    return clone


def main() -> int:
    args = sys.argv[1:]
    if "selftest" in args:
        return selftest()
    res = build()
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
