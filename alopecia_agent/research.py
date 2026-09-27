"""S321 active, bounded hypothesis investigation. Public inputs, local drafts only.

No arbitrary outbound query/URL, patient-profile input, sends or ranking changes.
Sources and proposed mechanisms remain evidence inputs, never instructions.
"""
import fcntl
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT/'alopecia/research-state'
CATALOG = ROOT/'alopecia/research-agenda.json'
EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
MAX_REQUESTS = 12


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2));tmp.replace(path)


def catalog():
    return json.loads(CATALOG.read_text())


def agenda():
    cfg=catalog();steps=load(STATE/'steps.json',[])
    last={s['path_id']:s['created'] for s in steps}
    paths=sorted(cfg['paths'],key=lambda p:last.get(p['id'],''))
    from alopecia_agent import notebook
    return {'notebook':notebook.summary(),'mission':cfg['mission'],'paths':paths,'recent_steps':steps[-6:],
            'suggested_path':paths[0]['id'],'lead_sources':cfg['lead_sources'],
            'scope':'Research hypotheses, not established causes or individual treatment advice.'}


def reserve_request():
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'network.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE/'network-budget.json';d=load(path,{})
        today=date.today().isoformat()
        if d.get('day')!=today:d={'day':today,'used':0,'last':0}
        if d['used']>=MAX_REQUESTS:raise ValueError('daily_research_network_cap')
        time.sleep(max(0,.4-(time.time()-d['last'])))
        d.update(used=d['used']+1,last=time.time());save(path,d)


def fetch(endpoint, params):
    reserve_request()
    url=EUTILS+endpoint+'?'+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'User-Agent':'Cowork-alopecia-research/1.0'})
    with urllib.request.urlopen(req,timeout=25) as r:
        raw=r.read(2_000_001)
    if len(raw)>2_000_000:raise ValueError('source_too_large')
    return raw


def parse_articles(raw):
    tree=ET.fromstring(raw);records=[]
    for item in tree.findall('.//PubmedArticle'):
        pmid=item.findtext('./MedlineCitation/PMID')
        if not pmid or not re.fullmatch(r'\d{1,9}',pmid):continue
        title=''.join(item.find('./MedlineCitation/Article/ArticleTitle').itertext())
        abstracts=[' '.join(x.itertext()) for x in item.findall('.//Abstract/AbstractText')]
        text='\n'.join(abstracts)
        types=[x.text for x in item.findall('.//PublicationType') if x.text]
        records.append({'id':'pmid:'+pmid,'title':title,'text':text[:12000],
            'url':'https://pubmed.ncbi.nlm.nih.gov/'+pmid+'/',
            'publication_types':types,'abstract_only':True,'truncated':len(text)>12000,
            'retraction_flag':any('retract' in x.lower() for x in types) or bool(item.findall('.//CommentsCorrections[@RefType="RetractionIn"]')),
            'fetched_utc':datetime.now(timezone.utc).isoformat()})
    return records


def get_articles(ids, origin):
    ids=[str(i) for i in ids if re.fullmatch(r'\d{1,9}',str(i))][:5]
    if not ids:return []
    records=parse_articles(fetch('efetch.fcgi',{'db':'pubmed','id':','.join(ids),'retmode':'xml'}))
    for record in records:
        record['origin']=origin
        save(STATE/'sources'/(record['id'].replace(':','-')+'.json'),record)
        digest=hashlib.sha256(record['text'].encode()).hexdigest()
        save(STATE/'source-versions'/(record['id'].replace(':','-')+'-'+digest+'.json'),record)
    return records


def source(source_id):
    if not re.fullmatch(r'(pmid:\d{1,9}|site:[a-z0-9-]+)',source_id):raise ValueError('unknown_source')
    if source_id.startswith('site:') and source_id.split(':')[1] not in SITE_LEADS:raise ValueError('unknown_source')
    p=STATE/'sources'/(source_id.replace(':','-')+'.json')
    if not p.exists():raise ValueError('source_not_retrieved')
    return load(p,{})


def investigate(path_id):
    cfg=catalog();path=next((p for p in cfg['paths'] if p['id']==path_id),None)
    if not path:raise ValueError('unknown_research_path')
    steps=load(STATE/'steps.json',[])
    rounds=sum(s['path_id']==path_id for s in steps)
    query_id=path['queries'][rounds % len(path['queries'])]
    query=cfg['queries'][query_id]
    raw=fetch('esearch.fcgi',{'db':'pubmed','term':query,'retmode':'json','retmax':5,'sort':'relevance' if rounds%2==0 else 'pub date'})
    result=json.loads(raw)['esearchresult']
    records=get_articles(result['idlist'],{'path_id':path_id,'query_id':query_id})
    search={'path_id':path_id,'query_id':query_id,'query':query,'count':int(result['count']),
            'retrieved':[r['id'] for r in records],'at':datetime.now(timezone.utc).isoformat()}
    save(STATE/'latest-search.json',search)
    return {'search':search,'sources':records,'instruction':'Separate initiation, maintenance and relapse. Assess contrary evidence, design, species and applicability; a plausible mechanism is not a proven cause.'}


