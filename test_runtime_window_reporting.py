"""S607: observed timer shapes cannot be silently excluded from window audit."""
import unittest
import runtime_window as r
import placement

class CalendarReportingTests(unittest.TestCase):
    def test_real_explicit_timezone_forms(self):
        bad=[]
        jobs=r.parse_schedule('''===== CUMULUS (systemd timers) =====
  cirrus-tailscalereview.timer  2026-11-14 14:00:00 UTC
  cowork-promise-route-check.timer  *-*-* *:00:00 UTC [dormant]
  cowork-model-report-cumulus1.timer  *-*-* 07:05:00 America/New_York
''',bad)
        self.assertEqual(bad,[])
        self.assertEqual(len(jobs),26)
        self.assertEqual((jobs[0]['hour'],jobs[0]['date']),(9,'2026-11-14'))
        self.assertTrue(all(j['dormant'] for j in jobs[1:25]))
        self.assertEqual({j['hour'] for j in jobs[1:25]},set(range(24)))
        self.assertEqual((jobs[-1]['hour'],jobs[-1]['minute']),(7,5))
        self.assertEqual(r.profile(jobs[-1]['job'])[0],'light')

    def test_unknown_timezone_still_visible(self):
        bad=[];r.parse_schedule('  odd.timer  *-*-* 07:05:00 Mars/Olympus',bad)
        self.assertEqual(len(bad),1)

    def test_one_shot_dates_do_not_collide(self):
        self.assertFalse(r.calendars_intersect({'date':'2026-11-14'},{'date':'2026-11-21'}))
        self.assertTrue(r.calendars_intersect({'date':'2026-11-14'},{}))

    def test_registry_preserves_timezone_underscore_and_detects_time_drift(self):
        reg=placement.parse_registry('```registry\ncumulus cowork-model-report-cumulus1.timer infra *-*-*_07:05:00_America/New_York live\n```')
        live=placement.parse_live('===== CUMULUS =====\n  cowork-model-report-cumulus1.timer *-*-* 07:05:00 America/New_York ENABLED\n')
        self.assertEqual(placement.check(reg,live)[0],[])
        live[0]['schedule']='*-*-* 08:05:00 America/New_York'
        self.assertTrue(any('DRIFT' in x for x in placement.check(reg,live)[0]))

if __name__=='__main__':unittest.main()
