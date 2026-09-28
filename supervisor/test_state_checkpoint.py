"""Rollback export rehearsals in scratch; no access to live state."""
import json
from pathlib import Path
import tempfile
import unittest
import state_checkpoint as checkpoint

class CheckpointTests(unittest.TestCase):
    def test_export_preserves_new_decisions_history_and_receipts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'state';source.mkdir();queue=source/'pending-requests';queue.mkdir()
            (source/'heartbeat-incidents.json').write_text(json.dumps({'incidents':{'snow':{'first_seen':100}},'resolved':[{'incident':'old'}]}))
            (source/'pending-request.json').write_text('{"status":"pending","requested_at":100}')
            (queue/'new.json').write_text('{"status":"consumed","delivery_attempted_at":200}')
            (source/'telegram-update-offset.txt').write_text('203')
            (source/'secrets.json').write_text('{"not_for_export":"fixture only"}')
            destination=root/'checkpoint';before=checkpoint.capture(source,destination)
            self.assertEqual(len(before),4);self.assertFalse((destination/'secrets.json').exists())
            # Simulate the old writer dropping new fields in its scratch state.
            (source/'heartbeat-incidents.json').write_text('{"incidents":{"snow":{"first_seen":100}}}')
            self.assertEqual(checkpoint.verify(destination),before)
            self.assertIn('resolved',json.loads((destination/'heartbeat-incidents.json').read_text()))
            self.assertEqual(json.loads((destination/'pending-requests/new.json').read_text())['status'],'consumed')
            with self.assertRaises(FileExistsError):checkpoint.capture(source,destination)

    def test_empty_symlink_and_tampered_state_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'state';source.mkdir()
            with self.assertRaises(ValueError):checkpoint.capture(source,root/'empty')
            p=source/'pending-request.json';p.symlink_to(root/'outside');(root/'outside').write_text('{}')
            with self.assertRaises(ValueError):checkpoint.capture(source,root/'linked')
            p.unlink();p.write_text('{}');dest=root/'good';checkpoint.capture(source,dest)
            (dest/'pending-request.json').write_text('{"altered":true}')
            with self.assertRaises(ValueError):checkpoint.verify(dest)

if __name__=='__main__':unittest.main()
