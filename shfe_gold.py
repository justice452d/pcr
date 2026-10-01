import json, os, re, ssl, subprocess, sys, threading, urllib.request, webbrowser, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from statistics import mean, pstdev
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST='0.0.0.0'; PORT=8065
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36'
BASE=os.path.dirname(os.path.abspath(__file__))
# v9: all versions share one persistent Windows data root. Override with PCR_HOME if needed.
PCR_HOME=os.environ.get('PCR_HOME', r'H:\\PCR' if os.name=='nt' else BASE)
CACHE_DIR=os.path.join(PCR_HOME,'cache'); os.makedirs(CACHE_DIR,exist_ok=True)
MEM={}
OPT='https://www.shfe.com.cn/data/tradedata/option/dailydata/kx{date}.dat'
FUT='https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat'


def local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(('8.8.8.8',80))
        ip=s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:return socket.gethostbyname(socket.gethostname())
        except:return '127.0.0.1'

def fnum(x):
    try:
        if x is None:return None
        s=str(x).replace(',','').strip()
        if s in ('','-','--','None','nan','NaN'):return None
        return float(s)
    except:return None

def fetch(url, timeout=20):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Referer':'https://www.shfe.com.cn/'})
    try:
        with urllib.request.urlopen(req,timeout=timeout,context=ssl.create_default_context()) as r:
            b=r.read(); return b,'urllib'
    except Exception as e1:
        if os.name=='nt':
            p=subprocess.run(['curl.exe','-L','--silent','--show-error','--max-time',str(timeout),'-A',UA,'-e','https://www.shfe.com.cn/',url],capture_output=True,timeout=timeout+5)
            if p.returncode==0 and p.stdout:return p.stdout,'curl.exe'
            raise RuntimeError(f'urllib={e1}; curl={(p.stderr or b"").decode("utf-8","ignore")}')
        raise

def get_json(kind, ds):
    key=(kind,ds)
    if key in MEM:return MEM[key]
    path=os.path.join(CACHE_DIR,f'{kind}_{ds}.json')
    if os.path.exists(path):
        try:
            obj=json.load(open(path,'r',encoding='utf-8')); MEM[key]=(obj,'disk-cache'); return MEM[key]
        except: pass
    url=(OPT if kind=='op' else FUT).format(date=ds)
    raw,tr=fetch(url)
    txt=raw.decode('utf-8-sig','ignore').lstrip()
    if not txt.startswith('{'):
        raise RuntimeError(f'{ds} 返回的不是JSON（可能尚未公布）: {txt[:60]!r}')
    obj=json.loads(txt)
    if not isinstance(obj.get('o_curinstrument'),list):raise RuntimeError('JSON里没有 o_curinstrument')
    try:
        with open(path,'w',encoding='utf-8') as f: json.dump(obj,f,ensure_ascii=False)
    except: pass
    MEM[key]=(obj,tr); return MEM[key]

def parse_option(ds):
    obj,tr=get_json('op',ds)
    put=call=0.0; np=nc=0; gold_rows=0; unmatched=[]; sample=[]
    for r in obj['o_curinstrument']:
        code=str(r.get('INSTRUMENTID','')).replace(' ','').strip()
        pname=str(r.get('PRODUCTNAME','')).strip()
        if pname!='黄金期权' and not code.lower().startswith('au'): continue
        if not code or code in ('小计','合计'): continue
        gold_rows+=1
        oi=fnum(r.get('OPENINTEREST'))
        if oi is None: continue
        if len(sample)<8: sample.append({'code':code,'pname':pname,'oi':oi})
        # tolerate au2610C920, AU2610P920, au2610-c-920 etc.
        u=code.upper()
        side=None
        m=re.search(r'AU\d{3,4}[^CP]*([CP])',u)
        if m: side=m.group(1)
        else:
            # fallback: last C/P occurring after AU prefix
            tail=u[2:] if u.startswith('AU') else u
            cps=[(tail.rfind('C'),'C'),(tail.rfind('P'),'P')]; pos,side=max(cps)
            if pos<0: side=None
        if side=='P': put+=oi; np+=1
        elif side=='C': call+=oi; nc+=1
        else:
            if len(unmatched)<12:unmatched.append(code)
    if call<=0 or (np+nc)==0:
        raise RuntimeError(f'找到了黄金行 {gold_rows} 条，但无法识别C/P；样例={sample}; 未匹配={unmatched}')
    return {'date':datetime.strptime(ds,'%Y%m%d').strftime('%Y-%m-%d'),'put_oi':put,'call_oi':call,'pcr':put/call,'put_contracts':np,'call_contracts':nc,'gold_rows':gold_rows,'sample':sample,'transport':tr}

