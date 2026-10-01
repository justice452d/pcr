import csv,json,urllib.request,time,datetime
from pathlib import Path
URL='https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=000985&startDate={}&endDate={}'
rows={}
for year in range(2010,2027):
    start=f'{year}0101'; end=f'{year}1231' if year<2026 else '20260926'
    for attempt in range(3):
        try:
            req=urllib.request.Request(URL.format(start,end),headers={'User-Agent':'Mozilla/5.0'})
            data=json.load(urllib.request.urlopen(req,timeout=25))['data']
            for r in data:
                d=r['tradeDate']; vol=r.get('tradingVol') or 0
                if d>='20110802' and d[:4]==str(year) and vol>0 and r.get('close') and r.get('peg') and r['peg']>0:
                    rows[d]=(d,float(r['close']),float(r['peg']),float(vol))
            print(year,len(data),'valid cumulative',len(rows),flush=True)
            break
        except Exception as e:
            print(year,'retry',attempt,repr(e),flush=True)
            if attempt==2: raise
            time.sleep(2)
with open('a_share_valuation/csi_all_share_daily.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['date','close','pe_ttm','trading_volume']);w.writerows(rows[d] for d in sorted(rows))

