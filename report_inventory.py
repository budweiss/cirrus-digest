"""Conservative recognition of report-list checks, without model inference.

Only the bounded inventory email format is recognized. Extra prose/offers stay
on normal promise detection; a question or mixed reply never counts as assent.
"""
import re


def is_inventory_email(body):
    lines = [line.strip() for line in (body or '').splitlines() if line.strip()]
    if lines and re.fullmatch(r'(?:Hi|Hello|Dear) [\w -]+[,!]', lines[0], re.I):
        lines.pop(0)
    if lines and re.fullmatch(r'(?:--|—)\s*(?:CUMULUS|CIRRUS)', lines[-1], re.I):
        lines.pop()
    if len(lines) < 3:
        return False
    if not re.fullmatch(
            r'(?:These are|Here are) the (?:weekly |current |existing )?reports '
            r'(?:our servers|we) (?:currently |already )?send you\.'
            r'(?: All arrive [\w (),:-]+:)?', lines[0], re.I):
        return False
    if not re.fullmatch(
            r'(?:Is this everything you expect from us\?|Please confirm '
            r'(?:this|the) (?:report )?list\.)'
            r'(?: If something is missing or no longer needed, just reply and tell me\.)?',
            lines[-1], re.I):
        return False
    for line in lines[1:-1]:
        # Every item must be a scheduled report. Any action/offer language keeps
        # normal promise handling, even if most of the email is an inventory.
        if not re.match(r'^[-*] .+', line) or not re.search(r'\b\d{1,2}:\d{2}\s*[ap]\.?m\.?', line, re.I):
            return False
        if re.search(r"\b(?:will|shall|can|could|would|build|create|prepare|compile|add|change|remove|resend|offer|if you|happy to|send you)\b|\b(?:we|I)['’]ll\b", line, re.I):
            return False
    return True


def is_plain_assent(body):
    # Full match rather than a yes-prefix: 'yes, also add X' is real work.
    text = re.sub(r"[.! ,;\s]+", ' ', (body or '').strip().lower()).strip()
    return bool(re.fullmatch(
        r"(?:(?:yes|yep|correct|confirmed|that['’]s correct|that is correct|"
        r"that['’]s everything|that is everything|that['’]s all|that is all|"
        r"looks (?:right|correct|good)|all correct|the list is correct|"
        r"this is correct|these are correct|those are correct|"
        r"thanks|thank you)(?: |$))+", text))


def record_outbound(text, creds, *, client, subject, project="general", message_id="", log=None):
    """Mailer bookkeeping after a successful send; existing inference unchanged."""
    import client_promises
    import promise_detect
    inventory = is_inventory_email(text)
    recorded = client_promises.record_thread_context(
        client, subject, "report_inventory" if inventory else "other")
    if not recorded and log:
        log("WARNING: outbound thread context could not be recorded")
    if inventory:
        return None
    return promise_detect.record(text, creds, client=client, subject=subject,
                                 project=project, message_id=message_id, log=log)
