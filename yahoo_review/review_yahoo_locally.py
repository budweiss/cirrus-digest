#!/usr/bin/env python3
"""Read-only weekly Yahoo triage report. Inference stays on the local CUMULUS model.
Regenerates the report from the next untracked batch of 20 newest Inbox messages
(any message with a recorded decision in yahoo-actions-queue.json is excluded).
No mail body/attachment fetches, remote model calls, or mailbox mutations here.
Only aggregate progress is printed. Private results stay in local HTML files.
"""
import json, os, sys, html, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yahoo_review_lib as lib  # noqa: E402

def main():
    os.umask(0o077)
    result = lib.generate_next_batch(mode='server')
    if 'error' in result:
        print(json.dumps({'error_type': result['error'], 'details': 'Report not regenerated. Queue status unchanged.'}))
        return 0 if result['error'] == 'nothing_new' else 1
    # Final line is the aggregate, content-free result used by the dispatcher.
    print(json.dumps({k: result[k] for k in ('headers_fetched', 'tracked_excluded', 'counts',
                                           'classification_failures', 'batch_fingerprint')} |
                     {'mailbox_changes': 0, 'message_bodies_fetched': 0, 'mailbox_mode': 'readonly'}))

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        item = lib.headers(b'From: Test <test@example.org>\nSubject: <script>alert(1)</script>\n\n', '1', False)
        item.update(category='review', reason='test')
        assert '&lt;script&gt;' in lib.render([item], 'test', 'file')
        assert '127.0.0.1:8638' in lib.render([item], 'test', 'server')
        try: lib.validate({'items': [{'id': 'wrong', 'category': 'keep', 'reason': 'x'}]}, [item])
        except ValueError: pass
        else: raise AssertionError('bad id accepted')
        assert lib.PROTECTED.search('Your bank statement')
        print('Offline safety checks passed; no mail accessed.')
    else:
        try: sys.exit(main() or 0)
        except SystemExit: raise
        except Exception as error:
            print(json.dumps({'error_type': type(error).__name__, 'details': 'Withheld to protect credentials and mail metadata.'}))
            sys.exit(1)
