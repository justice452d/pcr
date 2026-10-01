"""CME commodity option open-interest PCR radar; SHFE gold legacy view at /shfe."""
import csv, io, json, os, re, threading, urllib.request, webbrowser, shutil
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import mean, pstdev
from urllib.parse import parse_qs, urlparse
import pdfplumber
import shfe_gold
import shfe_others
import cffex_io

BASE=os.path.dirname(__file__)
PCR_HOME=os.environ.get('PCR_HOME', r'H:\\PCR' if os.name=='nt' else BASE)
HK_CACHE=os.path.join(PCR_HOME,'cache','hk_index_pcr')
os.makedirs(HK_CACHE,exist_ok=True)
for _name in ('monthly_oi.csv','prices.csv','dtop_unavailable.json'):
    _seed=os.path.join(BASE,'hk_index_pcr','data',_name)
    _target=os.path.join(HK_CACHE,_name)
    if not os.path.exists(_target) and os.path.exists(_seed):shutil.copy2(_seed,_target)
# Merge packaged older OI and the trading calendar into existing caches.
# Keep user-cache values for dates already present, including later updates.
for _name,_columns in (('monthly_oi.csv',('date','symbol','month')),
                       ('prices.csv',('date','symbol'))):
    _seed=os.path.join(BASE,'hk_index_pcr','data',_name)
    _target=os.path.join(HK_CACHE,_name)
    if not os.path.exists(_seed) or not os.path.exists(_target):continue
    with open(_target,newline='',encoding='utf-8-sig') as _file:
        _reader=csv.DictReader(_file);_fields=list(_reader.fieldnames or []);_cached=list(_reader)
    if not _fields:continue
    _known={tuple(r[column] for column in _columns) for r in _cached}
    with open(_seed,newline='',encoding='utf-8-sig') as _file:
        _added=[r for r in csv.DictReader(_file)
                if tuple(r[column] for column in _columns) not in _known]
    if _added:
        _temporary=_target+'.merge.tmp'
        with open(_temporary,'w',newline='',encoding='utf-8') as _file:
            _writer=csv.DictWriter(_file,_fields);_writer.writeheader()
            _writer.writerows(sorted(_cached+_added,key=lambda r:tuple(r[c] for c in _columns)))
        os.replace(_temporary,_target)
        print(f'已从新版补入 {_name} 历史记录 {len(_added)} 条',flush=True)
os.environ['HK_PCR_DATA']=HK_CACHE
import hk_index_pcr.app as hk
DATA=os.path.join(PCR_HOME,'cache','cme');os.makedirs(DATA,exist_ok=True)
# One-time migration of old package-local CME history.
_legacy_data=os.path.join(BASE,'data')
if os.path.isdir(_legacy_data):
    for _fn in os.listdir(_legacy_data):
        if _fn.endswith('.json') and _fn!='shfe_history.json':
            _src=os.path.join(_legacy_data,_fn); _dst=os.path.join(DATA,_fn)
            if not os.path.exists(_dst):
                try: shutil.copy2(_src,_dst)
                except Exception: pass
ROOT='https://www.cmegroup.com/daily_bulletin/current/'
PDFS={
 'metals':'Section02B_Summary_Volume_And_Open_Interest_Metals_Futures_And_Options.pdf',
 'energy':'Section02C_Summary_Volume_And_Open_Interest_Energy_Futures_And_Options.pdf',
 'corn':'Section56_Corn_Oat_RoughRice_Options.pdf',
 'grain':'Section57_Soybean_Soymeal_Soyoil_SoybeanCrush_wheat_Options.pdf'}
