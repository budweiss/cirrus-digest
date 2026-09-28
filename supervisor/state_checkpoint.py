"""Preserve non-secret monitoring state before a code rollback.

Stop the supervisor before using this operational command. It creates a NEW
checkpoint, never restores old state over current receipts or decisions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

NAMES = ('heartbeat-incidents.json', 'pending-request.json',
         'guidance-history.json', 'telegram-update-offset.txt')


def state_files(root):
    paths=[root/name for name in NAMES if (root/name).exists()]
    queue=root/'pending-requests'
    if queue.is_symlink():raise ValueError('refusing symlinked decision directory')
    paths+=sorted(queue.glob('*.json'))
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('refusing non-regular monitoring state')
    return paths


def capture(source, destination):
    source=Path(source);destination=Path(destination)
    if destination.exists():raise FileExistsError('checkpoint already exists')
    files=state_files(source)
    if not files:raise ValueError('no monitoring state found')
    contents={str(p.relative_to(source)):p.read_bytes() for p in files}
    # Reject an active writer or newly added request. Operators must stop first.
    if set(contents)!={str(p.relative_to(source)) for p in state_files(source)} or any((source/name).read_bytes()!=data for name,data in contents.items()):
        raise ValueError('state changed; stop supervisor before checkpoint')
    destination.parent.mkdir(parents=True,exist_ok=True)
    scratch=Path(tempfile.mkdtemp(prefix='.checkpoint-',dir=destination.parent))
    try:
        manifest={name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}
        for name,data in contents.items():
            p=scratch/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        (scratch/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        verify(scratch)
        # mkdir fails rather than replacing a concurrently-created checkpoint.
        destination.mkdir(mode=0o700)
        for p in scratch.iterdir():shutil.move(str(p),destination/p.name)
        return manifest
    finally:
        shutil.rmtree(scratch)


def verify(root):
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
    files=state_files(root)
    if set(manifest)!={str(p.relative_to(root)) for p in files}:raise ValueError('checkpoint file set differs')
    for name,digest in manifest.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise ValueError('checkpoint hash differs')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    print('Verified monitoring files:',len(capture(args.source,args.destination)))
