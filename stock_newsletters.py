"""Owned-machine newsletter evidence selection; no network, inference or trading.

Scan the complete saved text, retain a disposition for every issue, and offer a
bounded, publisher-diverse packet. A received email date is NOT an article date.
"""
import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

VERSION = 's537-v1'
WINDOW_DAYS = 7
MAX_EXCERPTS = 16
EXCERPT_CHARS = 1200
MAX_CONTEXT_CHARS = 20000
MAX_FILE_BYTES = 2_000_000
# Never admit brokerage/holdings notices or personalized ticker alerts.
PERSONAL = re.compile(r'\b(?:your|my)\s+(?:portfolios?|holdings|watchlist|account balance|positions)\b|'
                      r'stocks you follow|alerts for the|portfolio digest|portfolio health|'
                      r'account statement|trade confirmation|account number|balance summary', re.I)
FINANCE = re.compile(r'\b(?:stocks?|equities|earnings|dividends?|valuation|buybacks?|'
                     r'cash flow|revenue|inflation|interest rates?|bond yields?|'
                     r'investing|investments?|market outlook|securities|share price)\b', re.I)
SA_PRODUCT = re.compile(r'because\s+you\s+(?:[a-z]+\s+){0,3}?(?:to\s+receive|to|for)\s+(.{3,120}?)(?:\.\s|\.$|\s+If\s+you|$)', re.M)
# Stable publisher identities; other approved senders still require finance text.
PUBLISHERS = {'seekingalpha.com':'Seeking Alpha', 'morningstar.com':'Morningstar',
 'investopedia.com':'Investopedia', 'finance.comms.yahoo.net':'Yahoo Finance',
 'newsletters.yahoo.net':'Yahoo Finance', 'analystratings.net':'AnalystRatings',
 'market.briefs.co':'Market Briefs', 'fisherinvestments.com':'Fisher Investments',
 'wsj.com':'Wall Street Journal', 'fool.com':'Motley Fool'}


def sender_allowed(addr, rules):
    addr = addr.strip().lower()
    host = addr.rsplit('@', 1)[-1]
    rules = [x.strip().lower() for x in rules if isinstance(x, str) and x.strip()]
    return any((addr.endswith(x) or host == x[1:]) if x.startswith('.') else addr == x for x in rules)


def publisher(addr):
    host = addr.rsplit('@', 1)[-1]
    for suffix, name in PUBLISHERS.items():
        if host == suffix or host.endswith('.'+suffix):
            return name, suffix
    # Shared email platforms are NOT one independent publisher.
    return addr, host


def message_date(text):
    m = re.search(r'^> From: `[^`]+` · Date: (.*?) · Fetched:', text, re.M)
    if not m:return None
    try:
        d = parsedate_to_datetime(m[1])
        if d.tzinfo is None:return None
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):return None


def passages(text):
    # Contiguous excerpts with offsets: never manufacture a stitched quote.
    for start in range(0, len(text), 1000):
        yield start, text[start:start+EXCERPT_CHARS]


def company_terms(companies):
    terms = []
    for c in companies:
        ticker = c.get('ticker', '')
        name = c.get('company', {}).get('name', '') or c.get('name', '')
        if ticker:
            # Short English tickers need explicit market syntax.
            if len(ticker) < 3:
                terms.append(re.compile(r'(?:\$|NASDAQ:\s*|NYSE:\s*|\()'+re.escape(ticker)+r'\b'))
            else:terms.append(re.compile(r'(?<![A-Za-z])'+re.escape(ticker)+r'(?![A-Za-z])'))
        clean = re.sub(r'\b(?:Inc|Corporation|Corp|Ltd|Company|Holdings)\b\.?', '', name, flags=re.I).strip(' ,.')
        if len(clean) >= 5:terms.append(re.compile(re.escape(clean), re.I))
    return terms


def public_links(text):
    """Reference candidates only. Never fetch personalized email/tracking URLs."""
    links=[]
    for url in re.findall(r'https?://[^\s<>\[\]"\)]+', text):
        try:
            p=urlsplit(url.rstrip('.,;'))
            if p.username or p.password or p.query or p.fragment:continue
            if not p.hostname or re.search(r'click|track|unsubscribe|preferences|login|account',p.hostname+p.path,re.I):continue
            if p.scheme!='https':continue
            if p.geturl() not in links:links.append(p.geturl())
        except ValueError:continue
    return links[:20]


