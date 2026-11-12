"""Synthetic renewal case regressions; no models, sends or production files."""
import unittest
import job_status as j
import foundation_renewal as f


class CaseLabels(unittest.TestCase):
    def test_finished_auto_review_remains_warning(self):
        rec={'epoch':100,'last_run':'synthetic','ok':False,
             'note':'reviewed=18 renewed=12 held=1 evidence_pending=2 packets=1'}
        line, ok=j._row('foundationrenewalauto',24,rec,101)
        self.assertFalse(ok)
        self.assertIn('REVIEW NEEDED (completed)',line)
        self.assertNotIn('FAILED',line)
        for note in ('error=RuntimeError reason=install_count_mismatch',
                     rec['note']+' changed_since_packet=1', 'reviewed=0 renewed=0 held=0 evidence_pending=0 packets=0'):
            bad=dict(rec,note=note)
            self.assertIn('FAILED',j._row('foundationrenewalauto',24,bad,101)[0])
        self.assertIn('OVERDUE',j._row('foundationrenewalauto',24,rec,100000)[0])

    def test_process_completion_does_not_make_review_healthy(self):
        self.assertEqual(f.process_exit(False,'completed_review; gate rejected'),0)
        self.assertFalse(j._row('foundationrenewalcumulus',24,
            {'epoch':100,'last_run':'synthetic','ok':False,'note':'completed_review; gate rejected'},101)[1])
        for note in ('EXPIRED','error=RuntimeError','telegram FAILED','no fixtures'):
            self.assertEqual(f.process_exit(False,note),1)


if __name__=='__main__':unittest.main()