PRODUCTS={
 'gold':('黄金 COMEX','metals','OG','COMEX GOLD OPTIONS'),
 'silver':('白银 COMEX','metals','SO','COMEX SILVER OPTIONS'),
 'copper':('铜 COMEX','metals','HX','COMEX COPPER OPTIONS'),
 'wti':('WTI 原油','energy','LO','NYMEX CRUDE OIL OPTIONS (PHY)'),
 'gas':('天然气','energy','ON','NYMEX NATURAL GAS OPTIONS (PHY)'),
 'corn':('玉米','corn','CORN',''),
 'soybean':('大豆','grain','SOYBEAN',''),
 'wheat':('小麦','grain','WHEAT',''),
 'soyoil':('豆油','grain','SOYBEAN OIL','')}
# Restrict to regular monthly, standard-sized options. Weeklies/micros/CSOs excluded.
_cache={}; _lock=threading.Lock()

def pdf_lines(blob):
 with pdfplumber.open(io.BytesIO(blob)) as reader:
  return '\n'.join(p.extract_text(layout=False) or '' for p in reader.pages).splitlines()

def bulletin_date(lines):
 for line in lines[:90]:
  m=re.search(r'\b(?:Mon|Tue|Wed|Thu|Fri),?\s+(\w+\s+\d{1,2},?\s+20\d\d)',line)
  if m:
   return datetime.strptime(m.group(1).replace(',',''),'%b %d %Y').date().isoformat()
 raise ValueError('CME 公告日期未识别，拒绝使用可能过期的数据')

def summary_oi(lines, code, label):
 found={}
 for raw in lines:
  s=' '.join(raw.split());m=re.match(rf'^{re.escape(code)}\s+{re.escape(label)}\s+([CP])\s+(.+)$',s,re.I)
  if not m:continue
  # Columns: Globex volume, PNT volume, open outcry volume, total volume, total OI, OI change.
  # Absent volume columns disappear in text extraction. OI is immediately before '+'/'-'.
  nums=re.findall(r'\d[\d,]*|[+-]',m.group(2));sign=next((i for i,v in enumerate(nums) if v in ('+','-')),None)
  if sign is None or sign<1:continue
  oi=int(nums[sign-1].replace(',',''))
  found[m.group(1).upper()]=oi
 if not all(found.get(side,0)>0 for side in ('C','P')):raise ValueError(f'{label} 的 C/P 持仓汇总缺失')
 return found['P'],found['C']

def detailed_oi(lines, product):
 # Strike table is ordered CALLS then PUTS. Ignore futures pages and the later exercise appendix.
 headings={'SOYBEAN OIL':('SOYBEAN OIL CALLS','SOYBEAN OIL PUTS'),
           'SOYBEAN':('SOYBEAN CALLS','SOYBEAN PUTS'),
           'WHEAT':('WHEAT CALLS','WHEAT PUTS'),
           'CORN':('CORN CALLS','CORN PUTS')}
 a,b=headings[product]; side=None; totals={'C':0,'P':0}; counts={'C':0,'P':0}
 for raw in lines:
  s=' '.join(raw.split()).upper()
  if 'OPTIONS EOO' in s or 'OPTIONS EOO\'S' in s:break
  if s in (a,product+' CALL'):side='C';continue
  if s in (b,product+' PUT'):side='P';continue
  if re.match(r'^[A-Z][A-Z /-]+ CALLS?$',s) or re.match(r'^[A-Z][A-Z /-]+ PUTS?$',s):
   if s not in (a,b):side=None
   continue
  if side is None or not re.match(r'^\d{2,6}(?:\.\d+)?\s',s):continue
  # Each strike row has OI immediately before OI change (+/-/UNCH).
  matches=list(re.finditer(r'\b(\d[\d,]*)\s+(?:\+|-|UNCH)\s+(?:\d|----)',s))
  m=matches[-1] if matches else None
  if m:
   totals[side]+=int(m.group(1).replace(',',''));counts[side]+=1
 if min(counts.values())<10 or min(totals.values())<=0:raise ValueError(f'{product} 行权价持仓解析不足: {counts}')
 return totals['P'],totals['C']

