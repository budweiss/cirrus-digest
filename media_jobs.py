"""Schedule adapters; existing delivery/cursor owners remain authoritative."""
import json
import hashlib
import os
import re
import sys
from pathlib import Path

import media_pipeline as media


def merge_findings(previous, body):
    """Keep earlier same-day batches, and deduplicate exact replayed evidence."""
    marker = '<!-- yt-batch:' + hashlib.sha256(body.encode()).hexdigest() + ' -->'
    if marker in previous or previous == body or previous.startswith(body + '\n'):
        return previous
    # A crash between findings and cursor writes can replay a *changed* batch.
    # Deduplicate only identical rendered video sections; changed claims/errors
    # remain separate evidence rather than being discarded by video ID alone.
    section_rx = re.compile(r'^## .+?(?=^## |^# YT-WATCH findings|^<!-- yt-batch:|\Z)',
                            re.MULTILINE | re.DOTALL)
    known = {match.group(0).rstrip() for match in section_rx.finditer(previous)}
    omitted = [0]
    def keep_section(match):
        section = match.group(0)
        if section.rstrip() in known:
            omitted[0] += 1
            return ''
        known.add(section.rstrip())
        return section
    fresh = section_rx.sub(keep_section, body)
    if omitted[0]:
        fresh = ('_%d identical video section(s) from this batch already appear above; '
                  'batch totals describe the original attempt._\n\n' % omitted[0]) + fresh
    batch = marker + '\n' + fresh
    return previous.rstrip() + '\n\n' + batch if previous else batch


def youtube():
    import yt_watch as yt
    original_run = yt.run
    def remote_run(**kwargs):
        if any(k in kwargs for k in ('feed_fn','transcript_fn','extract_fn')):
            return original_run(**kwargs)  # injected offline tests
        state = yt.load_json(yt.SEEN_PATH, {'video_ids':[]})
        try:
            response = media.call('youtube', channels=yt.load_channels(),seen=state,
                                  limit=kwargs.get('limit') or yt.DEFAULT_LIMIT)
        except (Exception, media.YouTubeDeadline) as exc:
            if not kwargs.get('dry_run'):
                import job_status
                reason = 'worker_failed'
                if isinstance(exc, media.YouTubeDeadline):
                    reason = 'deadline_expired'
                elif isinstance(exc, media.subprocess.TimeoutExpired):
                    reason = 'ssh_timeout'
                elif isinstance(exc, RuntimeError) and str(exc) == 'youtube_deferred_busy_window':
                    reason = 'deferred_busy_window'
                job_status.record('ytwatch', False, reason + '; cursor preserved')
            raise
        if not kwargs.get('dry_run'):
            for name, body in response['files'].items():
                if not __import__('re').fullmatch(r'yt-watch-\d{4}-\d{2}-\d{2}\.md',name):
                    raise ValueError('invalid_findings_filename')
                yt.OUT_DIR.mkdir(parents=True,exist_ok=True)
                p=yt.OUT_DIR/name
                previous=p.read_text() if p.exists() else ''
                temp=p.with_suffix('.tmp');temp.write_text(merge_findings(previous,body));temp.replace(p)
            media.atomic_json(yt.SEEN_PATH,response['seen'])
        return response['stats']
    yt.run = remote_run
    return yt.main()


def pedagogy():
    import pedagogy_daily as p
    original_summary = p.summarize_for_teacher
    original_transcribe = p.transcribe
    p.transcribe = lambda url: media.call('transcribe',url=url)
    def summarize(kind,title,source,content,cfg):
        if kind != 'podcast episode':
            return original_summary(kind,title,source,content,cfg)
        instructions = p.TEACHER_PROMPT.format(kind=kind,title=title,source=source,content='[source supplied separately]')
        return media.call('analyze',text=content,instructions=instructions,domain='pedagogy',
                          metadata={'title':title,'source':source})
    p.summarize_for_teacher=summarize
    args=sys.argv[1:]
    if '--selftest' in args or 'selftest' in args:
        # Tests exercise unchanged legacy code without network-capable adapters.
        p.summarize_for_teacher=original_summary
        p.transcribe=original_transcribe
        return p.selftest()
    return p.main(dry_run='--dry-run' in args,force='--force' in args)


if __name__=='__main__':
    action=sys.argv.pop(1) if len(sys.argv)>1 else ''
    if action=='youtube':
        raise SystemExit(youtube())
    if action=='pedagogy':
        raise SystemExit(pedagogy())
    raise SystemExit('expected youtube or pedagogy')
