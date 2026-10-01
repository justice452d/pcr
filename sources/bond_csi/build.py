import csv,math,json,statistics
from pathlib import Path
root=Path(__file__).parent
b={}
with (root/'bond_10y_daily.csv').open(newline='') as f:
 for row in csv.DictReader(f):
  b[row['date']]=float(row['yield10_pct'])
out=[]
with (root/'csi_all_share_daily.csv').open(newline='') as f:
 for row in csv.DictReader(f):
  raw=row['date'];d=raw[:4]+'-'+raw[4:6]+'-'+raw[6:]
  if d not in b:continue
  pe=float(row['pe_ttm']);y=b[d]
  if not (5<pe<100 and 0<y<10):continue
  erp=100/pe-y
  out.append(dict(date=d,index=float(row['close']),pe=pe,yield10=y,earnings_yield=100/pe,erp=erp))
# 3 calendar years of available trading sessions, 756 rows. All bands use current/past observations only.
for i,a in enumerate(out):
 xs=[x['erp'] for x in out[max(0,i-755):i+1]]
 if len(xs)>=500:
  mu=statistics.fmean(xs);sd=statistics.pstdev(xs)
  a.update(mean=mu,upper=mu+2*sd,lower=mu-2*sd,z=(a['erp']-mu)/sd if sd else 0)
 else:a.update(mean=None,upper=None,lower=None,z=None)
with (root/'daily_data.csv').open('w',newline='') as f:
 keys=list(out[0]);w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(out)
(root/'data.json').write_text(json.dumps(out,ensure_ascii=False,separators=(',',':')))
print('obs',len(out),'range',out[0]['date'],out[-1]['date'],'bands',sum(x['lower'] is not None for x in out))
for m in ('2020-10','2021-02','2022-04','2022-10','2024-02','2024-08','2024-09','2025-05'):
 x=next((a for a in reversed(out) if a['date'].startswith(m)),None)
 if x:print(m,{k:round(x[k],2) for k in ('index','pe','yield10','erp','lower','mean','upper','z')})

# Rebuild standalone dashboard from the same computed observations.
template=(root/'index_template.html').read_text(encoding='utf-8')
(root/'index.html').write_text(template.replace('__DATA__',(root/'data.json').read_text(encoding='utf-8')),encoding='utf-8')

