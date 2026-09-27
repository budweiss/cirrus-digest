import json,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import fleet_worker as f
class WorkerTests(unittest.TestCase):
 def test_resource_metadata_only(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'sys/kernel/random').mkdir(parents=True)
   (p/'sys/kernel/random/boot_id').write_text('boot1');(p/'uptime').write_text('22.5 10')
   (p/'meminfo').write_text('MemAvailable: 8192 kB\nSwapTotal: 4096 kB\nSwapFree: 2048 kB\n')
   d=f.facts(p);self.assertEqual(d['available_mib'],8);self.assertEqual(d['swap_used_mib'],2)
   self.assertEqual(set(d),{'host','observed','available_mib','swap_used_mib','boot_id','uptime_seconds'})
 def test_unreadable_is_not_healthy(self):
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(OSError):f.facts(Path(tmp))
 def test_sources_and_routes(self):
  server=ThreadingHTTPServer(('127.0.0.1',0),f.Handler);t=threading.Thread(target=server.serve_forever);t.start()
  try:
   url='http://127.0.0.1:'+str(server.server_port)
   with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+'/health')
   self.assertEqual(e.exception.code,403);e.exception.close()
   with patch.object(f,'ALLOWED',{'127.0.0.1'}),patch.object(f,'facts',return_value={'host':'fixture'}):
    self.assertEqual(json.load(urllib.request.urlopen(url+'/health')),{'host':'fixture'})
    with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url+'/run?cmd=id')
    self.assertEqual(e.exception.code,404);e.exception.close()
    with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(url+'/health',data=b'{}'))
    self.assertEqual(e.exception.code,501);e.exception.close()
  finally:server.shutdown();t.join();server.server_close()
if __name__=='__main__':unittest.main()
