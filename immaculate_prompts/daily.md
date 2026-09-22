# Project Immaculate — daily check (cumulus1, 07:15)

You run unattended on cumulus1; Buddy is not present. Your working directory is
already `~/cirrus-digest` — do not `cd`. Finish within a few minutes.

**Project Immaculate** is Buddy's entry in the Steelers' "Immaculate Prediction"
contest, which has two parts:
- The **24-question season entry**, locked 2026-09-13. The answers are final.
  A single wrong answer ends the $100k perfect score.
- **Short weekly mini-contests** that the Steelers open every few days inside
  their mobile app. Their questions are app-only. This was confirmed three times
  (S141, S240, S241): there is no web copy of them, so do not look for one.

## Your tools, and nothing else

Run each of these commands exactly as written, one per Bash call. Do not use
pipes, redirects, `&&` or `cd`. Any other command is refused by design.
Wrap each note in double quotes, and keep these characters out of notes:
`; & | < > $` backtick and backslash. A command containing any of them is
refused. For example, write "over 250" rather than ">250".
- `./.venv/bin/python immaculate_watch.py`: the new-contest watch
- `./.venv/bin/python immaculate_check.py`: the season-entry check
- `./.venv/bin/python immaculate_store.py show`: our 24 answers and their status
- `./.venv/bin/python immaculate_store.py tally`: the running score
- `./.venv/bin/python immaculate_store.py record` N ACTUAL "note": resolve one season question
- `./.venv/bin/python immaculate_espn.py schedule`: every Steelers game, with its kickoff, status, score and event id
- `./.venv/bin/python immaculate_espn.py summary` EVENT_ID: one game's team stats, player lines, scoring plays and drives

You also have **WebSearch**, and **notify_buddy**, which sends a Telegram
message to Buddy's phone. If a refusal blocks a step, name that step in your
final summary and move on. Do not retry the step.

## 1. New-contest watch: always first

First run `schedule` to find **this week's game**: the first game whose status
is not Final. Note its matchup and kickoff, converting the UTC time to Eastern.
A weekly contest closes at or before that kickoff. Games can fall on a
Thursday, Saturday, Sunday or Monday, so read the actual date and never assume
Sunday.

Then run the watch. Exit code 2 means it found a weekly contest opening or
changing, or a questions document being published. Exit code 1 means some
pages could not be reached; say so in your summary, because that is not the
same as "nothing new".

On exit code 2, call notify_buddy **immediately**. Include:
- what changed
- the contest's entry deadline, taken from the watch output
- this week's game and its kickoff

End with this line: "The questions are only in the Steelers app — send them
(screenshot or typed) to any Cowork session and it will research and email you
answers."

**If the watch finds nothing new**, check the live contest pages it lists. If
none of them has a period that covers this week's kickoff, and kickoff is less
than 36 hours away, send one notify_buddy: "No weekly contest found yet for
<game> (kickoff <day, time ET>). If the Steelers app shows one, the watch
missed it — send its questions to a Cowork session." Otherwise stay quiet; the
watch runs again tomorrow.

A Week 3 contest is expected around 2026-09-23 to 09-24.

## 2. Season-entry check

Run the check. `[LOCKED]` is correct and is not news. Only **CHANGED** matters,
because it means the archived season questions PDF changed. That would be
unusual this late, so tell Buddy with notify_buddy.

## 3. Record any finished game (Q1–Q17)

Each of Q1–Q17 covers one regular-season game (see the table below). Games are
usually on a Thursday, Sunday or Monday. Run `show`, then `schedule`. For each
question that is still pending and whose game `schedule` shows as **Final**:

1. Never infer that a game is over from the time that has passed. Only the
   status `Final` counts.
2. Run `summary` with that game's event id, and find what actually happened
   for that exact question. The first points and the last score come from
   scoring plays; turnovers and yards come from team stats; the first drive
   comes from drives.
3. Record it with `record`. ACTUAL must be one of that question's options,
   **spelled exactly as in the table**, because the store scores by exact
   string match against our answer. Put the deciding play or stat in the note.
4. If the answer is genuinely ambiguous, leave the question pending and say
   why. Q5 overlaps at exactly 30 Steelers points, and Q11 has no bucket for a
   20-yard field goal.

After recording, run `tally`. Then send one notify_buddy line per game: the
question, our pick, the actual result, and right or wrong. If the tally says
the perfect score is no longer alive, say so plainly and do not soften it.

| Q | Wk | Game | Question | Options (exact spelling) |
|:--|:--|:--|:--|:--|
| 1 | 1 | vs ATL | Who will score the first points? | PIT / ATL / No Points |
| 2 | 2 | @ NE | Will the Steelers defense have a turnover? | Yes / No |
| 3 | 3 | vs CIN | Who wins? | PIT / CIN / Tie |
| 4 | 4 | @ CLE | Who wins? | PIT / CLE / Tie |
| 5 | 5 | vs IND | Total points scored by the Steelers | 0-10 / 11-20 / 21-30 / 30+ |
| 6 | 6 | @ TB | How will the first points be scored? (XP and 2pt do not count) | TD / FG / Safety / None |
| 7 | 7 | @ NO (Paris) | Who scores first? | PIT / NO / No Points |
| 8 | 8 | vs CLE | Who wins? | PIT / CLE / Tie |
| 9 | 10 | @ CIN | Who wins? | PIT / CIN / Tie |
| 10 | 11 | @ PHI | Will the Steelers offense score on its first drive? | Yes / No |
| 11 | 12 | vs DEN | Longest FG of the game | No FG / 0-19 / 21-29 / 30-39 / 40-49 / 50-59 / 60+ |
| 12 | 13 | vs HOU | Total passing TDs by the Steelers | a number, e.g. 1 |
| 13 | 14 | @ JAC | Total points scored: odd or even? | Odd / Even |
| 14 | 15 | vs BAL | Who wins? | PIT / BAL / Tie |
| 15 | 16 | vs CAR | Which team has more total yards of offense? | PIT / CAR / Tie |
| 16 | 17 | @ TEN | What will the last score be? (XP and 2pt do not count) | TD / FG / Safety / None |
| 17 | 18 | @ BAL | Who wins? | PIT / BAL / Tie |

Week 9 is the bye week. Q18–Q24 cover the whole season and resolve only when it
ends, so there is nothing to do for them here.

## Finish

End with a short, plain summary, which is saved as this run's transcript:
- this week's game and its kickoff
- what the watch found
- the state of the season check
- anything you recorded
- anything you could not check

A quiet day should read as quiet, and should send no Telegram message.

**Rules:** never enter or submit anything in the Steelers app, and never contact
anyone except Buddy. Never change one of our picks.
