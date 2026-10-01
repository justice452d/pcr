"""SHFE silver and copper / INE crude option open-interest radar."""
import json, os, re, threading, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from statistics import mean, pstdev

import shfe_gold as g

PRODUCTS={'silver':('白银','ag','白银期权'),
          'copper':('铜','cu','铜期权'),
          'oil':('原油','sc','原油期权')}
HISTORY_FILE=os.path.join(g.CACHE_DIR,'shfe_history.json')
# One-time migration from pre-v9 package-local archive, if present.
_legacy_history=os.path.join(g.BASE,'data','shfe_history.json')
if not os.path.exists(HISTORY_FILE) and os.path.exists(_legacy_history):
    try: shutil.copy2(_legacy_history,HISTORY_FILE)
    except Exception: pass
_archive_lock=threading.Lock()

def rows(kind, ds, product):
    # SHFE's published daily file includes both SHFE and INE (SC) products.
    return g.get_json('op' if kind=='option' else 'fu',ds)[0]['o_curinstrument']

def parse_option(ds,product):
    label,code,fullname=PRODUCTS[product]
    put=call=0; np=nc=0;bad=[];unreported=0
    for r in rows('option',ds,product):
        inst=str(r.get('INSTRUMENTID','')).replace(' ','').upper()
        pname=str(r.get('PRODUCTNAME','')).strip()
        if not (pname==fullname or re.match(rf'^{code.upper()}\d{{3,4}}-?[CP]',inst)):continue
        m=re.match(rf'^{code.upper()}\d{{3,4}}-?([CP])-?\d',inst)
        if not m:
            if re.match(rf'^{code.upper()}\d{{3,4}}',inst):bad.append(inst)
            continue  # excludes subtotal; reject unrecognized product contracts below
        oi=g.fnum(r.get('OPENINTEREST'))
        if oi is None:
            # The official feed can leave dormant strike OI blank, as in gold.
            # A missing OI on an actively traded contract is an actual error.
            if (g.fnum(r.get('VOLUME')) or 0)>0:bad.append(inst+': 成交合约持仓缺失')
            else:unreported+=1
            continue
        if oi<0:bad.append(inst+': 持仓为负');continue
        if m.group(1)=='P':put+=oi;np+=1
        else:call+=oi;nc+=1
    if bad:raise ValueError(f'{label}期权 {ds} 存在未计入合约: {bad[:8]}')
    if not put or not call or not np or not nc:
        raise ValueError(f'{label}期权 {ds} 未取得有效的 Put/Call 持仓；不生成 PCR')
    subtotals=[r for r in rows('option',ds,product) if
               str(r.get('PRODUCTID','')).lower()==code+'_o' and
               str(r.get('INSTRUMENTID','')).strip()=='小计']
    if len(subtotals)!=1 or g.fnum(subtotals[0].get('OPENINTEREST'))!=put+call:
        raise ValueError(f'{label}期权 {ds} 持仓汇总与官方小计不符')
    return {'date':datetime.strptime(ds,'%Y%m%d').date().isoformat(),
            'put_oi':int(put),'call_oi':int(call),'pcr':put/call,
            'put_contracts':np,'call_contracts':nc,'unreported_contracts':unreported,
            'source':'SHFE 日行情（原油为 INE 合约）' if product=='oil' else 'SHFE 日行情'}

def parse_future(ds,product):
    label,code,_=PRODUCTS[product]
    try:items=rows('future',ds,product)
    except Exception:return {}
    choices=[]
    for r in items:
        inst=str(r.get('INSTRUMENTID') or '').replace(' ','').upper()
        if not inst and str(r.get('PRODUCTID','')).lower()==code+'_f':
            month=str(r.get('DELIVERYMONTH') or '').strip()
            if re.fullmatch(r'\d{4}',month):inst=code.upper()+month
        if not re.fullmatch(rf'{code.upper()}\d{{3,4}}',inst):continue
        oi=g.fnum(r.get('OPENINTEREST'));price=g.fnum(r.get('CLOSEPRICE'))
        if oi is not None and price is not None:choices.append((oi,price,inst))
    if not choices:return {}
    oi,price,inst=max(choices)
    return {'gold_close':price,'gold_contract':inst,'underlying_close':price,
            'underlying_contract':inst,'underlying_oi':oi}

def one_day(ds,product):
    x=parse_option(ds,product)
    price=parse_future(ds,product)
    # Price is a separate download. Do not hide a valid option PCR when absent.
    x.update(price)
    return x

def latest(product):
    errors=[]
    for ds in g.weekday_dates(8):
        try:
            current=one_day(ds,product)
            sync_archive(ds)
            return current,errors
        except Exception as e:errors.append(f'{ds}: {e}')
    raise RuntimeError(f'{PRODUCTS[product][0]} 最近8个工作日未取得有效行情：'+ ' | '.join(errors[:3]))

def sync_archive(latest_ds):
    """Append newly published exchange dates so historical graphs keep updating."""
    if not os.path.exists(HISTORY_FILE):return
    with _archive_lock:
        with open(HISTORY_FILE,encoding='utf-8') as f: archive=json.load(f)
        last=archive['silver'][-1]['date'].replace('-','')
        if latest_ds<=last:return
        dates=[d for d in reversed(g.weekday_dates(14)) if last<d<=latest_ds]
        for ds in dates:
            fresh={}
            try:
                for k in PRODUCTS:fresh[k]=one_day(ds,k)
            except Exception:continue  # Holidays and unpublished days have no record.
            for k in PRODUCTS:
                if not any(x['date']==fresh[k]['date'] for x in archive[k]):archive[k].append(fresh[k])
        temp=HISTORY_FILE+'.tmp'
        with open(temp,'w',encoding='utf-8') as f:json.dump(archive,f,ensure_ascii=False,separators=(',',':'))
        os.replace(temp,HISTORY_FILE)

