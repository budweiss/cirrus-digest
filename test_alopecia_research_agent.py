"""Run in the actual alopecia SDK venv; no model calls."""
import unittest
from alopecia_agent import agent
class AgentSurfaceTests(unittest.TestCase):
 def test_dry_run_cannot_mutate_research(self):
  names={t.name for t in agent._build_mcp_tools(dry_run=True)}
  self.assertIn('investigate_research_path',names)
  self.assertTrue(names.isdisjoint({'update_research_avenue','medical_research_handoff','consult_research_models','search_research_avenue','record_research_step','append_to_brief_draft','write_hypothesis','mark_run_processed','send_telegram_summary','request_guidance'}))
 def test_no_send_can_record_research_only(self):
  names={t.name for t in agent._build_mcp_tools(no_send=True)}
  self.assertIn('record_research_step',names)
  self.assertIn('medical_research_handoff',names)
  self.assertIn('read_research_memory',names)
  self.assertIn('consult_research_models',names)
  self.assertIn('extract_research_evidence',names)
  self.assertTrue(names.isdisjoint({'write_hypothesis','mark_run_processed','send_telegram_summary','request_guidance'}))
if __name__=='__main__':unittest.main()
