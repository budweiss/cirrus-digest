#!/usr/bin/env python3
"""Read-only ESPN lookups for Project Immaculate's resolve passes (S253).

immaculate_agent.py runs with NO general shell -- it may only run a fixed list
of immaculate_* commands -- so it cannot curl ESPN itself. This is the one
fetch it needs, pinned to ESPN's public NFL API and trimmed to the parts a
question can turn on (the raw summary payload is ~600 KB).

    python3 immaculate_espn.py schedule            # PIT's season: week, kickoff, status, score, event id
    python3 immaculate_espn.py summary <EVENT_ID>  # one game: team stats, player lines, scoring plays, drives
    python3 immaculate_espn.py selftest

Found live S253: ESPN 403s BOTH a browser User-Agent and a made-up one
("cowork-immaculate/1.0"), and answers 200 to urllib's own default, curl's and
python-requests'. So no User-Agent is set: urllib's default is the one proven.
"""
import json
import sys
import urllib.request

API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def _get(path):
    req = urllib.request.Request(f"{API}/{path}")  # default UA -- see docstring
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _score(c):
    s = c.get("score")
    return s.get("displayValue") if isinstance(s, dict) else s


def schedule_rows(data):
    rows = []
    for e in data.get("events", []):
        comp = e["competitions"][0]
        rows.append({
            "week": e.get("week", {}).get("number"),
            "kickoff_utc": e.get("date"),
            "game": e.get("shortName"),
            "status": comp["status"]["type"]["description"],
            "score": {c["team"]["abbreviation"]: _score(c) for c in comp["competitors"]},
            "event_id": e["id"],
        })
    return rows


def summary_view(data):
    box = data.get("boxscore", {})
    comp = data.get("header", {}).get("competitions", [{}])[0]
    return {
        "status": comp.get("status", {}).get("type", {}).get("description"),
        "score": {c["team"]["abbreviation"]: c.get("score")
                  for c in comp.get("competitors", [])},
        "team_stats": {t["team"]["abbreviation"]:
                       {s["name"]: s.get("displayValue") for s in t.get("statistics", [])}
                       for t in box.get("teams", [])},
        "players": {t["team"]["abbreviation"]:
                    {g["name"]: {"labels": g.get("labels"),
                                 "rows": [[a["athlete"]["displayName"], *a.get("stats", [])]
                                          for a in g.get("athletes", [])]}
                     for g in t.get("statistics", [])}
                    for t in box.get("players", [])},
        "scoring_plays": [{"q": p.get("period", {}).get("number"),
                           "clock": p.get("clock", {}).get("displayValue"),
                           "team": p.get("team", {}).get("abbreviation"),
                           "type": p.get("type", {}).get("text"),
                           "text": p.get("text"),
                           "away": p.get("awayScore"), "home": p.get("homeScore")}
                          for p in data.get("scoringPlays", [])],
        "drives": [{"team": d.get("team", {}).get("abbreviation"),
                    "result": d.get("displayResult") or d.get("result"),
                    "summary": d.get("description"),
                    "scored": d.get("isScore")}
                   for d in data.get("drives", {}).get("previous", [])],
    }


def selftest() -> int:
    """Offline: shapes only, fed a hand-built payload (T32 -- no network)."""
    ok = True

    def ck(label, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    sched = {"events": [{"id": "9", "date": "2026-09-20T17:00Z", "shortName": "PIT @ NE",
                         "week": {"number": 2},
                         "competitions": [{"status": {"type": {"description": "Final"}},
                                           "competitors": [
                                               {"team": {"abbreviation": "NE"}, "score": {"displayValue": "20"}},
                                               {"team": {"abbreviation": "PIT"}, "score": "3"}]}]}]}
    r = schedule_rows(sched)[0]
    ck("schedule: dict and plain scores both read", r["score"] == {"NE": "20", "PIT": "3"})
    ck("schedule: status + event id carried", (r["status"], r["event_id"]) == ("Final", "9"))

    summ = {"header": {"competitions": [{"status": {"type": {"description": "Final"}},
                                         "competitors": [{"team": {"abbreviation": "PIT"}, "score": "3"}]}]},
            "boxscore": {"teams": [{"team": {"abbreviation": "PIT"},
                                    "statistics": [{"name": "totalYards", "displayValue": "251"}]}],
                         "players": [{"team": {"abbreviation": "PIT"},
                                      "statistics": [{"name": "defensive", "labels": ["TOT"],
                                                      "athletes": [{"athlete": {"displayName": "T.J. Watt"},
                                                                    "stats": ["7"]}]}]}]},
            "scoringPlays": [{"period": {"number": 1}, "clock": {"displayValue": "8:22"},
                              "team": {"abbreviation": "NE"}, "type": {"text": "Rushing Touchdown"},
                              "text": "39 Yd Rush", "awayScore": 0, "homeScore": 7}],
            "drives": {"previous": [{"team": {"abbreviation": "PIT"}, "displayResult": "Punt",
                                     "description": "3 plays, 5 yards", "isScore": False}]}}
    v = summary_view(summ)
    ck("summary: team stat by name", v["team_stats"]["PIT"]["totalYards"] == "251")
    ck("summary: player row keeps name + stats", v["players"]["PIT"]["defensive"]["rows"] == [["T.J. Watt", "7"]])
    ck("summary: first drive result readable", v["drives"][0]["result"] == "Punt")
    ck("summary: scoring play team + type", (v["scoring_plays"][0]["team"], v["scoring_plays"][0]["type"])
       == ("NE", "Rushing Touchdown"))
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["selftest"]:
        sys.exit(selftest())
    if args[:1] == ["schedule"]:
        print("\n".join(json.dumps(r) for r in schedule_rows(_get("teams/pit/schedule"))))
        sys.exit(0)
    if args[:1] == ["summary"] and len(args) == 2 and args[1].isdigit():
        print(json.dumps(summary_view(_get(f"summary?event={args[1]}"))))
        sys.exit(0)
    print("usage: immaculate_espn.py {schedule|summary EVENT_ID|selftest}")
    sys.exit(1)
