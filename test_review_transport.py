import json, unittest
from foundation_renewal_auto import _parse_review_answer
class TransportTests(unittest.TestCase):
 def test_raw_newline_only_normalizes_reason(self):
  raw='{"verdict":"pass","issue_type":"none","reason":"Verified statement\nwith evidence.","new_span_id":"NONE"}'
  d=_parse_review_answer(raw);self.assertEqual(d['verdict'],'pass');self.assertEqual(d['reason'],'Verified statement with evidence.')
 def test_failure_not_changed(self):
  d=_parse_review_answer('{"verdict":"fail","reason":"Unsupported\nclaim identified."}')
  self.assertEqual(d['verdict'],'fail');self.assertEqual(d['reason'],'Unsupported claim identified.')
 def test_truncation_forbidden(self):
  with self.assertRaises(ValueError):_parse_review_answer(json.dumps({'reason':'x'*385}))
 def test_nonwhitespace_control_forbidden(self):
  with self.assertRaises(ValueError):_parse_review_answer('{"reason":"Evidence\u0001conflict"}')
 def test_control_in_verdict_forbidden(self):
  with self.assertRaises(ValueError):_parse_review_answer('{"verdict":"pa\nss"}')
 def test_valid_json_leading_whitespace_preserved_for_validator(self):
  d=_parse_review_answer(json.dumps({'reason':' invalid leading space'}));self.assertTrue(d['reason'].startswith(' '))
 def test_extra_document_forbidden(self):
  with self.assertRaises(ValueError):_parse_review_answer('{"verdict":"pass"}{"verdict":"fail"}')
if __name__=='__main__':unittest.main()