def parse_future(ds):
    try: obj,tr=get_json('fu',ds)
    except:return None
    arr=[]
    for r in obj['o_curinstrument']:
        code=str(r.get('INSTRUMENTID') or '').replace(' ','').strip()
        pname=str(r.get('PRODUCTNAME','')).strip(); pid=str(r.get('PRODUCTID','')).strip().lower()
        if not code and pid=='au_f':
            month=str(r.get('DELIVERYMONTH') or '').strip()
            if re.fullmatch(r'\d{4}',month):code='au'+month
        if not (re.fullmatch(r'au\d{3,4}',code,re.I) or pname=='黄金' or pid in ('au','au_f')): continue
        oi=fnum(r.get('OPENINTEREST')); close=fnum(r.get('CLOSEPRICE'))
        if oi is not None and close is not None: arr.append((oi,close,code))
    if not arr:return None
    oi,close,code=max(arr,key=lambda x:x[0]); return {'gold_close':close,'gold_contract':code,'gold_oi':oi}

def weekday_dates(n=40):
    out=[]; cur=datetime.now()
    while len(out)<n:
        if cur.weekday()<5:out.append(cur.strftime('%Y%m%d'))
        cur-=timedelta(days=1)
    return out

def latest():
    errors=[]
    for ds in weekday_dates(8):
        try:
            op=parse_option(ds); fu=parse_future(ds)
            if fu:op.update(fu)
            return op,errors
        except Exception as e: errors.append(f'{ds}: {e}')
    raise RuntimeError('最近8个工作日都没有有效数据 | '+' | '.join(errors))

def one_day(ds):
    op=parse_option(ds); fu=parse_future(ds)
    if fu:op.update(fu)
    return op

def history(days=25):
    # BB(20,2) is calculated from the same underlying history regardless of the
    # selected display range. Fetch 19 warm-up trading days, calculate first,
    # then trim to the requested number of visible days.
    warmup=19
    need=days+warmup
    cands=weekday_dates(need+20)
    got=[]; errs=[]
    def task(ds):
        try:return ds,one_day(ds),None
        except Exception as e:return ds,None,str(e)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs=[ex.submit(task,ds) for ds in cands]
        for f in as_completed(futs):
            ds,row,err=f.result()
            if row:got.append(row)
            else:errs.append((ds,err))
    got.sort(key=lambda x:x['date'])
    if len(got)>need:got=got[-need:]
    vals=[x['pcr'] for x in got]
    for i,x in enumerate(got):
        if i>=19:
            w=vals[i-19:i+1]; m=mean(w); sd=pstdev(w); x.update(ma20=m,upper=m+2*sd,lower=m-2*sd)
        else:x.update(ma20=None,upper=None,lower=None)
    return got[-days:],errs[-6:]

