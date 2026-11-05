"""Bounded, resumable creator backfill; CIRRUS orchestration, CUMULUS media.

No sends. Public metadata only. Source gaps remain visible; failures never mean
reviewed. The normal watcher owns ongoing RSS, this job owns a fixed backlog.
"""
import argparse
import json
import re
import time
import urllib.request
from pathlib import Path
import media_pipeline as media
from media_jobs import merge_findings

ROOT = Path(__file__).resolve().parent


def metadata(row):
    """Resolve exact upload date and verify channel; approximate dates aren't proof."""
    vid = row['video_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise ValueError('invalid_video_id')
    req = urllib.request.Request('https://www.youtube.com/watch?v='+vid,
                                 headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=45) as response:
        html = response.read().decode()
    match = re.search(r'ytInitialPlayerResponse\s*=\s*', html)
    if not match:
        raise ValueError('video_metadata_unavailable')
    player = json.JSONDecoder().raw_decode(html[match.end():])[0]
    details = player['videoDetails']
    if details['videoId'] != vid or details['channelId'] != row['channel_id']:
        raise ValueError('video_identity_mismatch')
    published = player['microformat']['playerMicroformatRenderer']['publishDate'][:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', published):
        raise ValueError('missing_exact_date')
    return dict(row, title=details['title'], published=published,
                url='https://www.youtube.com/watch?v='+vid, date_verified=True)


def run(limit=6):
    if not 1 <= limit <= 6:
        raise ValueError('limit_must_be_1_to_6')
    plan = json.loads((ROOT/'yt-watch/creator-backfill.json').read_text())
    state_path = ROOT/'media/creator-backfill-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'items':{},'batches':[]}
    selected=[]; inspected=0
    for source in sorted(plan['videos'], key=lambda r:r['priority']):
        vid=source['video_id']; prior=state['items'].get(vid,{})
        if prior.get('status') in ('complete','source_unavailable','outside_window','held'):
            continue
        if prior.get('attempts',0)>=2:
            state['items'][vid]=dict(prior,status='held')
            continue
        if inspected>=limit:
            break
        inspected+=1
        row=dict(source,**prior.get('metadata',{}))
        attempt=prior.get('attempts',0)+1
        state['items'][vid]=dict(status='in_progress',attempts=attempt,metadata=row)
        media.atomic_json(state_path,state)
        try:
            if not row.get('date_verified'):
                row=metadata(row)
                time.sleep(4)
            state['items'][vid]['metadata']=row
            if row['creator']=='marktilbury' and row['priority']>0 and not (
                    plan['from_date']<=row['published']<=plan['through_date']):
                state['items'][vid]['status']='outside_window'
                media.atomic_json(state_path,state)
                continue
            selected.append(row)
        except Exception as exc:
            state['items'][vid].update(status='held' if attempt>=2 else 'retry',error=type(exc).__name__)
            media.atomic_json(state_path,state)
            # Don't burst through a whole catalog when YouTube is unavailable.
            raise
    if not selected:
        media.atomic_json(state_path,state)
        return status(plan,state)
    channels=[]; videos={}
    for row in selected:
        cid=row['channel_id']
        if cid not in videos:
            channels.append(plan['channels'][row['creator']]); videos[cid]=[]
        videos[cid].append({k:row[k] for k in ('video_id','title','published','url')})
    try:
        response=media.call('youtube', channels=channels,videos=videos,
                            seen={'video_ids':[]},limit=limit)
        # Durable response first: a failed import is recoverable without inference.
        stamp=time.strftime('%Y%m%d-%H%M%S')
        media.atomic_json(ROOT/'media/creator-backfill-batches'/(stamp+'.json'),response)
        for name,body in response['files'].items():
            if not re.fullmatch(r'yt-watch-\d{4}-\d{2}-\d{2}\.md',name):
                raise ValueError('invalid_findings_name')
            path=ROOT/'yt-watch/findings'/name;path.parent.mkdir(parents=True,exist_ok=True)
            previous=path.read_text() if path.exists() else ''
            temp=path.with_suffix('.tmp');temp.write_text(merge_findings(previous,body));temp.replace(path)
        for item in response.get('business_items',[]):
            media.atomic_json(ROOT/'yt-watch/business-inbox'/(item['video_id']+'.json'),item)
        failures={r['video_id']:r['reason'] for r in response.get('caption_failures',[])}
        import yt_watch
        done=set(response['seen']['video_ids'])
        for row in selected:
            item=state['items'][row['video_id']]
            if row['video_id'] in failures:
                reason=failures[row['video_id']]
                disposition=('held' if item['attempts']>=2 else 'retry') if yt_watch.is_transient(reason) else 'source_unavailable'
                item.update(status=disposition,error=reason)
            elif row['video_id'] in done:
                item['status']='complete'
            else:
                item['status']='held' if item['attempts']>=2 else 'retry'
        state['batches'].append(dict(at=stamp,stats=response['stats']))
        media.atomic_json(state_path,state)
    except BaseException:
        for row in selected:
            item=state['items'][row['video_id']]
            item['status']='held' if item['attempts']>=2 else 'retry'
        media.atomic_json(state_path,state)
        raise
    return status(plan,state)


def status(plan,state):
    counts={}
    for row in plan['videos']:
        key=state['items'].get(row['video_id'],{}).get('status','pending')
        counts[key]=counts.get(key,0)+1
    return {'total':len(plan['videos']),'counts':counts,'state':str(ROOT/'media/creator-backfill-state.json')}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=6)
    parser.add_argument('--status',action='store_true');args=parser.parse_args()
    with media.lease(ROOT/'logs/media/creator-backfill.lock',timeout=1):
        if args.status:
            plan=json.loads((ROOT/'yt-watch/creator-backfill.json').read_text())
            p=ROOT/'media/creator-backfill-state.json'
            result=status(plan,json.loads(p.read_text()) if p.exists() else {'items':{}})
        else:
            result=run(args.limit)
        print(json.dumps(result))
