"""Nightly build admission: preserve unscoped work without consuming build slots.

No model calls or sends in preflight. The existing builder retains all risk,
review, deployment and delivery rules. This wrapper is the scheduled entry point.
"""
import argparse
from datetime import datetime
from pathlib import Path
import re
import dev_agent as agent


def disposition(item):
    spec=item.get('dev_spec') or {}
    text=' '.join(str(item.get(k,'')) for k in ('detail','source_line')).lower()
    if re.search(r'\b(install|loaded onto|pip install)\b',text):
        return 'operator-task', 'Cowork: verify installation need and environment; do not invent a source-code patch'
    if not spec.get('files_to_change'):
        if re.search(r'\b(research|investigate|locate|find|discover|look at)\b',text) or spec.get('discovery_step'):
            return 'needs-discovery', 'Cowork: complete discovery, identify target files and test plan, then explicitly requeue'
        return 'needs-scope', 'Cowork: identify intended behavior, target files and acceptance test, then explicitly requeue'
    return 'ready', ''


def preflight(project_dir=None):
    builds=agent.builds_load(project_dir)
    held=[]
    for item in agent.find_buildable(project_dir):
        state,action=disposition(item)
        if state=='ready':continue
        spec=item['dev_spec'];bid=spec['id']
        row=dict(id=bid,status='blocked',scope_status=state,error=action,
                 owner='Cowork',next_action=action,source=item.get('source',''),
                 created=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 preflight=True,attempted=False)
        builds=[b for b in builds if b.get('id')!=bid]+[row]
        held.append(row)
    if held:agent.builds_save(builds,project_dir)
    return held


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['nightly','preflight'])
    args=parser.parse_args()
    if args.mode=='nightly':agent.promote_tickets()
    held=preflight()
    print('Preflight held',len(held),'tasks; no build attempts consumed')
    if args.mode=='nightly':agent.run_nightly()


if __name__=='__main__':main()