def history(product,days=35):
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE,encoding='utf-8') as f: archive=json.load(f)
        stored=archive.get(product,[])
        if stored:
            got=[dict(x) for x in stored]
            for i,x in enumerate(got):
                w=[y['pcr'] for y in got[max(0,i-19):i+1]]
                x.update(ma20=mean(w) if len(w)==20 else None,
                         upper=mean(w)+2*pstdev(w) if len(w)==20 else None,
                         lower=mean(w)-2*pstdev(w) if len(w)==20 else None)
            return got[-days:],[]
    # Fallback without archive: use 19 warm-up trading days so BB values do not
    # depend on whether the user selected 35/60/120/260 days.
    warmup=19; need=days+warmup
    dates=g.weekday_dates(need+20); got=[]; errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(one_day,ds,product):ds for ds in dates}
        for f in as_completed(futures):
            try:got.append(f.result())
            except Exception as e:errors.append((futures[f],str(e)))
    got.sort(key=lambda x:x['date']);got=got[-need:]
    for i,x in enumerate(got):
        w=[y['pcr'] for y in got[max(0,i-19):i+1]]
        x.update(ma20=mean(w) if len(w)==20 else None,
                 upper=mean(w)+2*pstdev(w) if len(w)==20 else None,
                 lower=mean(w)-2*pstdev(w) if len(w)==20 else None)
    return got[-days:],errors[-6:]

def page(product):
    name=PRODUCTS[product][0]
    html=g.HTML.replace('黄金 PCR 雷达 V16',name+' PCR 雷达').replace('黄金期权',name+'期权')
    # 修正顶部市场导航高亮：非黄金页面不能继续高亮沪金。
    html=html.replace("<a class='active' href='/shfe'>沪金 AU</a>", "<a href='/shfe'>沪金 AU</a>")
    nav_target={
        'silver': ("<a href='/shfe/silver'>沪银 AG</a>", "<a class='active' href='/shfe/silver'>沪银 AG</a>"),
        'copper': ("<a href='/shfe/copper'>沪铜 CU</a>", "<a class='active' href='/shfe/copper'>沪铜 CU</a>"),
        'oil': ("<a href='/shfe/oil'>原油 SC</a>", "<a class='active' href='/shfe/oil'>原油 SC</a>"),
    }
    if product in nav_target:
        old,new=nav_target[product]
        html=html.replace(old,new)
    html=html.replace('SHFE AU 黄金','SHFE AG 白银' if product=='silver' else 'SHFE CU 铜' if product=='copper' else 'INE SC 原油')
    html=html.replace('沪金代表合约收盘价',name+'代表合约收盘价').replace('数据日期 / 沪金','数据日期 / '+name)
    html=html.replace('15:30后每10分钟检查，更新成功后当天停止','历史数据可回溯查询')
    html=html.replace("<option value='520'>520日</option>","<option value='520'>全部可用历史</option>")
    html=html.replace('收盘后智能更新 + 局域网版','官方日行情 · 历史滚动轨道')
    html=html.replace('V15：日频智能更新。每个工作日 15:30 后每10分钟检查一次；发现当天新数据后停止当天检查。手机和同一局域网内其他电脑都可访问。端口：8065。',
                      '日频数据源：'+('上海国际能源交易中心' if product=='oil' else '上期所')+'；按全部上市期权合约汇总持仓。历史图满20个交易日才显示轨道。期货收盘价单独读取，缺价日不能用于价格回测。')
    html=html.replace('完成：${a.length} 个交易日。',
                      '完成：${a.length} 个交易日。期货收盘价缺失 ${a.filter(v=>v.gold_close==null).length} 日。')
    html=html.replace('let x=await refreshLatest(false);if(!x) return;baselineDate=x.date;loadLan();loadHist();scheduleSmartUpdate();',
                      'loadLan();await loadHist();let x=await refreshLatest(true);if(x)baselineDate=x.date;scheduleSmartUpdate();')
    html=html.replace("let x=a[a.length-1];$('ma').textContent=fmt(x.ma20);",
                      "let x=a[a.length-1];if(!$('date').dataset.date){$('pcr').textContent=fmt(x.pcr);$('po').textContent=Math.round(x.put_oi).toLocaleString();$('co').textContent=Math.round(x.call_oi).toLocaleString();$('date').textContent=x.date+(x.gold_contract?` / ${x.gold_contract} ${fmt(x.gold_close,2)}`:'');$('date').dataset.date=x.date;}$('ma').textContent=fmt(x.ma20);")
    html=html.replace('历史数据可回溯查询','官方历史日行情已回填 · 可选择范围回测')
    html=html.replace("<div id='msg' class='msg'>", "<div class='msg'>持仓量为当日结算后数据，回测信号从下一交易日使用。价格为当日持仓最大的期货合约收盘价，换月时可能跳变。</div><div id='msg' class='msg'>")
    return html