def follow_related(source_id):
    original=source(source_id)
    raw=fetch('elink.fcgi',{'dbfrom':'pubmed','db':'pubmed','id':source_id.split(':')[1],
                          'linkname':'pubmed_pubmed','retmode':'json'})
    data=json.loads(raw);ids=[]
    for group in data.get('linksets',[]):
        for links in group.get('linksetdbs',[]):
            if links.get('linkname')=='pubmed_pubmed':ids.extend(links.get('links',[]))
    return {'relationship':'NCBI related articles, NOT verified citations',
            'sources':get_articles([i for i in ids if str(i)!=source_id.split(':')[1]],{'related_to':original['id']})}


def extract_evidence(question, source_ids):
    """Fresh retrieved abstracts use the already-qualified C2 extractor."""
    import alopecia_medical as medical
    import local_specialists as specialist
    if not isinstance(question,str) or not 1<=len(question)<=4000:raise ValueError('invalid_question')
    if not isinstance(source_ids,list) or not 1<=len(source_ids)<=3:raise ValueError('invalid_source_list')
    records=[source(i) for i in source_ids]
    if any(r['retraction_flag'] for r in records):raise ValueError('retracted_evidence_refused')
    passages={'S'+str(i+1):{'text':r['text'][:5000]} for i,r in enumerate(records) if r['text']}
    if not passages:return {'claims':[],'abstain':True,'reason':'no_abstract_text'}
    try:
        result=specialist.request('http://192.168.100.11:8012','/evidence',{'question':question,'passages':passages},timeout=300)
    except urllib.error.HTTPError as exc:
        if exc.code!=422:raise
        try:rejection=json.loads(exc.read(2048))
        finally:exc.close()
        reasons={'invalid_evidence_schema','inconsistent_abstention','unsupported_quote','invalid_evidence_output'}
        if rejection.get('error')!='evidence_rejected' or rejection.get('reason') not in reasons:raise ValueError('invalid_rejection_receipt')
        audit={'status':'rejected','reason':rejection['reason'],'source_ids':source_ids,'at':datetime.now(timezone.utc).isoformat()}
        save(STATE/'latest-extraction.json',audit)
        return dict(audit,claims=[],abstain=True,instruction='Model output rejected; no accepted evidence. Do not retry the same question. Retain the limitation; use one focused question on a later investigation.')
    if result.get('host')!='cumulus2' or result.get('model')!='medgemma-text:27b-q8_0' or result.get('unloaded') is not True:raise ValueError('invalid_worker_receipt')
    evidence=medical.validate(json.dumps(result['evidence']),passages)
    save(STATE/'latest-extraction.json',{'status':'validated','source_ids':source_ids,'at':datetime.now(timezone.utc).isoformat(),'model':result['model'],'unloaded':True,'claim_count':len(evidence['claims']),'abstain':evidence['abstain']})
    evidence.update(source_mapping={'S'+str(i+1):r['id'] for i,r in enumerate(records)},scope='Retrieved source text only; preserve abstract-only, institutional and company-statement distinctions',model=result['model'])
    return evidence


