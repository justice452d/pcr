import csv,datetime,json,bisect,statistics,math
from pathlib import Path
p=Path(__file__).parent
r=list(csv.DictReader((p/'csi_all_share_daily.csv').open()))
by_month={}
for a in r:
 d=a['date']; by_month[d[:6]]=a
m=list(by_month.values())
for i,a in enumerate(m):
 a['date']=a['date'][:4]+'-'+a['date'][4:6]+'-'+a['date'][6:]
 a['close']=float(a['close']);a['pe_ttm']=float(a['pe_ttm'])
for i,a in enumerate(m):
 hist=[x['pe_ttm'] for x in m[max(0,i-119):i+1]]
 a['pe_percentile_10y']=round(100*(sum(x<a['pe_ttm'] for x in hist)+.5*sum(x==a['pe_ttm'] for x in hist))/len(hist),2) if len(hist)>=36 else None
 a['history_months']=len(hist)
 for n in (6,12):a[f'forward_{n}m_pct']=round(100*(m[i+n]['close']/a['close']-1),2) if i+n<len(m) else None
with (p/'csi_monthly_study.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=['date','close','pe_ttm','history_months','pe_percentile_10y','forward_6m_pct','forward_12m_pct'],extrasaction='ignore');w.writeheader();w.writerows(m)
events=['2015-06','2018-12','2021-02','2022-10','2024-02','2024-09','2026-09']
for month in events:
 a=next((x for x in m if x['date'].startswith(month)),None)
 print(month, {k:a.get(k) for k in ('close','pe_ttm','pe_percentile_10y','forward_6m_pct','forward_12m_pct')} if a else 'N/A')
for lo,hi in [(0,20),(20,40),(40,60),(60,80),(80,100.01)]:
 vals=[x['forward_12m_pct'] for x in m if x['pe_percentile_10y'] is not None and lo<=x['pe_percentile_10y']<hi and x['forward_12m_pct'] is not None]
 print('P/E percentile',lo,hi,'12m count',len(vals),'median',round(statistics.median(vals),2) if vals else None)
(p/'study_data.json').write_text(json.dumps(m,ensure_ascii=False,separators=(',',':')))

