"""CFFEX CSI 300 (IO) option open-interest PCR - v6.

Design rules:
1) Historical series is built from official MONTHLY ZIP archives, not hundreds of daily probes.
2) A displayed range never changes BB values. We always fetch enough prior rows for the 20-day window,
   compute BB on the complete ordered trading-day series, then slice only for display.
3) Missing completed months are fatal for that request: we never silently skip a month, because silent
   holes would corrupt rolling standard deviation and make 60d/120d disagree.
4) Current month is refreshed; latest daily CSV is only a small fallback for the newest few weekdays.
"""
import csv, io, json, os, re, ssl, subprocess, urllib.request, zipfile, threading, time
from datetime import datetime, timedelta
from statistics import mean, pstdev

UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36'
REFERER='https://www.cffex.com.cn/rtj/'
BASE=os.path.dirname(os.path.abspath(__file__))
PCR_HOME=os.environ.get('PCR_HOME', r'H:\\PCR' if os.name=='nt' else BASE)
CACHE_DIR=os.path.join(PCR_HOME,'cache','cffex'); os.makedirs(CACHE_DIR,exist_ok=True)
STATE_PATH=os.path.join(CACHE_DIR,'io_state.json')
LOCK=threading.Lock()
MONTH_MEM={}
LATEST_ROW=None


def fnum(x):
    try:
        if x is None:return None
        s=str(x).replace(',','').strip()
        if s in ('','-','--','None','nan','NaN'):return None
        return float(s)
    except:return None