def record_step(data):
    required={'path_id','hypothesis','supporting','contradicting','uncertainties','falsifier','next_step','solution_direction'}
    extra={'avenue_id','compare_to','comparison','study_context'}
    if not isinstance(data,dict) or set(data) not in (required,required|extra):raise ValueError('invalid_step_fields')
    if data['path_id'] not in {p['id'] for p in catalog()['paths']}:raise ValueError('unknown_research_path')
    for field in ['hypothesis','uncertainties','falsifier','next_step','solution_direction']:
        if not isinstance(data[field],str) or not 15<=len(data[field])<=2500:raise ValueError('incomplete_'+field)
    prose=' '.join(data[f] for f in ['hypothesis','uncertainties','falsifier','next_step','solution_direction'])
    if re.search(r'\b(no stud(?:y|ies) (?:has|have) ever|confirmed literature gap|never been (?:studied|measured|investigated))\b',prose,re.I):
        raise ValueError('unsupported_exhaustive_absence_claim: describe only what this bounded search retrieved')
    for field in ['supporting','contradicting']:
        if not isinstance(data[field],list) or len(data[field])>5:raise ValueError('invalid_evidence_list')
        for claim in data[field]:
            if not isinstance(claim,dict) or set(claim)!={'source_id','quote'}:raise ValueError('invalid_quote')
            record=source(claim['source_id']);quote=claim['quote']
            if record['retraction_flag'] or not isinstance(quote,str) or len(quote)<30 or quote not in record['text']:raise ValueError('unverified_quote')
    if 'avenue_id' in data:
        from alopecia_agent import notebook
        notebook.validate_comparison(data)
    if not data['supporting'] and not data['contradicting']:
        search=load(STATE/'latest-search.json',{})
        frontier_zero=any(s.get('avenue_id')==data.get('avenue_id') and s.get('status')=='ok' and s.get('count')==0 for s in load(STATE/'searches.json',[])) if 'avenue_id' in data else False
        if not frontier_zero and (search.get('path_id')!=data['path_id'] or search.get('count')!=0):raise ValueError('evidence_or_zero_result_required')
    snapshots={}
    for claim in data['supporting']+data['contradicting']:
        record=source(claim['source_id'])
        snapshots[record['id']]={k:record[k] for k in ['url','fetched_utc','abstract_only','publication_types','truncated']}
        snapshots[record['id']]['text_sha256']=hashlib.sha256(record['text'].encode()).hexdigest()
    data=dict(data,source_snapshots=snapshots,created=datetime.now(timezone.utc).isoformat(),status='UNREVIEWED RESEARCH HYPOTHESIS — not a causal finding or treatment recommendation')
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'steps.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        steps=load(STATE/'steps.json',[]);data['step_id']='step-%05d' % (len(steps)+1);steps.append(data);save(STATE/'steps.json',steps)
    from alopecia_agent import notebook
    notebook.write_report()
    return {'saved':True,'step_id':data['step_id'],'status':data['status'],'path_id':data['path_id']}


def read_lead(lead_id):
    if lead_id in SITE_LEADS:
        return fetch_site(lead_id)
    names={'podcast':'EP290-CLAIM-LEDGER.md','labs':'LABS-REGISTER.md'}
    if lead_id not in names:raise ValueError('unknown_lead')
    return {'status':'Historical research lead; reverify claims and dates before using as evidence',
            'text':(ROOT/'alopecia/research-inputs'/names[lead_id]).read_text()[:35000]}


def retrieve_source(source_id):
    if not re.fullmatch(r'pmid:\d{1,9}',source_id):raise ValueError('invalid_public_record_id')
    records=get_articles([source_id.split(':')[1]],{'route':'public PMID from research lead'})
    if not records:raise ValueError('public_record_missing')
    return records[0]


SITE_LEADS = {
    'naaf': ('https://www.naaf.org/registry/', 'registry history and public research leads, not patient-level data'),
    'geo68801': ('https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE68801', 'public expression dataset metadata; age/onset coverage unverified'),
    'niams': ('https://www.niams.nih.gov/health-topics/alopecia-areata/more-info', 'institutional research overview'),
    'unither': ('https://ir.unither.com/press-releases', 'company announcements, not independent efficacy evidence'),
}

class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed=urllib.parse.urlparse(newurl)
        if parsed.scheme!='https' or parsed.hostname!=urllib.parse.urlparse(req.full_url).hostname or parsed.port not in (None,443):
            raise ValueError('unapproved_source_redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class PageText(HTMLParser):
    def __init__(self):super().__init__();self.skip=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip and data.strip():self.parts.append(data.strip())

def fetch_site(lead_id):
    url,kind=SITE_LEADS[lead_id];reserve_request()
    req=urllib.request.Request(url,headers={'User-Agent':'Cowork-alopecia-research/1.0'})
    with urllib.request.build_opener(SameHostRedirect()).open(req,timeout=25) as response:
        raw=response.read(2_000_001)
    if len(raw)>2_000_000:raise ValueError('source_too_large')
    parser=PageText();parser.feed(raw.decode('utf-8',errors='replace'));text=' '.join(parser.parts)
    if len(text)<100:raise ValueError('source_text_unavailable')
    record={'id':'site:'+lead_id,'title':kind,'url':url,'text':text[:20000],
            'publication_types':[kind],'abstract_only':False,'truncated':len(text)>20000,
            'retraction_flag':False,'fetched_utc':datetime.now(timezone.utc).isoformat()}
    save(STATE/'sources'/('site-'+lead_id+'.json'),record)
    return record