HTML=r'''<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>黄金 PCR 雷达 V16</title><style>
body{margin:0;background:#0b1118;color:#e8eef7;font-family:Arial,'Microsoft YaHei',sans-serif}.wrap{padding:20px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.title{font-size:31px;font-weight:800}.tag{font-size:13px;color:#62b5ff}.sub{color:#93a4bc;margin:8px 0 14px}.msg{background:#10243b;border:1px solid #183654;color:#afd2ff;padding:11px 14px;border-radius:12px;margin:8px 0 14px;white-space:pre-wrap}.msg.err{background:#35191b;border-color:#643033;color:#ffd3d6}.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.card,.panel{background:#111b27;border:1px solid #223247;border-radius:16px;padding:16px}.lab{color:#91a5be;font-size:14px}.val{font-size:28px;font-weight:750;margin-top:8px}.charts{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;margin-top:14px}.panel{min-height:455px}.panel h3{margin:0 0 6px;color:#9db2cc;font-size:16px}.legend{display:flex;gap:14px;flex-wrap:wrap;color:#8fa5bf;font-size:12px;margin:0 0 4px}.dot{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}.btn{background:#122338;color:#eaf2ff;border:1px solid #2c4058;border-radius:12px;padding:10px 16px;cursor:pointer}.btn:hover{background:#18304c}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 0}.sel{background:#0e1a28;color:#dce8f7;border:1px solid #2c4058;border-radius:9px;padding:8px 10px}.mini{padding:7px 10px;font-size:12px}svg{width:100%;height:390px;overflow:visible}.footer{margin-top:12px;color:#8294aa;font-size:13px}.tip{position:fixed;pointer-events:none;display:none;background:#08121f;border:1px solid #38516e;border-radius:9px;padding:8px 10px;color:#eaf2ff;font-size:12px;box-shadow:0 8px 22px #0008;z-index:20;white-space:pre-line}.marketnav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}.marketnav a{display:inline-block;text-decoration:none;background:#122338;color:#cfe4ff;border:1px solid #2c4058;border-radius:10px;padding:8px 12px;font-size:13px}.marketnav a.active{background:#215385;border-color:#4d96ce;color:#fff}.marketnav a:hover{background:#18304c}@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}.charts{grid-template-columns:1fr}}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.wrap{padding:10px}.title{font-size:25px}}
</style></head><body><div class='wrap'><div class='marketnav'><a href='/cffex/io'>沪深300 IO</a><a class='active' href='/shfe'>沪金 AU</a><a href='/shfe/silver'>沪银 AG</a><a href='/shfe/copper'>沪铜 CU</a><a href='/shfe/oil'>原油 SC</a><a href='/hk'>恒指 / 恒科</a></div><div class='top'><div><div class='title'>黄金 PCR 雷达 V16 <span class='tag'>收盘后智能更新 + 局域网版</span></div><div class='sub'>SHFE AU 黄金期权持仓量 PCR = ΣPut OI / ΣCall OI · BB(20,2) · 15:30后每10分钟检查，更新成功后当天停止</div></div><button class='btn' onclick='refreshLatest(false).then(()=>loadHist())'>立即刷新</button></div><div id='msg' class='msg'>正在读取最近一个可用交易日…</div><div class='cards'>
<div class='card'><div class='lab'>最新 PCR</div><div class='val' id='pcr'>—</div></div><div class='card'><div class='lab'>Put OI</div><div class='val' id='po'>—</div></div><div class='card'><div class='lab'>Call OI</div><div class='val' id='co'>—</div></div><div class='card'><div class='lab'>20日均线</div><div class='val' id='ma'>—</div></div><div class='card'><div class='lab'>布林下轨 / 上轨</div><div class='val' id='bb'>—</div></div><div class='card'><div class='lab'>数据日期 / 沪金</div><div class='val' id='date' style='font-size:19px'>—</div></div></div>
<div class='toolbar'><span class='lab'>历史范围</span><select id='range' class='sel' onchange='changeRange()'><option value='35' selected>35日</option><option value='60'>60日</option><option value='120'>120日</option><option value='260'>260日</option><option value='520'>520日</option></select><button class='btn mini' onclick='resetZoom()'>复位缩放</button><span class='lab'>操作：滚轮缩放 · 左键拖动 · 双击复位</span><span class='lab' id='autost'>自动刷新：等待首次加载</span><span class='lab' id='lanurl'></span></div><div class='charts'><div class='panel'><h3>PCR + BB(20,2)</h3><div class='legend'><span><i class='dot' style='background:#62b5ff'></i>PCR</span><span><i class='dot' style='background:#f0a24a'></i>MA20</span><span><i class='dot' style='background:#8a9bb0'></i>BB 上/下轨</span></div><svg id='pcrChart'></svg></div><div class='panel'><h3>沪金代表合约收盘价</h3><div class='legend'><span><i class='dot' style='background:#62b5ff'></i>收盘价</span></div><svg id='goldChart'></svg></div></div><div class='footer'>V16：日频智能更新。每个工作日 15:30 后每10分钟检查一次；发现当天新数据后停止当天检查。手机和同一局域网内其他电脑都可访问。端口：8065。</div></div><div id='tip' class='tip'></div><script>
const $=id=>document.getElementById(id);window.histDays=35;window.histData=[];window.viewStart=0;window.viewEnd=0;window.dragState=null;
function fmt(x,n=4){return x==null?'—':Number(x).toFixed(n)};

function visibleData(){let a=window.histData||[];if(!a.length)return a;if(window.viewEnd<=window.viewStart||window.viewEnd>a.length){window.viewStart=0;window.viewEnd=a.length;}return a.slice(window.viewStart,window.viewEnd)}
function renderAll(){let a=visibleData();draw($('pcrChart'),a,['pcr','ma20','upper','lower']);draw($('goldChart'),a,['gold_close'],{price:true});attachZoom($('pcrChart'));attachZoom($('goldChart'));}
function resetZoom(){window.viewStart=0;window.viewEnd=(window.histData||[]).length;renderAll()}
async function changeRange(){window.histDays=parseInt($('range').value);$('msg').textContent=`正在加载 ${window.histDays} 个交易日历史…首次可能较慢，之后会使用缓存。`;await loadHist();}
function attachZoom(svg){if(svg.dataset.zoomBound)return;svg.dataset.zoomBound='1';
 svg.addEventListener('wheel',e=>{e.preventDefault();let a=window.histData||[];if(a.length<10)return;let n=window.viewEnd-window.viewStart;if(n<=0)n=a.length;let rect=svg.getBoundingClientRect();let frac=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));let anchor=window.viewStart+frac*n;let nn=Math.round(n*(e.deltaY>0?1.22:0.82));nn=Math.max(20,Math.min(a.length,nn));let ns=Math.round(anchor-frac*nn);ns=Math.max(0,Math.min(a.length-nn,ns));window.viewStart=ns;window.viewEnd=ns+nn;renderAll();},{passive:false});
 svg.addEventListener('mousedown',e=>{window.dragState={x:e.clientX,s:window.viewStart,e:window.viewEnd};});
 window.addEventListener('mouseup',()=>window.dragState=null);
 svg.addEventListener('mousemove',e=>{if(!window.dragState)return;let a=window.histData||[];let n=window.dragState.e-window.dragState.s;let rect=svg.getBoundingClientRect();let dx=e.clientX-window.dragState.x;let shift=Math.round(-dx/Math.max(1,rect.width)*n);let ns=Math.max(0,Math.min(a.length-n,window.dragState.s+shift));window.viewStart=ns;window.viewEnd=ns+n;renderAll();});
 svg.addEventListener('dblclick',e=>{e.preventDefault();resetZoom();});}

function draw(svg,data,keys,opts={}){const W=920,H=390,L=64,R=18,T=22,B=46;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML='';let vals=[];keys.forEach(k=>data.forEach(d=>{if(d[k]!=null&&isFinite(+d[k]))vals.push(+d[k])}));if(!vals.length){svg.innerHTML='<text x="64" y="60" fill="#7890aa">历史数据仍在加载</text>';return}let mn=Math.min(...vals),mx=Math.max(...vals),pad=(mx-mn)*.08;if(!pad)pad=Math.max(Math.abs(mx)*.03,.01);mn-=pad;mx+=pad;const X=i=>L+(W-L-R)*i/Math.max(1,data.length-1),Y=v=>H-B-(H-T-B)*(v-mn)/(mx-mn);
for(let g=0;g<5;g++){let y=T+(H-T-B)*g/4,val=mx-(mx-mn)*g/4;svg.innerHTML+=`<line x1="${L}" y1="${y}" x2="${W-R}" y2="${y}" stroke="#203044"/><text x="${L-8}" y="${y+4}" text-anchor="end" fill="#7890aa" font-size="11">${opts.price?val.toFixed(2):val.toFixed(3)}</text>`}
let ticks=Math.min(6,data.length);for(let t=0;t<ticks;t++){let i=Math.round((data.length-1)*t/Math.max(1,ticks-1)),x=X(i),lab=(data[i].date||'').slice(5);svg.innerHTML+=`<line x1="${x}" y1="${H-B}" x2="${x}" y2="${H-B+5}" stroke="#38506a"/><text x="${x}" y="${H-18}" text-anchor="middle" fill="#7890aa" font-size="11">${lab}</text>`}
const cols=['#62b5ff','#f0a24a','#8a9bb0','#8a9bb0'];keys.forEach((k,ki)=>{let seg=[],parts=[];data.forEach((d,i)=>{if(d[k]!=null){seg.push(`${X(i)},${Y(+d[k])}`)}else if(seg.length){parts.push(seg);seg=[]}});if(seg.length)parts.push(seg);parts.forEach(pts=>{if(pts.length>1)svg.innerHTML+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${cols[ki]}" stroke-width="${ki===0?2.5:1.55}"/>`})});
const ns='http://www.w3.org/2000/svg';
const cross=document.createElementNS(ns,'g');cross.style.display='none';cross.style.pointerEvents='none';
const vl=document.createElementNS(ns,'line');vl.setAttribute('y1',T);vl.setAttribute('y2',H-B);vl.setAttribute('stroke','#6f8298');vl.setAttribute('stroke-dasharray','4 4');
const hl=document.createElementNS(ns,'line');hl.setAttribute('x1',L);hl.setAttribute('x2',W-R);hl.setAttribute('stroke','#50647b');hl.setAttribute('stroke-dasharray','3 4');
const dot=document.createElementNS(ns,'circle');dot.setAttribute('r','4');dot.setAttribute('fill','#62b5ff');dot.setAttribute('stroke','#dcecff');dot.setAttribute('stroke-width','1');
cross.append(vl,hl,dot);svg.appendChild(cross);
const hit=document.createElementNS(ns,'rect');hit.setAttribute('x',L);hit.setAttribute('y',T);hit.setAttribute('width',W-L-R);hit.setAttribute('height',H-T-B);hit.setAttribute('fill','transparent');hit.style.cursor='crosshair';svg.appendChild(hit);
function clientToSvgPoint(e){let pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;let ctm=svg.getScreenCTM();return ctm?pt.matrixTransform(ctm.inverse()):{x:L,y:T}}
hit.addEventListener('pointermove',e=>{let p=clientToSvgPoint(e),frac=(p.x-L)/(W-L-R),i=Math.max(0,Math.min(data.length-1,Math.round(frac*(data.length-1)))),x=X(i),d=data[i],primary=d[keys[0]],y=(primary!=null&&isFinite(+primary))?Y(+primary):Math.max(T,Math.min(H-B,p.y));vl.setAttribute('x1',x);vl.setAttribute('x2',x);hl.setAttribute('y1',y);hl.setAttribute('y2',y);dot.setAttribute('cx',x);dot.setAttribute('cy',y);cross.style.display='';let labels={pcr:'PCR',ma20:'MA20',upper:'upper',lower:'lower',gold_close:'close'};let lines=[d.date||''];keys.forEach(k=>{if(d[k]!=null)lines.push(`${labels[k]||k}: ${opts.price?Number(d[k]).toFixed(2):Number(d[k]).toFixed(4)}`)});let tip=$('tip');tip.textContent=lines.join('\n');tip.style.display='block';let tw=190,th=112,left=e.clientX+14,top=e.clientY+12;if(left+tw>innerWidth)left=e.clientX-tw-14;if(top+th>innerHeight)top=e.clientY-th-14;tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px'});hit.addEventListener('pointerleave',()=>{cross.style.display='none';$('tip').style.display='none'})}
async function refreshLatest(silent=false){let m=$('msg');try{let r=await fetch('/api/latest?ts='+Date.now());let j=await r.json();if(!r.ok)throw Error(j.error);let x=j.data;let old=$('date').dataset.date||'';$('pcr').textContent=fmt(x.pcr);$('po').textContent=Math.round(x.put_oi).toLocaleString();$('co').textContent=Math.round(x.call_oi).toLocaleString();$('date').textContent=x.date+(x.gold_contract?` / ${x.gold_contract} ${fmt(x.gold_close,2)}`:'');$('date').dataset.date=x.date;let now=new Date();$('autost').textContent='自动刷新：'+now.toLocaleTimeString()+' 已检查';if(!silent)m.textContent=`最新数据已加载：${x.date}。后台正在加载历史和布林带…`;if(old && old!==x.date){await loadHist();}return x}catch(e){$('autost').textContent='自动刷新失败：'+e.message;if(!silent){m.className='msg err';m.textContent='最新 PCR 读取失败：'+e.message;}return null}}
async function loadLan(){try{let r=await fetch('/api/info');let j=await r.json();if(j.lan_url){$('lanurl').textContent='手机：'+j.lan_url}}catch(e){}}
let updateTimer=null;let baselineDate='';
function ymd(d=new Date()){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function nextCheckDelay(){let n=new Date(), t=new Date(n);t.setHours(15,30,0,0);if(n.getDay()===0){t.setDate(n.getDate()+1)}else if(n.getDay()===6){t.setDate(n.getDate()+2)}else if(n>=t){return 0}return Math.max(1000,t-n)}
function scheduleSmartUpdate(){if(updateTimer)clearTimeout(updateTimer);let delay=nextCheckDelay();if(delay>0){let when=new Date(Date.now()+delay);$('autost').textContent='自动更新：下次检查 '+when.toLocaleString();updateTimer=setTimeout(startCloseChecks,delay);return}startCloseChecks()}
async function startCloseChecks(){let n=new Date();if(n.getDay()===0||n.getDay()===6){scheduleSmartUpdate();return}if(n.getHours()<15||(n.getHours()===15&&n.getMinutes()<30)){scheduleSmartUpdate();return}let today=ymd(n);$('autost').textContent='自动更新：正在检查收盘后新数据…';let x=await refreshLatest(true);if(x && x.date===today){$('autost').textContent='自动更新：'+today+' 已更新，今日停止检查';await loadHist();updateTimer=null;return}if(x && baselineDate && x.date!==baselineDate){$('autost').textContent='自动更新：发现新交易日 '+x.date+'，今日停止检查';baselineDate=x.date;await loadHist();updateTimer=null;return}if(n.getHours()>=23){$('autost').textContent='自动更新：今日未发现新数据，明日15:30后再检查';setTimeout(scheduleSmartUpdate, 60*60*1000);return}$('autost').textContent='自动更新：尚无新数据，10分钟后再查';updateTimer=setTimeout(startCloseChecks,600000)}
async function boot(){let m=$('msg');try{let x=await refreshLatest(false);if(!x) return;baselineDate=x.date;loadLan();loadHist();scheduleSmartUpdate();}catch(e){m.className='msg err';m.textContent='最新 PCR 读取失败：'+e.message}}
async function loadHist(){try{let r=await fetch('/api/history?days='+window.histDays);let j=await r.json();if(!r.ok)throw Error(j.error);let a=j.data;window.histData=a;window.viewStart=0;window.viewEnd=a.length;if(!a.length)throw Error('无历史数据');let x=a[a.length-1];$('ma').textContent=fmt(x.ma20);$('bb').textContent=fmt(x.lower)+' / '+fmt(x.upper);$('msg').textContent=`完成：${a.length} 个交易日。滚轮缩放，按住鼠标左键拖动平移，双击图表复位。`;window.renderAll();}catch(e){let m=$('msg');m.className='msg err';m.textContent='最新 PCR 已显示，但历史 BB 加载失败：'+e.message}}
boot();</script></body></html>'''