def _fetch(url, timeout=8):
    """Short bounded fetch. Never let one dead URL freeze the UI for minutes."""
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Referer':REFERER,'Accept':'*/*','Connection':'close'})
    try:
        with urllib.request.urlopen(req,timeout=timeout,context=ssl.create_default_context()) as r:
            return r.read(),'urllib'
    except Exception as e1:
        if os.name=='nt':
            try:
                p=subprocess.run(['curl.exe','-L','--silent','--show-error','--connect-timeout','4','--max-time',str(timeout),'-A',UA,'-e',REFERER,url],capture_output=True,timeout=timeout+3)
                if p.returncode==0 and p.stdout:return p.stdout,'curl.exe'
                e2=(p.stderr or b'').decode('utf-8','ignore')
            except Exception as e:e2=str(e)
            raise RuntimeError(f'urllib={e1}; curl={e2}')
        raise


def _decode(raw):
    for enc in ('gb18030','gbk','utf-8-sig','utf-8'):
        try:
            text=raw.decode(enc)
            if '合约' in text or '今开盘' in text:return text
        except:pass
    return raw.decode('utf-8','ignore')


def _zip_urls(ym):
    path=f'/sj/historysj/{ym}/zip/{ym}.zip'
    return [f'https://www.cffex.com.cn{path}',f'http://www.cffex.com.cn{path}']


def _daily_urls(ds):
    ym,dd=ds[:6],ds[6:]
    paths=[f'/sj/hqsj/rtj/{ym}/{dd}/{ds}_1.csv',f'/fzjy/mrhq/{ym}/{dd}/{ds}_1.csv']
    return [f'https://www.cffex.com.cn{p}' for p in paths]+[f'http://www.cffex.com.cn{p}' for p in paths]


def _get_month_zip(ym, refresh_current=False):
    current=datetime.now().strftime('%Y%m')
    current_month=(ym==current)
    path=os.path.join(CACHE_DIR,f'{ym}.zip')
    # Completed months are immutable and must use cache once present.
    if not current_month and os.path.exists(path):
        raw=open(path,'rb').read()
        if raw.startswith(b'PK'):return raw,'disk-cache'
    # Current month: use a recent file for 5 min unless explicitly refreshed.
    if current_month and os.path.exists(path) and not refresh_current:
        age=time.time()-os.path.getmtime(path)
        raw=open(path,'rb').read()
        if age<300 and raw.startswith(b'PK'):return raw,'disk-cache-current'
    last=[]
    for url in _zip_urls(ym):
        try:
            raw,tr=_fetch(url,10)
            if not raw.startswith(b'PK'):raise RuntimeError('not zip')
            try:open(path,'wb').write(raw)
            except:pass
            return raw,tr
        except Exception as e:last.append(f'{url}: {e}')
    raise RuntimeError(f'{ym} 月度ZIP读取失败: '+ ' | '.join(last[-2:]))


def _rows_from_raw(raw):
    text=_decode(raw); reader=csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:raise RuntimeError('CSV没有表头')
    out=[]
    for rr in reader:
        out.append({(str(k).replace('\ufeff','').strip() if k is not None else ''):(v.strip() if isinstance(v,str) else v) for k,v in rr.items()})
    return out


def _pick(r,*keys):
    for k in keys:
        if k in r and str(r[k]).strip()!='':return r[k]
    return None


def _side(symbol):
    u=symbol.upper().replace(' ','')
    for pat in (r'^IO\d{4}[-_]?([CP])[-_]?',r'[-_]([CP])[-_]',r'^IO\d{4}([CP])\d'):
        m=re.search(pat,u)
        if m:return m.group(1)
    return None


def _one_day_from_rows(ds,rows,tr):
    put=call=0.0; np=nc=0; fut=[]; io_rows=0
    for r in rows:
        symbol=str(_pick(r,'合约代码','合约','instrumentId') or '').replace(' ','').strip()
        if not symbol or any(x in symbol for x in ('小计','合计','总计')):continue
        u=symbol.upper(); oi=fnum(_pick(r,'持仓量','空盘量'))
        if u.startswith('IO'):
            side=_side(u)
            if side not in ('C','P') or oi is None:continue
            io_rows+=1
            if side=='P':put+=oi;np+=1
            else:call+=oi;nc+=1
        elif re.fullmatch(r'IF\d{4}',u):
            close=fnum(_pick(r,'今收盘','收盘价','收盘'))
            if oi is not None and close is not None:fut.append((oi,close,symbol))
    if put<=0 or call<=0:raise RuntimeError(f'{ds} IO持仓汇总失败(IO行={io_rows})')
    out={'date':datetime.strptime(ds,'%Y%m%d').strftime('%Y-%m-%d'),'put_oi':put,'call_oi':call,'pcr':put/call,
         'put_contracts':np,'call_contracts':nc,'transport':tr,'source':'CFFEX 日行情'}
    if fut:
        oi,close,code=max(fut,key=lambda x:x[0]);out.update(index_close=close,index_contract=code,index_oi=oi)
    return out


def _parse_month(ym, refresh_current=False):
    rawzip,tr=_get_month_zip(ym,refresh_current=refresh_current)
    rows=[]
    with zipfile.ZipFile(io.BytesIO(rawzip)) as zf:
        for name in zf.namelist():
            m=re.search(r'(20\d{6})_1\.csv$',name)
            if not m:continue
            ds=m.group(1)
            try:rows.append(_one_day_from_rows(ds,_rows_from_raw(zf.read(name)),f'{tr}/monthly-zip'))
            except Exception:pass
    rows.sort(key=lambda x:x['date'])
    return rows


def _daily_one(ds):
    last=[]
    path=os.path.join(CACHE_DIR,f'{ds}_1.csv')
    if os.path.exists(path):
        try:return _one_day_from_rows(ds,_rows_from_raw(open(path,'rb').read()),'disk-cache-daily')
        except:pass
    for url in _daily_urls(ds):
        try:
            raw,tr=_fetch(url,6)
            text=_decode(raw)
            if '合约' not in text or '持仓' not in text:raise RuntimeError('not daily csv')
            try:open(path,'wb').write(raw)
            except:pass
            return _one_day_from_rows(ds,_rows_from_raw(raw),tr)
        except Exception as e:last.append(str(e))
    raise RuntimeError('daily failed: '+' | '.join(last[-2:]))


def _previous_weekdays(limit=6):
    d=datetime.now(); out=[]
    while len(out)<limit:
        if d.weekday()<5:out.append(d.strftime('%Y%m%d'))
        d-=timedelta(days=1)
    return out


def latest():
    """Fast latest: fresh current-month ZIP first, then only a few recent daily files."""
    global LATEST_ROW
    errors=[]; current=datetime.now().strftime('%Y%m')
    try:
        rows=_parse_month(current,refresh_current=True)
        if rows:
            LATEST_ROW=rows[-1]
            # If ZIP's last trading day is older, probe newer weekdays only.
            last_dt=datetime.strptime(rows[-1]['date'],'%Y-%m-%d')
            probes=[]; d=datetime.now()
            while d.date()>last_dt.date():
                if d.weekday()<5:probes.append(d.strftime('%Y%m%d'))
                d-=timedelta(days=1)
            for ds in sorted(probes,reverse=True):
                try:
                    r=_daily_one(ds)
                    if r['date']>LATEST_ROW['date']:LATEST_ROW=r
                    break
                except Exception as e:errors.append(f'{ds}: {e}')
            return LATEST_ROW,errors
    except Exception as e:errors.append(f'{current} ZIP: {e}')
    for ds in _previous_weekdays(6):
        try:
            LATEST_ROW=_daily_one(ds);return LATEST_ROW,errors
        except Exception as e:errors.append(f'{ds}: {e}')
    raise RuntimeError('最新IO数据读取失败 | '+' | '.join(errors[-4:]))


def _month_back(ym):
    y,m=int(ym[:4]),int(ym[4:]); m-=1
    if m==0:y-=1;m=12
    return f'{y:04d}{m:02d}'


def _build_for_days(days):
    """Fetch whole months backwards until there are days+19 valid rows.

    No month is silently skipped. That invariant is what keeps rolling BB identical
    in every display range.
    """
    need=days+19
    current=datetime.now().strftime('%Y%m'); ym=current; allrows={}; errors=[]; months=0
    while len(allrows)<need and months<36:
        try:
            rows=_parse_month(ym,refresh_current=(ym==current))
        except Exception as e:
            raise RuntimeError(f'历史序列不完整：{ym} 月读取失败。为避免BB失真，本版不会跳过缺失月份。错误：{e}')
        for r in rows:allrows[r['date']]=r
        ym=_month_back(ym);months+=1
    if LATEST_ROW:allrows[LATEST_ROW['date']]=LATEST_ROW
    got=sorted(allrows.values(),key=lambda x:x['date'])
    if len(got)<min(20,days):raise RuntimeError(f'有效历史仅 {len(got)} 日')
    vals=[x['pcr'] for x in got]
    for i,x in enumerate(got):
        if i>=19:
            w=vals[i-19:i+1];m=mean(w);sd=pstdev(w);x['ma20']=m;x['upper']=m+2*sd;x['lower']=m-2*sd
        else:x['ma20']=x['upper']=x['lower']=None
    return got[-days:],errors


def history(days=260):
    days=max(20,min(520,int(days)))
    with LOCK:
        return _build_for_days(days)

HTML=r'''<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>沪深300 IO PCR 雷达</title><style>
body{margin:0;background:#0b1118;color:#e8eef7;font-family:Arial,'Microsoft YaHei',sans-serif}.wrap{padding:20px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.title{font-size:31px;font-weight:800}.tag{font-size:13px;color:#62b5ff}.sub{color:#93a4bc;margin:8px 0 14px}.msg{background:#10243b;border:1px solid #183654;color:#afd2ff;padding:11px 14px;border-radius:12px;margin:8px 0 14px;white-space:pre-wrap}.msg.err{background:#35191b;border-color:#643033;color:#ffd3d6}.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.card,.panel{background:#111b27;border:1px solid #223247;border-radius:16px;padding:16px}.lab{color:#91a5be;font-size:14px}.val{font-size:28px;font-weight:750;margin-top:8px}.charts{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;margin-top:14px}.panel{min-height:455px}.panel h3{margin:0 0 6px;color:#9db2cc;font-size:16px}.legend{display:flex;gap:14px;flex-wrap:wrap;color:#8fa5bf;font-size:12px;margin:0 0 4px}.btn{background:#122338;color:#eaf2ff;border:1px solid #2c4058;border-radius:12px;padding:10px 16px;cursor:pointer}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 0}.sel{background:#0e1a28;color:#dce8f7;border:1px solid #2c4058;border-radius:9px;padding:8px 10px}.mini{padding:7px 10px;font-size:12px}svg{width:100%;height:390px;overflow:visible}.footer{margin-top:12px;color:#8294aa;font-size:13px;line-height:1.65}.anchors{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.anchor{background:#0e1926;border:1px solid #25374d;border-radius:12px;padding:12px}.anchor b{display:block;font-size:18px;margin:5px 0}.ok{color:#83dda8}.bad{color:#ff9a91}.tip{position:fixed;pointer-events:none;display:none;background:#08121f;border:1px solid #38516e;border-radius:9px;padding:8px 10px;color:#eaf2ff;font-size:12px;box-shadow:0 8px 22px #0008;z-index:20;white-space:pre-line}.marketnav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}.marketnav a{display:inline-block;text-decoration:none;background:#122338;color:#cfe4ff;border:1px solid #2c4058;border-radius:10px;padding:8px 12px;font-size:13px}.marketnav a.active{background:#215385;border-color:#4d96ce;color:#fff}.marketnav a:hover{background:#18304c}@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}.charts{grid-template-columns:1fr}}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.anchors{grid-template-columns:1fr}.wrap{padding:10px}.title{font-size:25px}}
</style></head><body><div class='wrap'><div class='marketnav'><a class='active' href='/cffex/io'>沪深300 IO</a><a href='/shfe'>沪金 AU</a><a href='/shfe/silver'>沪银 AG</a><a href='/shfe/copper'>沪铜 CU</a><a href='/shfe/oil'>原油 SC</a><a href='/hk'>恒指 / 恒科</a></div><div class='top'><div><div class='title'>沪深300 IO PCR 雷达 <span class='tag'>v9 · 公共缓存 H:\\PCR\\cache · 稳定历史序列</span></div><div class='sub'>CFFEX IO 全部合约持仓量 PCR = ΣPut OI / ΣCall OI · BB(20,2) · 浏览器不直接访问中金所，不受 CORS 限制</div></div><button class='btn' onclick='refreshLatest(false).then(()=>loadHist())'>立即刷新</button></div><div id='msg' class='msg'>正在读取最近一个可用交易日…</div><div class='cards'>
<div class='card'><div class='lab'>最新 PCR</div><div class='val' id='pcr'>—</div></div><div class='card'><div class='lab'>Put OI</div><div class='val' id='po'>—</div></div><div class='card'><div class='lab'>Call OI</div><div class='val' id='co'>—</div></div><div class='card'><div class='lab'>20日均线</div><div class='val' id='ma'>—</div></div><div class='card'><div class='lab'>布林下轨 / 上轨</div><div class='val' id='bb'>—</div></div><div class='card'><div class='lab'>数据日期 / IF</div><div class='val' id='date' style='font-size:19px'>—</div></div></div>
<div class='toolbar'><span class='lab'>历史范围</span><select id='range' class='sel' onchange='changeRange()'><option value='35'>35日</option><option value='60'>60日</option><option value='120'>120日</option><option value='260' selected>260日</option><option value='520'>520日</option></select><button class='btn mini' onclick='resetZoom()'>复位缩放</button><span class='lab'>滚轮缩放 · 左键拖动 · 双击复位</span><span class='lab' id='autost'>自动刷新：等待首次加载</span></div>
<div class='charts'><div class='panel'><h3>PCR 与 BB(20,2)</h3><div class='legend'>蓝=PCR　橙=20日均线　灰=上下2σ</div><svg id='pcrChart'></svg></div><div class='panel'><h3>IF 代表合约收盘价</h3><div class='legend'>每日取 IF 持仓量最大的合约；换月处可能跳变</div><svg id='idxChart'></svg></div></div>
<div class='anchors'><div class='anchor'>截图 2026-03-27「开」<b>0.6469</b><span id='a1'>等待历史数据</span></div><div class='anchor'>截图 2026-04-17「开」<b>0.8095</b><span id='a2'>等待历史数据</span></div><div class='anchor'>截图 2026-08-18「开」<b>0.7948</b><span id='a3'>等待历史数据</span></div></div>
<div class='footer'>抓取路径：中金所官方单日日行情 CSV；单日不可用时自动回退官方月度历史 ZIP。当天收盘持仓最早在收盘后可知；截图中的指标“开盘值”可能对应前一交易日最终 PCR，所以锚点同时比较当日与前一交易日。<br><a href='/shfe' style='color:#78b9ef'>沪金 PCR</a>　·　<a href='/' style='color:#78b9ef'>大宗 PCR 首页</a>　·　<span id='lanurl'></span></div></div><div id='tip' class='tip'></div><script>
const APP_VERSION='v7'; const $=x=>document.getElementById(x),fmt=(x,n=4)=>x==null?'—':Number(x).toFixed(n);window.histDays=260;window.histData=[];window.viewStart=0;window.viewEnd=0;
function visibleData(){let a=window.histData||[];if(window.viewEnd<=window.viewStart||window.viewEnd>a.length){window.viewStart=0;window.viewEnd=a.length}return a.slice(window.viewStart,window.viewEnd)}
function renderAll(){let a=visibleData();draw($('pcrChart'),a,['pcr','ma20','upper','lower']);draw($('idxChart'),a,['index_close'],{price:true});attachZoom($('pcrChart'));attachZoom($('idxChart'))}
function resetZoom(){window.viewStart=0;window.viewEnd=(window.histData||[]).length;renderAll()}
async function changeRange(){window.histDays=parseInt($('range').value);$('msg').textContent=`正在加载 ${window.histDays} 个交易日历史…按月读取官方历史并缓存；同一日期的BB不会因显示范围改变。`;await loadHist()}
function attachZoom(svg){if(svg.dataset.zoomBound)return;svg.dataset.zoomBound='1';svg.addEventListener('wheel',e=>{e.preventDefault();let a=window.histData||[];if(a.length<10)return;let n=window.viewEnd-window.viewStart;if(n<=0)n=a.length;let rect=svg.getBoundingClientRect(),frac=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),anchor=window.viewStart+frac*n,nn=Math.round(n*(e.deltaY>0?1.22:.82));nn=Math.max(20,Math.min(a.length,nn));let ns=Math.round(anchor-frac*nn);ns=Math.max(0,Math.min(a.length-nn,ns));window.viewStart=ns;window.viewEnd=ns+nn;renderAll()},{passive:false});svg.addEventListener('mousedown',e=>window.dragState={x:e.clientX,s:window.viewStart,e:window.viewEnd});window.addEventListener('mouseup',()=>window.dragState=null);svg.addEventListener('mousemove',e=>{if(!window.dragState)return;let a=window.histData||[],n=window.dragState.e-window.dragState.s,rect=svg.getBoundingClientRect(),dx=e.clientX-window.dragState.x,shift=Math.round(-dx/Math.max(1,rect.width)*n),ns=Math.max(0,Math.min(a.length-n,window.dragState.s+shift));window.viewStart=ns;window.viewEnd=ns+n;renderAll()});svg.addEventListener('dblclick',e=>{e.preventDefault();resetZoom()})}
function clientToSvg(svg,e){let pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;let ctm=svg.getScreenCTM();return ctm?pt.matrixTransform(ctm.inverse()):{x:0,y:0}}
function draw(svg,data,keys,opts={}){const W=920,H=390,L=64,R=18,T=22,B=46;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.innerHTML='';let vals=[];keys.forEach(k=>data.forEach(d=>{if(d[k]!=null&&isFinite(+d[k]))vals.push(+d[k])}));if(!vals.length){svg.innerHTML='<text x="64" y="60" fill="#7890aa">历史数据仍在加载</text>';return}let mn=Math.min(...vals),mx=Math.max(...vals),pad=(mx-mn)*.08;if(!pad)pad=Math.max(Math.abs(mx)*.03,.01);mn-=pad;mx+=pad;const X=i=>L+(W-L-R)*i/Math.max(1,data.length-1),Y=v=>H-B-(H-T-B)*(v-mn)/(mx-mn);for(let g=0;g<5;g++){let y=T+(H-T-B)*g/4,val=mx-(mx-mn)*g/4;svg.innerHTML+=`<line x1="${L}" y1="${y}" x2="${W-R}" y2="${y}" stroke="#203044"/><text x="${L-8}" y="${y+4}" text-anchor="end" fill="#7890aa" font-size="11">${opts.price?val.toFixed(2):val.toFixed(3)}</text>`}let ticks=Math.min(6,data.length);for(let t=0;t<ticks;t++){let i=Math.round((data.length-1)*t/Math.max(1,ticks-1)),x=X(i),lab=(data[i].date||'').slice(5);svg.innerHTML+=`<text x="${x}" y="${H-18}" text-anchor="middle" fill="#7890aa" font-size="11">${lab}</text>`}const cols=['#62b5ff','#f0a24a','#8a9bb0','#8a9bb0'];keys.forEach((k,ki)=>{let seg=[],parts=[];data.forEach((d,i)=>{if(d[k]!=null)seg.push(`${X(i)},${Y(+d[k])}`);else if(seg.length){parts.push(seg);seg=[]}});if(seg.length)parts.push(seg);parts.forEach(pts=>{if(pts.length>1)svg.innerHTML+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${cols[ki]}" stroke-width="${ki===0?2.5:1.55}"/>`})});
// Real crosshair. Client coordinates are transformed through the SVG CTM, so
// the date under the mouse stays exact even when the SVG is stretched responsively.
const ns='http://www.w3.org/2000/svg';
const cross=document.createElementNS(ns,'g');cross.style.display='none';cross.style.pointerEvents='none';
const vl=document.createElementNS(ns,'line');vl.setAttribute('y1',T);vl.setAttribute('y2',H-B);vl.setAttribute('stroke','#d8e5f5');vl.setAttribute('stroke-width','1');vl.setAttribute('stroke-dasharray','4 4');vl.setAttribute('opacity','.75');
const hl=document.createElementNS(ns,'line');hl.setAttribute('x1',L);hl.setAttribute('x2',W-R);hl.setAttribute('stroke','#d8e5f5');hl.setAttribute('stroke-width','1');hl.setAttribute('stroke-dasharray','4 4');hl.setAttribute('opacity','.45');
const dot=document.createElementNS(ns,'circle');dot.setAttribute('r','4');dot.setAttribute('fill','#eaf2ff');dot.setAttribute('stroke','#62b5ff');dot.setAttribute('stroke-width','2');
cross.append(vl,hl,dot);svg.appendChild(cross);
const hit=document.createElementNS(ns,'rect');hit.setAttribute('x',L);hit.setAttribute('y',T);hit.setAttribute('width',W-L-R);hit.setAttribute('height',H-T-B);hit.setAttribute('fill','transparent');hit.style.cursor='crosshair';svg.appendChild(hit);
const labels={pcr:'PCR',ma20:'MA20',upper:'上轨',lower:'下轨',index_close:'IF收盘'};
hit.addEventListener('pointermove',e=>{let p=clientToSvg(svg,e),frac=(p.x-L)/(W-L-R),i=Math.max(0,Math.min(data.length-1,Math.round(frac*(data.length-1)))),d=data[i],x=X(i),primary=d[keys[0]],y=(primary!=null?Y(+primary):Math.max(T,Math.min(H-B,p.y)));vl.setAttribute('x1',x);vl.setAttribute('x2',x);hl.setAttribute('y1',y);hl.setAttribute('y2',y);dot.setAttribute('cx',x);dot.setAttribute('cy',y);cross.style.display='';let lines=[d.date||''];keys.forEach(k=>{if(d[k]!=null)lines.push(`${labels[k]||k}: ${opts.price?Number(d[k]).toFixed(2):Number(d[k]).toFixed(4)}`)});let tip=$('tip');tip.textContent=lines.join('\n');tip.style.display='block';let tw=190,th=110,left=e.clientX+14,top=e.clientY+12;if(left+tw>innerWidth)left=e.clientX-tw-14;if(top+th>innerHeight)top=e.clientY-th-14;tip.style.left=Math.max(8,left)+'px';tip.style.top=Math.max(8,top)+'px'});
hit.addEventListener('pointerleave',()=>{cross.style.display='none';$('tip').style.display='none'});
}
function checkAnchor(date,target,id){let a=window.histData||[],i=a.findIndex(x=>x.date===date),el=$(id);if(i<0){el.textContent='历史范围内没有该日期';return}let cur=a[i].pcr,prev=i>0?a[i-1].pcr:null,dc=Math.abs(cur-target),dp=prev==null?999:Math.abs(prev-target),best=dc<=dp?`当日 ${fmt(cur)}`:`前一交易日 ${fmt(prev)}`;el.innerHTML=`当日 ${fmt(cur)} · 前日 ${fmt(prev)}<br><span class="${Math.min(dc,dp)<.01?'ok':'bad'}">最接近：${best}，误差 ${fmt(Math.min(dc,dp),4)}</span>`}
async function refreshLatest(silent=false){let m=$('msg');try{let r=await fetch('/cffex/io/api/latest?ts='+Date.now(),{cache:'no-store'}),j=await r.json();if(!r.ok)throw Error(j.error);let x=j.data,old=$('date').dataset.date||'';$('pcr').textContent=fmt(x.pcr);$('po').textContent=Math.round(x.put_oi).toLocaleString();$('co').textContent=Math.round(x.call_oi).toLocaleString();$('date').textContent=x.date+(x.index_contract?` / ${x.index_contract} ${fmt(x.index_close,2)}`:'');$('date').dataset.date=x.date;$('autost').textContent='自动刷新：'+new Date().toLocaleTimeString()+' 已检查';if(!silent)m.textContent=`最新数据已加载：${x.date}。后台正在加载历史和布林带…`;if(old&&old!==x.date)await loadHist();return x}catch(e){$('autost').textContent='自动刷新失败：'+e.message;if(!silent){m.className='msg err';m.textContent='最新 IO PCR 读取失败：'+e.message}return null}}
async function loadLan(){try{let r=await fetch('/cffex/io/api/info?ts='+Date.now(),{cache:'no-store'}),j=await r.json();if(j.lan_url)$('lanurl').textContent='手机（同Wi‑Fi）：'+j.lan_url}catch(e){}}
let updateTimer=null,baselineDate='';function ymd(d=new Date()){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}function nextCheckDelay(){let n=new Date(),t=new Date(n);t.setHours(15,30,0,0);if(n.getDay()===0)t.setDate(n.getDate()+1);else if(n.getDay()===6)t.setDate(n.getDate()+2);else if(n>=t)return 0;return Math.max(1000,t-n)}function scheduleSmartUpdate(){if(updateTimer)clearTimeout(updateTimer);let d=nextCheckDelay();if(d>0){$('autost').textContent='自动更新：下次检查 '+new Date(Date.now()+d).toLocaleString();updateTimer=setTimeout(startCloseChecks,d);return}startCloseChecks()}async function startCloseChecks(){let n=new Date();if(n.getDay()===0||n.getDay()===6||n.getHours()<15||(n.getHours()===15&&n.getMinutes()<30)){scheduleSmartUpdate();return}let today=ymd(n),x=await refreshLatest(true);if(x&&(x.date===today||(baselineDate&&x.date!==baselineDate))){baselineDate=x.date;$('autost').textContent='自动更新：发现新交易日 '+x.date+'，今日停止检查';await loadHist();updateTimer=null;return}if(n.getHours()>=23){$('autost').textContent='自动更新：今日未发现新数据，明日15:30后再检查';setTimeout(scheduleSmartUpdate,3600000);return}$('autost').textContent='自动更新：尚无新数据，10分钟后再查';updateTimer=setTimeout(startCloseChecks,600000)}
async function loadHist(){try{let r=await fetch('/cffex/io/api/history?days='+window.histDays+'&ts='+Date.now(),{cache:'no-store'}),j=await r.json();if(!r.ok)throw Error(j.error);let a=j.data||[];if(!a.length)throw Error('无历史数据');window.histData=a;window.viewStart=0;window.viewEnd=a.length;let x=a[a.length-1];$('ma').textContent=fmt(x.ma20);$('bb').textContent=fmt(x.lower)+' / '+fmt(x.upper);$('msg').className='msg';$('msg').textContent=`完成：${a.length} 个交易日。当前程序 ${APP_VERSION}；数据由本机 Python 服务端直接抓中金所。`;renderAll();checkAnchor('2026-03-27',.6469,'a1');checkAnchor('2026-04-17',.8095,'a2');checkAnchor('2026-08-18',.7948,'a3')}catch(e){$('msg').className='msg err';$('msg').textContent='历史加载失败：'+e.message}}
async function boot(){loadLan();loadHist();refreshLatest(false).then(x=>{if(x)baselineDate=x.date});scheduleSmartUpdate()}boot();
</script></body></html>'''

