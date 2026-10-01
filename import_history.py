"""Import licensed historical daily put/call OI supplied by the user.

CSV headers: date,product,put_oi,call_oi[,futures_close]
product: gold,silver,copper,wti,gas,corn,soybean,wheat,soyoil
"""
import csv,json,os,sys
from datetime import date
from app import PRODUCTS,DATA

def import_file(path):
 rows={k:{} for k in PRODUCTS}
 with open(path,encoding='utf-8-sig',newline='') as f:
  rd=csv.DictReader(f)
  if not {'date','product','put_oi','call_oi'}<=set(rd.fieldnames or []):
   raise ValueError('CSV 缺少 date,product,put_oi,call_oi 列')
  for line,r in enumerate(rd,2):
   k=r['product'].strip().lower();ds=r['date'].strip()
   if k not in PRODUCTS:raise ValueError(f'第 {line} 行未知品种: {k}')
   try:date.fromisoformat(ds);p=int(r['put_oi'].replace(',',''));c=int(r['call_oi'].replace(',',''))
   except Exception as e:raise ValueError(f'第 {line} 行日期或持仓量无效: {e}')
   if p<0 or c<=0:raise ValueError(f'第 {line} 行持仓量无效')
   if ds in rows[k]:raise ValueError(f'第 {line} 行重复日期: {k} {ds}')
   row={'date':ds,'put_oi':p,'call_oi':c,'pcr':round(p/c,8),'source':'导入历史数据','scope':'请确认与当日公告的标准月度期权口径一致'}
   close=(r.get('futures_close') or '').strip()
   if close:row['futures_close']=float(close)
   rows[k][ds]=row
 total=0
 for k,incoming in rows.items():
  if not incoming:continue
  target=os.path.join(DATA,k+'.json')
  existing=json.load(open(target,encoding='utf-8')) if os.path.exists(target) else []
  merged={x['date']:x for x in existing}
  for ds,row in incoming.items():
   if ds in merged and (merged[ds]['put_oi'],merged[ds]['call_oi'])!=(row['put_oi'],row['call_oi']):
    raise ValueError(f'已有数据与导入冲突: {k} {ds}；请核对期权口径')
   if ds not in merged:merged[ds]=row
  ordered=[merged[d] for d in sorted(merged)]
  temp=target+'.tmp'
  with open(temp,'w',encoding='utf-8') as f:json.dump(ordered,f,ensure_ascii=False)
  os.replace(temp,target)
  total+=len(incoming)
  print(f'{PRODUCTS[k][0]}: {len(ordered)} 日，{ordered[0]["date"]} 至 {ordered[-1]["date"]}')
 print(f'导入检查完成，共处理 {total} 行。')

if __name__=='__main__':
 if len(sys.argv)!=2:raise SystemExit('用法: python import_history.py 历史数据.csv')
 import_file(sys.argv[1])