def extract(lines,section):
 ds=bulletin_date(lines); rows={}
 for key,(_,group,code,label) in PRODUCTS.items():
  if group!=section:continue
  try:
   p,c=(summary_oi(lines,code,label) if section in ('metals','energy') else detailed_oi(lines,code))
   rows[key]={'date':ds,'put_oi':p,'call_oi':c,'pcr':round(p/c,8),'source':'CME Daily Bulletin','scope':'标准月度期权'}
  except ValueError as e:rows[key]={'error':str(e),'date':ds}
 return ds,rows

def refresh(force=False):
 results={}; errors={}
 for section,filename in PDFS.items():
  try:
   if not force and section in _cache and (datetime.now()-_cache[section][0]).total_seconds()<1800:
    ds,rows=_cache[section][1:]
   else:
    req=urllib.request.Request(ROOT+filename,headers={'User-Agent':'Mozilla/5.0','Accept':'application/pdf','Referer':'https://www.cmegroup.com/market-data/daily-bulletin.html'})
    with urllib.request.urlopen(req,timeout=25) as r:blob=r.read()
    if not blob.startswith(b'%PDF'):raise ValueError('返回内容不是 PDF')
    ds,rows=extract(pdf_lines(blob),section)
    _cache[section]=(datetime.now(),ds,rows)
   for key,row in rows.items():
    if 'error' in row:errors[key]=row['error'];continue
    path=os.path.join(DATA,key+'.json'); hist=json.load(open(path,encoding='utf-8')) if os.path.exists(path) else []
    hist=[x for x in hist if x['date']!=ds]+[row];hist.sort(key=lambda x:x['date'])
    with open(path,'w',encoding='utf-8') as f:json.dump(hist[-600:],f,ensure_ascii=False)
    results[key]=row
  except Exception as e:errors[section]=str(e)
 return results,errors

def background_updates():
 """Collect while the server runs, even if no browser tab is open."""
 while True:
  try:
   with _lock:results,errors=refresh(force=True)
   latest=max((x['date'] for x in results.values()),default='无')
   print(f'CME 自动检查：{len(results)} 个品种，{len(errors)} 项错误；最近公告 {latest}',flush=True)
  except Exception as e:
   errors={'background':str(e)}
   print(f'CME 自动检查失败：{e}',flush=True)
  threading.Event().wait(3600 if errors else 6*3600)

def history(key):
 path=os.path.join(DATA,key+'.json')
 rows=json.load(open(path,encoding='utf-8')) if os.path.exists(path) else []
 for i,row in enumerate(rows):
  w=[x['pcr'] for x in rows[max(0,i-19):i+1]]
  row.update(ma20=mean(w) if len(w)==20 else None,
             upper=mean(w)+2*pstdev(w) if len(w)==20 else None,
             lower=mean(w)-2*pstdev(w) if len(w)==20 else None)
 return rows

