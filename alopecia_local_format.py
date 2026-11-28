"""Structured, local-only formatting helpers for the Alopecia research brief.

This module makes required presentation deterministic. It does not verify the
medical accuracy of model-written section prose; citations and source review
remain necessary before any brief is sent.
"""

import re

import foundation_contracts as contracts


SECTION_KEYS = ("what_found", "trials", "subgroup", "community")


def response_schema():
    """OpenAI-compatible JSON Schema for vLLM structured output."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["sections", "standing_answers"],
        "properties": {
            "sections": {
                "type": "object",
                "additionalProperties": False,
                "required": list(SECTION_KEYS),
                "properties": {key: {"type": "string", "minLength": 1}
                               for key in SECTION_KEYS},
            },
            "standing_answers": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


def structured_instruction(today):
    """Append exact output instructions while keeping source text untrusted."""
    return (
        "\n\nLOCAL ONLY: return exactly one JSON object matching the supplied JSON "
        "schema, with top-level keys sections and standing_answers. sections "
        "must contain exactly what_found, trials, subgroup, and community as "
        "string values. standing_answers must contain exactly three strings. "
        "Write only the section prose and three short standing-question answers. "
        "Use citations like [1] for supplied items; invent no item numbers. "
        "A trial registration is a plan, not outcome evidence. State when the "
        "supplied material is insufficient. Do not include medical advice. "
        "The program will supply date, headings, exact question titles, and a "
        "single-model disclosure. Requested date: %s." % today
    )


def structured_system(system):
    """Resolve council/Markdown instructions for this single-model JSON pass."""
    return system + (
        "\n\nLOCAL-ONLY OUTPUT OVERRIDE: this is one local model, not a council. "
        "Return only the JSON object required by the response schema. Do not "
        "write Markdown headings, a date, or a council verdict. The assembler "
        "adds the fixed sections and states that no independent council "
        "comparison was run. Do not imply the council agreed."
    )


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def render(raw_json, user, items, full, today):
    """Validate model JSON and deterministically assemble a brief.

    Returns (markdown, error_code). On failure, markdown is None. Error codes
    are safe to log and contain no source or model text.
    """
    import json
    import alopecia_brief as medical

    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "wrong_top_level_type"
    if set(data) != {"sections", "standing_answers"}:
        return None, "wrong_top_level_keys"
    sections = data["sections"]
    answers = data["standing_answers"]
    if not isinstance(sections, dict) or set(sections) != set(SECTION_KEYS):
        return None, "wrong_section_shape"
    if not all(_text(sections[key]) for key in SECTION_KEYS):
        return None, "empty_section"
    if not isinstance(answers, list) or len(answers) != 3 or not all(_text(a) for a in answers):
        return None, "wrong_answer_count"

    question_titles = re.findall(r"^[123]\. (.+)$", medical.STANDING_QUESTIONS, re.M)
    if len(question_titles) != 3:
        return None, "question_template_error"
    headings = (medical.STRUCTURE_FULL if full else medical.STRUCTURE_DELTA).splitlines()
    section_text = {
        headings[0]: sections["what_found"],
        headings[1]: sections["trials"],
        headings[2]: sections["subgroup"],
        headings[3]: sections["community"],
        headings[4]: "\n".join(
            "%d. %s\n   %s" % (i, title, answer.strip())
            for i, (title, answer) in enumerate(zip(question_titles, answers), 1)),
        headings[5]: "Single-model local synthesis; no independent council comparison was run.",
    }
    result = "Brief date: %s\n\n%s" % (
        today, "\n\n".join("%s\n%s" % (heading, section_text[heading]) for heading in headings))
    # Prefix marks this as a local single-model task. The shared contract still
    # validates date, exact headings, non-empty sections and exact questions.
    local_task = "LOCAL ONLY — single-model synthesis\n" + user
    if not contracts.valid("alopecia-brief", result, local_task):
        return None, "brief_contract_failed"
    citations = [int(n) for n in re.findall(r"\[(\d+)\]", result)]
    if items and not citations:
        return None, "no_citations"
    if any(n < 1 or n > len(items) for n in citations):
        return None, "invalid_citation"
    return result, "ok"
