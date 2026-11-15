"""Use synthetic diskutil output; never read a real unlock key or change a disk."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
VOLUME_UUID = 'B98DC866-AC87-4988-A3AC-12F7813EA3BF'


class StableBackupVolume(unittest.TestCase):
    def test_mounted_volume_survives_bsd_device_renumbering(self):
        for device in ['disk7s2', 'disk10s2']:
            with self.subTest(device=device), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                source = (ROOT / 'tm_unlock.sh').read_text()
                source = source.replace('PASSFILE="/etc/cirrus-tm-passphrase"', f'PASSFILE="{base}/absent-key"')
                source = source.replace('LOG="/var/log/cirrus-tm-unlock.log"', f'LOG="{base}/log"')
                script = base / 'unlock.sh'
                script.write_text(source)
                fake = base / 'diskutil'
                fake.write_text(f'''#!/bin/bash
if [ "$1" = info ] && [ "$2" = {VOLUME_UUID} ]; then
  echo 'Device Identifier: {device}'
  echo 'Mounted: Yes'
  exit 0
fi
exit 1
''')
                fake.chmod(0o755)
                result = subprocess.run(['bash', str(script)], env={**os.environ, 'PATH': f'{base}:/usr/bin:/bin'}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)
                self.assertFalse((base / 'log').exists())

    def test_passphrase_setup_uses_same_stable_default(self):
        source = (ROOT / 'set_tm_passphrase.sh').read_text()
        assignment = next(line for line in source.splitlines() if line.startswith('VOL='))
        result = subprocess.run(['bash', '-c', assignment + '\nprintf "%s" "$VOL"'], capture_output=True, text=True)
        self.assertEqual(result.stdout, VOLUME_UUID)


if __name__ == '__main__':
    unittest.main()