class H(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def sendj(self,o,status=200):
        b=json.dumps(o,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/':
            b=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        try:
            if p.path=='/api/latest':
                d,errs=latest(); self.sendj({'ok':True,'data':d,'skipped':errs}); return
            if p.path=='/api/daytest':
                ds=parse_qs(p.query).get('date',['20260924'])[0].replace('-',''); self.sendj({'ok':True,'data':one_day(ds)}); return
            if p.path=='/api/history':
                days=max(20,min(520,int(parse_qs(p.query).get('days',['25'])[0]))); d,errs=history(days); self.sendj({'ok':True,'data':d,'count':len(d),'errors':errs}); return
            if p.path=='/api/info':
                ip=local_ip(); self.sendj({'version':'V15-smart-close-lan','port':PORT,'lan_ip':ip,'lan_url':f'http://{ip}:{PORT}'}); return
            if p.path=='/api/health':
                ip=local_ip(); self.sendj({'version':'V15-smart-close-lan','port':PORT,'python':sys.version,'cache_dir':CACHE_DIR,'cache_files':len(os.listdir(CACHE_DIR)),'lan_url':f'http://{ip}:{PORT}'}); return
            self.sendj({'error':'Not found'},404)
        except Exception as e:self.sendj({'ok':False,'error':str(e)},500)

def main():
    ip=local_ip(); print('Gold PCR Radar V15'); print(f'PC: http://127.0.0.1:{PORT}'); print(f'Phone (same Wi-Fi): http://{ip}:{PORT}'); print('Smart update: after 15:30 on weekdays, check every 10 minutes until new daily data is found.')
    s=ThreadingHTTPServer((HOST,PORT),H); threading.Timer(1,lambda:webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    try:s.serve_forever()
    except KeyboardInterrupt:pass
if __name__=='__main__':main()

