"""Check the repository documentation package without accessing local experiment logs."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit,unquote
import hashlib,json,re

paper=Path(__file__).resolve().parent
root=paper.parent
repo=root.parents[2]
errors=[]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def require(ok,message):
    if not ok: errors.append(message)

class Links(HTMLParser):
    def __init__(self): super().__init__();self.links=[];self.ids=set()
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        for name in ['href','src']:
            if name in attrs:self.links.append(attrs[name])
        if 'id' in attrs:self.ids.add(attrs['id'])

files=[f for f in root.rglob('*') if f.is_file() and 'review' not in f.relative_to(root).parts and f.name!='publication-checks.json']
count=0
for f in files+[repo/'README.md',repo/'docs/README.md']:
    if f.suffix not in ['.md','.html','.json','.py','.mjs','.css','.svg']:continue
    text=f.read_text()
    require(re.search(r'/Users/[A-Za-z0-9_-]+/',text) is None,'Machine path: '+str(f.relative_to(repo)))
    require(re.search(r'file:///[A-Za-z0-9_-]+/',text) is None,'Machine file URL: '+str(f.relative_to(repo)))
    require(not re.search(r'(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9_]{24,}|github_pat_[A-Za-z0-9_]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)',text),'Possible credential: '+str(f.relative_to(repo)))
    if f.suffix not in ['.md','.html']:continue
    p=Links();p.feed(text)
    refs=p.links+(re.findall(r'\[[^\]]*\]\(([^)]+)\)',text) if f.suffix=='.md' else [])
    for ref in refs:
        u=urlsplit(ref.strip('<>'))
        if u.scheme or u.netloc:continue
        if not u.path:
            if u.fragment and f.suffix=='.html':require(u.fragment in p.ids,'Missing anchor: '+str(f.name)+' '+ref)
            continue
        target=(f.parent/unquote(u.path)).resolve()
        require(target.is_relative_to(repo),'Link outside repository: '+ref)
        if target==paper/'publication-checks.json':continue
        count+=1;require(target.exists(),'Missing local link: '+str(f.relative_to(repo))+' '+ref)

m=json.loads((root/'data/metrics.json').read_text())
md=(root/'iboard-experiment-report.md').read_text()
rows=[r for r in md.splitlines() if r.startswith('| [#')]
require(len(rows)==10,'Need ten benchmark rows')
for c in m['cases']:
    row=next((r for r in rows if '[#'+str(c['issue'])+' ' in r),'')
    require(c['active_hms'] in row,'Duration mismatch '+str(c['issue']))
    require(re.search(r'(?<!\d)'+str(c['points'])+r'分',row.split('|')[2] if row else '') is not None,'Score mismatch '+str(c['issue']))
require(sum(c['points'] for c in m['cases'])==80,'Wrong sum')
require(sum(c['outcome']=='solved' for c in m['cases'])==8,'Wrong accepted count')
require(len(re.findall(r'^## ',md,re.M))==6,'Need six main sections')

arch=[]
for base in ['iboard_as_is','iboard_to_be']:
    d=paper/'architecture';r=json.loads((d/(base+'.receipt.json')).read_text())
    require(r['ok'] and r['validation']['checksPassed']==9 and r['validation']['errors']==r['validation']['warnings']==0,'Archify validation '+base)
    require(sha(d/(base+'.archify.html'))==r['artifact']['sha256'],'Changed delivered HTML '+base)
    require(sha(d/(base+'.archify.json'))==r['specification']['sha256'],'Changed delivered specification '+base)
    arch.append({'diagram':base,'showcase_checks':9,'errors':0,'warnings':0,'specification_sha256':r['specification']['sha256'],'html_sha256':r['artifact']['sha256']})

browser_file=paper/'review/browser-check.json'
browser=None
if browser_file.exists():
    browser=json.loads(browser_file.read_text())
    require(browser['status']=='pass','Browser failed')
    require(browser['htmlSha256']==sha(root/'iboard-experiment-report.html'),'Stale browser check')
    browser={k:v for k,v in browser.items() if k!='errors'}
result={'status':'pass' if not errors else 'fail','published_on':'2026-11-15','report_revision':'2026-11-12','experiment_materials_as_of':'2026-11-07','scope':'Documentation package; no model/product experiments rerun','score':80,'accepted_cases':8,'total_cases':10,'local_link_occurrences_checked':count,'archify':arch,'browser':browser,'artifacts':{str(f.relative_to(root)):sha(f) for f in files},'errors':errors}
(paper/'publication-checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['status','local_link_occurrences_checked','score','accepted_cases','errors']},ensure_ascii=False))
raise SystemExit(bool(errors))
