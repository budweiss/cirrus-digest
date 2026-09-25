"""Schedule adapters; existing delivery/cursor owners remain authoritative."""
import json
import os
import sys
from pathlib import Path

import media_pipeline as media


def youtube():
    import yt_watch as yt
    original_run = yt.run
    def remote_run(**kwargs):
        if any(k in kwargs for k in ('feed_fn','transcript_fn','extract_fn')):
            return original_run(**kwargs)  # injected offline tests
        state = yt.load_json(yt.SEEN_PATH, {'video_ids':[]})
        response = media.call('youtube', channels=yt.load_channels(),seen=state,
                              limit=kwargs.get('limit') or yt.DEFAULT_LIMIT)
        if not kwargs.get('dry_run'):
            for name, body in response['files'].items():
                if not __import__('re').fullmatch(r'yt-watch-\d{4}-\d{2}-\d{2}\.md',name):
                    raise ValueError('invalid_findings_filename')
                yt.OUT_DIR.mkdir(parents=True,exist_ok=True)
                p=yt.OUT_DIR/name
                temp=p.with_suffix('.tmp');temp.write_text(body);temp.replace(p)
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
        return media.call('analyze',text=content,instructions=instructions,domain='pedagogy')
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
