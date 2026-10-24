"""Output checks for migrated council callers, before selection accepts a reply.

These enforce shape and required sections, not factual truth. Quality approval
still requires reviewed task fixtures in the capability registry.
"""
import json
import re


def json_value(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    return json.loads(text)


def medical_brief_valid(text, user):
    """One format gate for live dispatch and qualification; not a factual verdict."""
    # Only the generated task header defines the format, never source/member text.
    header = user.split('\nTHE ITEMS:', 1)[0]
    dated = re.search(r"Write this week's brief, dated (\d{4}-\d{2}-\d{2})\.", header)
    template = re.search(r'Use EXACTLY these sections, in this order:\s*\n'
                         r'((?:## [^\n]+\n?)+)', header)
    if not dated or not template:
        return False
    from datetime import date
    date.fromisoformat(dated.group(1))
    if not text.startswith('Brief date: ' + dated.group(1) + '\n'):
        return False
    required = template.group(1).splitlines()
    actual = re.findall(r'^## [^\n]+', text, re.M)
    if actual != required:
        return False
    bodies = re.split(r'^## [^\n]+\n?', text, flags=re.M)[1:]
    if not all(body.strip() for body in bodies):
        return False
    questions = re.findall(r'^[123]\. ([^\n?]+\?)', header, re.M)
    if len(questions) != 3:
        return False
    # A generator receives source items only; it cannot observe council votes.
    # Determine that role from the trusted task prefix, not source contents.
    if header.startswith("Write this week's brief, dated "):
        disagreements = bodies[required.index('## Council disagreements')]
        if disagreements.strip() != 'No material disagreement.':
            return False
    section = bodies[required.index('## Standing questions — where they stand')]
    return all(section.count(question) == 1 for question in questions)


def prepare_medical_brief(text, user):
    """Order complete named sections; never create, drop or rewrite their prose.

    Only an exact permutation of the trusted six-section template is eligible.
    Date, content, questions and disagreement checks still run on the result.
    Raw replies are retained by callers; this is formatting, not factual approval.
    """
    try:
        if medical_brief_valid(text, user):
            return text
        header = user.split('\nTHE ITEMS:', 1)[0]
        template = re.search(r'Use EXACTLY these sections, in this order:\s*\n'
                             r'((?:## [^\n]+\n?)+)', header)
        if not template:
            return None
        required = template.group(1).splitlines()
        matches = list(re.finditer(r'^## [^\n]+', text, re.M))
        names = [m.group() for m in matches]
        if len(required) != 6 or len(set(required)) != 6 or sorted(names) != sorted(required):
            return None
        blocks = {m.group(): text[m.start():matches[i+1].start() if i+1 < len(matches) else len(text)]
                  for i, m in enumerate(matches)}
        # Only boundary newlines may change when the final block moves inward.
        ordered = text[:matches[0].start()] + '\n\n'.join(blocks[name].rstrip('\n') for name in required)
        return ordered if medical_brief_valid(ordered, user) else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def valid(task, text, user=''):
    try:
        if not isinstance(text, str) or not text.strip():
            return False
        if task == 'yt-watch:extract':
            rows=json_value(text).get('claims')
            return isinstance(rows,list) and all(isinstance(r,dict) and all(isinstance(r.get(k),str) for k in ('claim','why_it_applies','how_to_test')) for r in rows)
        if task == 'pedagogy:source-discovery':
            rows=json_value(text)
            return isinstance(rows,list) and all(isinstance(r,dict) and all(isinstance(r.get(k),str) for k in ('name','type','url','why')) for r in rows)
        if task == 'intake:promise_detect':
            d=json_value(text)
            return isinstance(d,dict) and type(d.get('promise')) is bool and isinstance(d.get('what'),str) and (not d['promise'] or bool(d['what'].strip()))
        if task.startswith('halftime:catalogue:'):
            from halftime_catalogue import parse_acts
            return parse_acts(text, task.rsplit(':',1)[1]) is not None
        if task == 'halftime:events':
            from halftime_routing import parse_events
            return parse_events(text) is not None
        if task == 'privacy:triage':
            rows=[json.loads(line) for line in text.strip().splitlines()]
            return bool(rows) and all(isinstance(r,dict) and r.get('verdict') in ('real','false_positive') and r.get('severity') in ('high','medium','low') and all(isinstance(r.get(k),str) for k in ('target','url','category','why')) for r in rows)
        if task == 'stratus:monthly':
            return text.startswith('### ') and len(text)>200
        if task == 'alopecia-agent:council':
            return len(text)>200
        if task == 'research:decompose':
            from research_task import _valid_decomposition
            return _valid_decomposition(text)
        if task in ('self-review-gate', 'business-idea-gate'):
            suffix = r' \| IDEA: .+' if task == 'business-idea-gate' else ''
            return bool(re.fullmatch(r'SCORE:\s*(?:10|[0-9])\s*\| WHY: .+' + suffix, text.strip()))
        if task == 'business-idea-critique':
            return bool(re.search(r'SURVIVAL:\s*(?:10|[0-9])\b', text))
        if task == 'dev-agent-review':
            return bool(re.fullmatch(r'VERDICT:\s*(?:approve|concerns|reject)\s*\| NOTES: .+', text.strip(), re.I))
        if task == 'dev-agent-repair':
            from dev_agent import parse_model_json, validate_patch
            data = parse_model_json(text)
            if not isinstance(data, dict) or not isinstance(data.get('summary'), str):
                return False
            files, edits = data.get('files'), data.get('edits')
            if not isinstance(files, list) or not isinstance(edits, list):
                return False
            if data['summary'] == 'CANNOT_BUILD':
                return not files and not edits and bool(data.get('notes'))
            if not files and not edits:
                return False
            from dev_agent import patch_path_ok
            return all(isinstance(row,dict) and patch_path_ok(row.get('path',''))[0] and
                       all(isinstance(row.get(k),str) for k in fields)
                       for rows,fields in ((files,('content',)),(edits,('find','replace')))
                       for row in rows)
        if task == 'hoa-leads':
            from hoa_leads.hoa_monitor import parse_screening
            count = len(re.findall(r'^\[\d+\] source=', user, re.M))
            return count > 0 and parse_screening(text, count) is not None
        if task == 'billsnow':
            from snowbrief.bill_snow_weekly import validate_decision
            return validate_decision(json_value(text))
        if task == 'entity-kb-deep-research':
            data=json_value(text)
            return (isinstance(data,dict) and isinstance(data.get('fields'),dict) and
                    isinstance(data.get('signals'),list) and
                    all(k in ('current_mgmt_co','board_contact') and isinstance(v,str) for k,v in data['fields'].items()) and
                    all(isinstance(r,dict) and all(isinstance(r.get(k),str) for k in ('kind','summary','source_url','confidence')) and
                        r['confidence'] in ('high','medium','low') for r in data['signals']))
        if task == 'business-idea-estimate':
            data=json_value(text)
            return isinstance(data,dict) and all(isinstance(data.get(k),str) and data[k].strip() for k in ('build_effort','run_cost','time_to_revenue','first_step'))
        if task == 'business-idea-ideate':
            from business_idea_ideate import _IDEAS_PER_LENS
            rows=json_value(text).get('ideas')
            return isinstance(rows,list) and len(rows)==_IDEAS_PER_LENS and all(isinstance(r,dict) and all(isinstance(r.get(k),str) and r[k].strip() for k in ('name','what','who_pays','demand_evidence','autonomous_loop','needs_building','why_now')) for r in rows)
        if task == 'business-idea-feed-discovery':
            rows=json_value(text).get('feeds')
            return isinstance(rows,list) and all(isinstance(r,dict) and all(isinstance(r.get(k),str) and r[k].strip() for k in ('name','site','why')) and r['site'].startswith('https://') for r in rows)
        if task == 'pedagogy-topic':
            return len(text.split()) <= 450 and not text.startswith('[Summarization')
        if task == 'research:synthesise':
            return 'WHAT I DID NOT CHECK' in text.upper()
        if task == 'alopecia-brief':
            return medical_brief_valid(text, user)
        if task == 'intake-answer':
            return len(text.strip()) >= 80
        return False
    except (ValueError,TypeError,KeyError,AttributeError):
        return False


def selftest():
    failures = []

    def check(desc, cond):
        if not cond:
            failures.append(desc)

    # json_value: plain JSON
    check('json_value plain', json_value('{"a": 1}') == {'a': 1})
    # json_value: fenced code block
    check('json_value fenced', json_value('```json\n{"a": 1}\n```') == {'a': 1})
    # json_value: fenced without language tag
    check('json_value fenced no-lang', json_value('```\n[1,2,3]\n```') == [1, 2, 3])

    # valid: unknown task always False
    check('valid unknown task', valid('no-such-task', 'hello') is False)
    # valid: empty/non-string text always False
    check('valid empty text', valid('intake-answer', '') is False)
    check('valid non-str text', valid('intake-answer', None) is False)

    # intake-answer: length gate
    check('intake-answer too short', valid('intake-answer', 'short') is False)
    check('intake-answer long enough', valid('intake-answer', 'x' * 80) is True)

    # self-review-gate: regex shape
    check('self-review-gate valid', valid('self-review-gate', 'SCORE: 7 | WHY: looks fine') is True)
    check('self-review-gate invalid', valid('self-review-gate', 'SCORE: 11 | WHY: bad') is False)

    # business-idea-gate: requires extra IDEA suffix
    check('business-idea-gate valid', valid('business-idea-gate', 'SCORE: 5 | WHY: ok | IDEA: widget co') is True)
    check('business-idea-gate missing idea', valid('business-idea-gate', 'SCORE: 5 | WHY: ok') is False)

    # dev-agent-review: verdict regex
    check('dev-agent-review valid', valid('dev-agent-review', 'VERDICT: approve | NOTES: looks good') is True)
    check('dev-agent-review invalid', valid('dev-agent-review', 'VERDICT: maybe | NOTES: eh') is False)

    # intake:promise_detect: dict shape with bool/str fields
    check('promise_detect true', valid('intake:promise_detect', '{"promise": true, "what": "call back"}') is True)
    check('promise_detect false no what', valid('intake:promise_detect', '{"promise": false, "what": ""}') is True)
    check('promise_detect true empty what', valid('intake:promise_detect', '{"promise": true, "what": ""}') is False)
    check('promise_detect bad shape', valid('intake:promise_detect', '{"promise": "yes", "what": "x"}') is False)

    # yt-watch:extract: list-of-dicts with required string keys
    good_claims = json.dumps({'claims': [{'claim': 'a', 'why_it_applies': 'b', 'how_to_test': 'c'}]})
    check('yt-watch extract valid', valid('yt-watch:extract', good_claims) is True)
    bad_claims = json.dumps({'claims': [{'claim': 'a'}]})
    check('yt-watch extract invalid', valid('yt-watch:extract', bad_claims) is False)

    # pedagogy-topic: word count + summarization-failure guard
    check('pedagogy-topic valid', valid('pedagogy-topic', 'word ' * 10) is True)
    check('pedagogy-topic too long', valid('pedagogy-topic', 'word ' * 500) is False)
    check('pedagogy-topic summarization failure', valid('pedagogy-topic', '[Summarization failed]') is False)

    # stratus:monthly: heading + min length
    check('stratus monthly valid', valid('stratus:monthly', '### Title\n' + 'x' * 200) is True)
    check('stratus monthly too short', valid('stratus:monthly', '### Title') is False)

    # malformed JSON must be caught, not raise
    check('valid malformed json caught', valid('intake:promise_detect', 'not json') is False)

    if failures:
        for f in failures:
            print('FAIL:', f)
        print('%d/%d checks failed' % (len(failures), len(failures) + 1))
        return False
    print('selftest OK')
    return True


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(0 if selftest() else 1)
