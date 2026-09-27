import unittest
from fleet_hermes import proposal,RULE_HASH
class HermesPolicyTests(unittest.TestCase):
 def test_E04_E05_scope_and_destructive_requests(self):
  i={'job_id':'one','state':'cancelled','backend_idle':True,'delivery':'none','retries_left':1}
  for args,rule in [({'job_id':'other','action':'retry'},RULE_HASH),({'job_id':'one','action':'cancel'},RULE_HASH),({'job_id':'one','action':'restart-service'},RULE_HASH),({'job_id':'one','action':'retry'},'wrong'),({'job_id':'one','action':'retry','shell':'id'},RULE_HASH)]:
   self.assertFalse(proposal(i,args,rule)['accepted'])
 def test_E07_E08_known_repair_and_uncertainty(self):
  i={'job_id':'one','state':'cancelled','backend_idle':True,'delivery':'none','retries_left':1}
  a={'job_id':'one','action':'retry'}
  self.assertTrue(proposal(i,a,RULE_HASH)['accepted'])
  for key,value in [('state','running'),('backend_idle',False),('delivery','unknown'),('retries_left',0)]:
   bad=dict(i);bad[key]=value;self.assertFalse(proposal(bad,a,RULE_HASH)['accepted'])
  self.assertTrue(proposal({}, {'job_id':None,'action':'wait'},'wrong')['accepted'] is False)
if __name__=='__main__':unittest.main()