HTML='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>大宗期权 PCR 雷达</title><style>
body{margin:0;background:#0b1118;color:#e8eef7;font:15px Arial,"Microsoft YaHei",sans-serif}main{max-width:1200px;margin:auto;padding:24px}h1{font-size:30px;margin:0 0 8px}.muted{color:#98a9bf}.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0}.tab,button{background:#14253a;color:#dceaff;border:1px solid #29415c;border-radius:10px;padding:10px 15px;cursor:pointer}.tab.active{background:#215385;border-color:#4d96ce}.card{background:#111d2b;border:1px solid #26374d;border-radius:15px;padding:19px;margin:14px 0}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat strong{display:block;font-size:23px;margin-top:7px}.warn{color:#ffbf78}.ok{color:#82d8ac}canvas{width:100%;height:360px}.foot{line-height:1.7}@media(max-width:650px){main{padding:12px}.stats{grid-template-columns:repeat(2,1fr)}h1{font-size:23px}}
</style><main><h1>大宗期权 PCR 雷达</h1><div class="muted">每日收盘持仓量 PCR = Put OI / Call OI · 20 日均值 ± 2 倍总体标准差 · 标准月度期权</div><div class="tabs" id="tabs"></div><div class="card"><span id="status">正在读取数据…</span> <button onclick="update()">检查最新公告</button></div><div class="stats"><div class="card stat">PCR<strong id="pcr">—</strong></div><div class="card stat">Put OI<strong id="put">—</strong></div><div class="card stat">Call OI<strong id="call">—</strong></div><div class="card stat">交易日期 / 样本<strong id="date">—</strong></div></div><div class="card"><h3 id="chartTitle">PCR 与布林带</h3><canvas id="chart"></canvas><p id="band" class="muted"></p></div><div class="card foot muted">数据：CME Daily Bulletin 官方期权报告；月度标准合约，不含周度、微型、价差期权。数据按公告中的美国交易日标注。历史从本程序首次成功采集后逐日积累，或用历史 CSV 导入；满 20 个交易日才显示布林带。评估预测效果还需要对齐期货价格，并统计触发后的收益。<br>沪金原版：<a href="/shfe" style="color:#78b9ef">打开上期所黄金 PCR 网页</a>（维持原有 15:30 后每 10 分钟更新规则）。<br>这两个市场的合约和参与者不同，不合并计算；期权持仓比例本身不能直接解释为看涨或看跌建议。</div></main><script>
const names={gold:'黄金',silver:'白银',copper:'铜',wti:'WTI 原油',gas:'天然气',corn:'玉米',soybean:'大豆',wheat:'小麦',soyoil:'豆油'};let chosen='gold',db={},issues={};const $=id=>document.getElementById(id);const n=v=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:4});
function tabs(){ $('tabs').innerHTML=Object.entries(names).map(([k,v])=>`<button class="tab ${k===chosen?'active':''}" onclick="select('${k}')">${v}</button>`).join('')+`<a class="tab" href="/shfe">沪金 ↗</a><a class="tab" href="/shfe/silver">沪银 ↗</a><a class="tab" href="/shfe/copper">沪铜 ↗</a><a class="tab" href="/shfe/oil">原油 SC ↗</a><a class="tab" href="/cffex/io">沪深300 IO ↗</a><a class="tab" href="/hk">恒指 / 恒科 ↗</a>` }
function select(k){chosen=k;tabs();load()}
async function update(){ $('status').textContent='正在检查 CME 公告（首次可能需要约一分钟）…';try{let r=await fetch('/api/refresh');let x=await r.json();issues=x.errors||{};$('status').textContent=`已检查：${Object.keys(x.updated||{}).length} 个品种更新；${Object.keys(issues).length} 项未读到。`;load()}catch(e){$('status').textContent='检查失败：'+e.message}}
async function load(){let r=await fetch('/api/history?product='+chosen);let j=await r.json();let a=j.data||[];db[chosen]=a;let x=a.at(-1);$('pcr').textContent=x?n(x.pcr):'—';$('put').textContent=x?n(x.put_oi):'—';$('call').textContent=x?n(x.call_oi):'—';$('date').textContent=x?x.date+' / '+a.length+' 日':'—';$('chartTitle').textContent=names[chosen]+' PCR 与 BB(20,2)';$('band').textContent=x?.ma20!=null?`均值 ${n(x.ma20)} · 下轨 ${n(x.lower)} · 上轨 ${n(x.upper)}`:'样本不足 20 个交易日；从首次成功采集起逐日积累。'+(issues[chosen]?' 当前错误：'+issues[chosen]:'');draw(a)}
function draw(a){let c=$('chart'),dpr=devicePixelRatio||1,w=c.clientWidth,h=360;c.width=w*dpr;c.height=h*dpr;let g=c.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,w,h);let vals=a.flatMap(x=>[x.pcr,x.upper,x.lower]).filter(x=>Number.isFinite(x));if(a.length<2){g.fillStyle='#91a5be';g.font='16px Arial';g.fillText('当前只有 '+a.length+' 个交易日，无法绘制历史曲线或判断信号。',20,90);return}if(!vals.length){g.fillStyle='#91a5be';g.fillText('暂无可绘制历史',20,90);return}let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo)*.15||.1;lo-=pad;hi+=pad;const X=i=>48+(w-64)*i/Math.max(1,a.length-1),Y=v=>h-36-(h-58)*(v-lo)/(hi-lo);g.strokeStyle='#304253';g.fillStyle='#8ea3ba';for(let i=0;i<5;i++){let y=22+i*(h-58)/4;g.beginPath();g.moveTo(48,y);g.lineTo(w-16,y);g.stroke();g.fillText((hi-pad-i*(hi-lo-2*pad)/4).toFixed(2),3,y+4)}for(let [key,color] of [['lower','#75879c'],['upper','#75879c'],['ma20','#efa955'],['pcr','#67b5fa']]){g.strokeStyle=color;g.lineWidth=key==='pcr'?2.5:1.3;g.beginPath();let started=false;a.forEach((x,i)=>{if(x[key]==null){started=false;return}if(!started)g.moveTo(X(i),Y(x[key]));else g.lineTo(X(i),Y(x[key]));started=true});g.stroke()}g.fillStyle='#9eb1c5';g.fillText(a[0].date,48,h-10);g.fillText(a.at(-1).date,w-100,h-10)}
tabs();load();update();setInterval(update,60*60*1000);window.addEventListener('resize',()=>draw(db[chosen]||[]));
</script></html>'''

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,obj,status=200):
  b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store, no-cache, must-revalidate, max-age=0');self.send_header('Pragma','no-cache');self.send_header('Expires','0');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def page(self,html):
  b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store, no-cache, must-revalidate, max-age=0');self.send_header('Pragma','no-cache');self.send_header('Expires','0');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  p=urlparse(self.path)
  if p.path in ('/hk','/hk/'):return self.page(open(os.path.join(BASE,'hk_index_pcr','dashboard.html'),encoding='utf-8').read())
  if p.path=='/hk/api/data':return self.send({'series':hk.series(),'state':hk.STATE})
  if p.path=='/hk/api/backfill':
   try:
    from datetime import date
    from zoneinfo import ZoneInfo
    start=date.fromisoformat(parse_qs(p.query).get('start',[hk.HISTORY_START.isoformat()])[0])
    end=date.fromisoformat(parse_qs(p.query).get('end',[datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat()])[0])
    if end<start or (end-start).days>365*5:raise ValueError('日期范围须在五年以内')
    started=hk.start_backfill(start,end)
    hk.start_dtop_backfill()
    return self.send({'started':True} if started else {'error':'任务正在运行'},200 if started else 409)
   except ValueError as e:return self.send({'error':str(e)},400)
  if p.path=='/':return self.page(HTML)
  if p.path=='/shfe':return self.page(shfe_gold.HTML.replace("'/api/","'/shfe/api/").replace('"/api/','"/shfe/api/').replace('端口：8065。',f'端口：{self.server.server_port}。'))
  if p.path=='/cffex/io':return self.page(cffex_io.HTML)
  if p.path.startswith('/cffex/io/api/'):
   try:
    if p.path.endswith('/latest'):
     x,errors=cffex_io.latest();return self.send({'ok':True,'data':x,'skipped':errors})
    if p.path.endswith('/history'):
     days=max(20,min(520,int(parse_qs(p.query).get('days',['260'])[0])));x,errors=cffex_io.history(days);return self.send({'ok':True,'data':x,'errors':errors})
    if p.path.endswith('/info'):return self.send({'lan_url':f'http://{shfe_gold.local_ip()}:{self.server.server_port}/cffex/io'})
   except Exception as e:return self.send({'ok':False,'error':str(e)},500)
  if p.path in ('/shfe/silver','/shfe/copper','/shfe/oil'):
   product=p.path.rsplit('/',1)[1]
   return self.page(shfe_others.page(product).replace("'/api/",f"'/shfe/{product}/api/").replace('"/api/',f'"/shfe/{product}/api/'))
  if p.path.startswith(('/shfe/silver/api/','/shfe/copper/api/','/shfe/oil/api/')):
   product=p.path.split('/')[2]
   try:
    if p.path.endswith('/latest'):
     x,errors=shfe_others.latest(product);return self.send({'ok':True,'data':x,'skipped':errors})
    if p.path.endswith('/history'):
     days=max(20,min(520,int(parse_qs(p.query).get('days',['35'])[0])))
     x,errors=shfe_others.history(product,days);return self.send({'ok':True,'data':x,'errors':errors})
    if p.path.endswith('/info'):return self.send({'lan_url':f'http://{shfe_gold.local_ip()}:{self.server.server_port}/shfe/{product}'})
   except Exception as e:return self.send({'ok':False,'error':str(e)},500)
  if p.path.startswith('/shfe/api/'):
   try:
    if p.path.endswith('/latest'):
     x,errors=shfe_gold.latest();return self.send({'ok':True,'data':x,'skipped':errors})
    if p.path.endswith('/history'):
     days=max(20,min(520,int(parse_qs(p.query).get('days',['35'])[0])));x,errors=shfe_gold.history(days);return self.send({'ok':True,'data':x,'errors':errors})
    if p.path.endswith('/info'):return self.send({'lan_url':f'http://{shfe_gold.local_ip()}:{self.server.server_port}/shfe'})
   except Exception as e:return self.send({'ok':False,'error':str(e)},500)
  if p.path=='/api/history':
   key=parse_qs(p.query).get('product',['gold'])[0]
   if key not in PRODUCTS:return self.send({'error':'未知品种'},400)
   return self.send({'data':history(key)})
  if p.path=='/api/refresh':
   try:
    with _lock:results,errors=refresh()
    return self.send({'updated':results,'errors':errors})
   except Exception as e:return self.send({'error':str(e)},500)
  return self.send({'error':'Not found'},404)

 def do_POST(self):
  if urlparse(self.path).path!='/hk/api/import':return self.send({'error':'Not found'},404)
  try:
   from datetime import date
   body=self.rfile.read(min(int(self.headers.get('Content-Length','0')),2_000_000))
   req=json.loads(body);day=date.fromisoformat(req['date']);symbol=req['symbol']
   if symbol not in hk.PREFIX:raise ValueError('品种须为 HSI 或 HTI')
   rows=hk.parse_report(req['text'],symbol,day);hk.save_rows(rows)
   return self.send({'months':len(rows),'date':day.isoformat(),'symbol':symbol})
  except (ValueError,KeyError) as e:return self.send({'error':str(e)},400)

def create_server(preferred=8070):
 """Never send a newly launched UI to another version's listening process."""
 try:return ThreadingHTTPServer(('0.0.0.0',preferred),Handler)
 except OSError as e:
  if getattr(e,'errno',None) not in (48,98,10048):raise
  for port in range(8071,8100):
   try:return ThreadingHTTPServer(('0.0.0.0',port),Handler)
   except OSError as retry:
    if getattr(retry,'errno',None) not in (48,98,10048):raise
  raise OSError('8070–8099 均被占用，无法启动新服务')

if __name__=='__main__':
 server=create_server()
 port=server.server_port
 url=f'http://127.0.0.1:{port}/hk?build=v14'
 with open(os.path.join(BASE,'OPEN_CURRENT_HK.url'),'w',encoding='utf-8') as shortcut:
  shortcut.write('[InternetShortcut]\nURL='+url+'\n')
 print(f'PCR Radar integrated v14: {url}',flush=True)
 print(f'请复制上一行地址；旧的 8065、8068 收藏地址仍可能指向旧版。',flush=True)
 threading.Thread(target=background_updates,daemon=True).start()
 hk.start_backfill()
 hk.start_dtop_backfill()
 threading.Thread(target=hk.update_prices,daemon=True).start()
 threading.Thread(target=hk.auto_update_loop,daemon=True).start()
 threading.Timer(1,lambda:webbrowser.open(url)).start()
 server.serve_forever()