def collect(folder, now, companies, allowlist, rejectlist=(), max_excerpts=MAX_EXCERPTS):
    """Return private evidence packet and a full audit. No account data leaves host.

    All files are screened; text admitted to a model is bounded. Counts distinguish
    deterministic screening from model review, including unknown/stale dates.
    """
    cutoff=now-timedelta(days=WINDOW_DAYS)
    audit=[]; candidates=[]; seen=set(); reasons=Counter(); terms=company_terms(companies)
    for path in sorted(folder.glob('*.md')):
        rec={'file':path.name}; audit.append(rec)
        def exclude(reason):
            rec['disposition']=reason; reasons[reason]+=1
        try:
            if path.is_symlink():exclude('symlink');continue
            if path.stat().st_size>MAX_FILE_BYTES:exclude('oversize');continue
            text=path.read_text(encoding='utf-8')
        except (OSError,UnicodeError):exclude('unreadable');continue
        rec['sha256']=hashlib.sha256(text.encode()).hexdigest()
        m=re.search(r'^> From: `([^`]+)`',text,re.M)
        if not m:exclude('unidentified_sender');continue
        addr=m[1].lower(); name,host=publisher(addr)
        if sender_allowed(addr,rejectlist):exclude('rejected_sender');continue
        if not sender_allowed(addr,allowlist):exclude('unapproved_sender');continue
        body=text.split('\n---\n',1)[-1]
        if PERSONAL.search(text):exclude('personal_or_followed');continue
        if host=='seekingalpha.com' and not SA_PRODUCT.search(text):exclude('unidentified_sa_product');continue
        stamp=message_date(text)
        if stamp is None:exclude('unknown_date');continue
        if stamp>now+timedelta(minutes=5):exclude('future_date');continue
        if stamp<cutoff:exclude('stale');continue
        if not FINANCE.search(body):exclude('not_financial');continue
        # The same issue in both inboxes and repeated fetches gets one vote.
        identity=hashlib.sha256((addr+'\n'+re.sub(r'\s+',' ',body).strip()).encode()).hexdigest()
        if identity in seen:exclude('duplicate');continue
        seen.add(identity)
        chunks=list(passages(body))
        def relevance(chunk):return sum(bool(rx.search(chunk)) for rx in terms)
        # Scan all chunks, prefer relevant evidence wherever it occurs, then
        # finance density. Ties choose earlier passages, never filename ordering.
        ranked=sorted(chunks,key=lambda x:(-relevance(x[1]),-len(FINANCE.findall(x[1])),x[0]))
        if not ranked:exclude('empty');continue
        offset,excerpt=ranked[0]
        title=text.splitlines()[0].removeprefix('# ') if text.startswith('# ') else 'Newsletter issue'
        archive_date=path.name[:8]
        archive_date=archive_date[:4]+'-'+archive_date[4:6]+'-'+archive_date[6:8]
        row={'id':identity[:20],'source':name,'site':host,'email_date':stamp.isoformat(),
             'archive_date':archive_date,'title':title[:240],'text':excerpt,
             'source_sha256':rec['sha256'],'offset':offset,'body_chars':len(body),
             'urls':public_links(body)[:3],'relevance':relevance(excerpt),
             'notice':'Secondary opinion; email date is not article publication date. Exact partial excerpt; verify claims in dated primary sources.'}
        rec.update(source=name,email_date=row['email_date'],id=row['id'],disposition='deferred_context_budget',
                   body_chars=len(body),source_truncated='TRUNCATED:' in text.upper())
        candidates.append((row,rec))
    # One relevant item per publisher first; round-robin prevents one high-volume
    # newsletter from crowding out the rest. Within a publisher: relevance, date.
    groups=defaultdict(list)
    for row,rec in candidates:groups[row['source']].append((row,rec))
    for g in groups.values():g.sort(key=lambda x:(x[0]['relevance'],x[0]['email_date'],x[0]['id']),reverse=True)
    order=sorted(groups,key=lambda k:(groups[k][0][0]['relevance'],groups[k][0][0]['email_date'],k),reverse=True)
    selected=[]; used=0
    while len(selected)<max_excerpts and any(groups.values()):
        progress=False
        for source in order:
            if not groups[source] or len(selected)>=max_excerpts:continue
            row,rec=groups[source].pop(0)
            if used+len(row['text'])>MAX_CONTEXT_CHARS:continue
            selected.append(row);used+=len(row['text']);rec['disposition']='offered_to_model';progress=True
        if not progress:break
    per_source={}
    for row,rec in candidates:
        counts=per_source.setdefault(row['source'],{'eligible':0,'offered':0})
        counts['eligible']+=1;counts['offered']+=rec['disposition']=='offered_to_model'
    stats={'version':VERSION,'scanned':len(audit),'eligible':len(candidates),'selected':len(selected),
           'deferred':len(candidates)-len(selected),'excluded':dict(reasons),'sources':per_source,
           'window_days':WINDOW_DAYS,'context_chars':used,'archive_exists':folder.is_dir()}
    return {'newsletter_excerpts':selected,'newsletter_coverage':stats,'audit':audit}


def coverage_text(stats):
    text=('Newsletter coverage: %d saved issues screened; %d recent eligible issues from %d publishers; '
          '%d excerpts offered to the local decision model; %d deferred by the context budget. '
          'Window: %d days by email date, not archive date. Full websites/articles were not fetched in this pass.' %
          (stats['scanned'],stats['eligible'],len(stats['sources']),stats['selected'],stats['deferred'],stats['window_days']))
    if stats['excluded']:
        text+=' Exclusions: '+', '.join(k.replace('_',' ')+'='+str(v) for k,v in sorted(stats['excluded'].items()))+'.'
    if not stats['selected']:text+=' WARNING: no eligible newsletter evidence was offered.'
    for source,counts in sorted(stats['sources'].items()):
        text+='\n- '+source+': '+str(counts['offered'])+' excerpts offered from '+str(counts['eligible'])+' eligible issues.'
    return text
