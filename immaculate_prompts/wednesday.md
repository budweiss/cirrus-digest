# Project Immaculate — Wednesday resolve (cumulus1, Wed 06:50)

You run unattended on cumulus1; Buddy is not present. Your working directory is
already `~/cirrus-digest` — do not `cd`. Finish within about 10 minutes.

**Project Immaculate** is Buddy's entry in the Steelers' "Immaculate Prediction"
contest, which has two parts:
- The **24-question season entry**, locked 2026-09-13. The answers are final.
  A single wrong answer ends the $100k perfect score.
- **Short weekly mini-contests** that the Steelers open every few days inside
  their mobile app. Their questions are app-only. This was confirmed several
  times: there is no web copy of them, so do not look for one.

This is the research half of Wednesday. At 09:05 a separate job on this box
(`immaculate-wednesday-report.timer`) emails Buddy one recap built from what
you record here. Your job is to make the stores accurate; the email is the
delivery. The only time you call notify_buddy is in step 4.

## Your tools, and nothing else

Run each of these commands exactly as written, one per Bash call. Do not use
pipes, redirects, `&&` or `cd`. Any other command is refused by design.
Wrap each note in double quotes, and keep these characters out of notes:
`; & | < > $` backtick and backslash. A command containing any of them is
refused. For example, write "over 250" rather than ">250".
- `./.venv/bin/python immaculate_store.py show`: our 24 season answers and their status
- `./.venv/bin/python immaculate_store.py tally`: the season score
- `./.venv/bin/python immaculate_store.py record` N ACTUAL "note": resolve one season question
- `./.venv/bin/python immaculate_store.py snapshot-leader` N "leader" "value" "note/source": for Q18–Q24 only
- `./.venv/bin/python immaculate_weekly_store.py weeks`: the weekly contests on file
- `./.venv/bin/python immaculate_weekly_store.py detail` WEEK: that week's questions, options, our answers and status
- `./.venv/bin/python immaculate_weekly_store.py tally` WEEK: that week's score
- `./.venv/bin/python immaculate_weekly_store.py resolve` WEEK NUM "ACTUAL" "note": resolve one weekly question
- `./.venv/bin/python immaculate_espn.py schedule`: every Steelers game, with its kickoff, status, score and event id
- `./.venv/bin/python immaculate_espn.py summary` EVENT_ID: one game's team stats, player lines, scoring plays and drives
- `./.venv/bin/python immaculate_watch.py`: the new-contest watch

You also have **WebSearch**, and **notify_buddy**, which sends a Telegram
message to Buddy's phone. If a refusal blocks a step, name that step in your
summary and move on. Do not retry the step.

## 1. Season questions Q1–Q17

Run `show`, then `schedule`. For each question that is still pending and whose
game `schedule` shows as **Final**:
1. Never infer that a game is over from the time that has passed. Only the
   status `Final` counts.
2. Run `summary` with that game's event id, and find what actually happened
   for that exact question.
3. Record it with `record`. ACTUAL must be one of that question's options,
   **spelled exactly as in the table**, because the store scores by exact
   string match. Put the deciding play or stat in the note.
4. If the answer is ambiguous, leave the question pending and say why. Q5
   overlaps at exactly 30 points, and Q11 has no bucket for a 20-yard field
   goal.

Never record a question that `show` already lists as resolved. The 07:15 daily
run records these too. Then run `tally`. If the perfect score is no longer
alive, say so plainly and do not soften it.

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

## 2. The most recent weekly mini-contest

Run `weeks`. The highest number it lists is the current week. Run `detail` on
that week. For each question that is still pending and whose game is Final,
use the same `schedule` and `summary` approach to find the real result. Record
it with `resolve`.

Write ACTUAL in the same form as that question's options and our answer. For
example, write a team as the options name it, a player's name as the options
spell it, and a number or range exactly as the options write it. The store
scores by exact string match.

Week 2's questions are a guide to what these usually cover: the winner, the
first TD scorer, total points, total yards, the tackle leader, rushing yards
over/under, a sack bucket, the receiving-yards leader, time of possession, and
passing TDs over/under. The box score answers all of these.

If a result is genuinely ambiguous, leave it pending and say why rather than
guessing. Afterwards, run `tally` for that week.

## 3. Current leaders for Q18–Q24 (informational only, not a resolution)

These questions resolve at the end of the season. Buddy wants a running view
of who is leading. Do one or two targeted WebSearches per question and record
each with `snapshot-leader`:
- Q18: most rushing + receiving TDs (our pick: DK Metcalf)
- Q19: longest offensive TD of the season (our pick: DK Metcalf)
- Q20: Steelers sack leader (our pick: T.J. Watt)
- Q21: Steelers team interceptions (our pick: 16-19)
- Q22: Steelers field goals made (our pick: 26-30)
- Q23: pace for regular-season wins (our pick: 9)
- Q24: AFC North standings (our pick: Ravens)

If a search comes back unclear, skip that question rather than record a guess.
Never use `record` for Q18–Q24.

## 4. New-contest watch

Run the watch. It saves what it has seen, so if you are the one who catches a
new contest, nobody else will. On exit code 2 (a contest opened or changed, or
a questions document was published), call notify_buddy **immediately**. Include
what changed, the contest's entry deadline, and this week's game and kickoff.
This week's game is the first game `schedule` does not show as Final; convert
its UTC kickoff to Eastern, and never assume Sunday. End with this line: "The
questions are only in the Steelers app — send them (screenshot or typed) to any
Cowork session and it will research and email you answers."

A Week 3 contest is expected around 2026-09-23 to 09-24.

## Finish

End with a short, plain summary, which is saved as this run's transcript. Lead
with anything that changed a question's status or any new contest. A quiet
week should read as quiet.

**Rules:** never enter or submit anything in the Steelers app, and never contact
anyone except Buddy. Never change one of our picks.
