"""Build a reviewed SHFE/INE daily option OI history for the local website."""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import shfe_gold as g
import shfe_others as other


def trading_candidates(n):
    day=date.today()
    found=[]
    while len(found)<n:
        if day.weekday()<5:found.append(day.strftime('%Y%m%d'))
        day-=timedelta(days=1)
    return found


def one_day(ds):
    # The same official option file contains AG, CU, and SC; one request/day.
    options,_=g.get_json('op',ds)
    entries=options['o_curinstrument']
    result={}
    for product,(_,code,_) in other.PRODUCTS.items():
        sides={'P':0,'C':0};counts={'P':0,'C':0}
        for row in entries:
            inst=str(row.get('INSTRUMENTID') or '').replace(' ','').upper()
            match=re.fullmatch(code.upper()+r'\d{4}([CP])\d+',inst)
            if not match:continue
            side=match.group(1)
            if str(row.get('PRODUCTID','')).lower()!=code+'_o' or str(row.get('OPTIONSTYPE'))!=('1' if side=='C' else '2'):
                raise ValueError(f'{ds}: {inst} 品种或期权类型不一致')
            oi=g.fnum(row.get('OPENINTEREST'))
            if oi is None or oi<0 or int(oi)!=oi:
                raise ValueError(f'{ds}: {inst} 持仓量无效')
            sides[side]+=int(oi);counts[side]+=1
        totals=[r for r in entries if r.get('PRODUCTID')==code+'_o' and str(r.get('INSTRUMENTID')).strip()=='小计']
        if not sides['P'] or not sides['C'] or len(totals)!=1 or g.fnum(totals[0]['OPENINTEREST'])!=sum(sides.values()):
            raise ValueError(f'{ds}: {product} 持仓量与交易所小计不一致')
        result[product]={'date':date.fromisoformat(ds[:4]+'-'+ds[4:6]+'-'+ds[6:]).isoformat(),
                         'put_oi':sides['P'],'call_oi':sides['C'],'pcr':sides['P']/sides['C'],
                         'put_contracts':counts['P'],'call_contracts':counts['C'],
                         'source':'SHFE 日行情（原油为 INE 合约）' if product=='oil' else 'SHFE 日行情'}
    try:
        futures,_=g.get_json('fu',ds)
        for product,(_,code,_) in other.PRODUCTS.items():
            choices=[]
            for row in futures['o_curinstrument']:
                if row.get('PRODUCTID')!=code+'_f':continue
                month=str(row.get('DELIVERYMONTH') or '').strip()
                if not re.fullmatch(r'\d{4}',month):continue
                oi=g.fnum(row.get('OPENINTEREST'));price=g.fnum(row.get('CLOSEPRICE'))
                if oi is not None and price is not None and price>0:choices.append((oi,price,code+month))
            if choices:
                oi,price,inst=max(choices)
                result[product].update(gold_close=price,gold_contract=inst,
                                       underlying_close=price,underlying_contract=inst,underlying_oi=oi)
    except Exception:
        pass  # Option OI is valid; the site labels missing underlying prices.
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--days',type=int,default=260,help='Target number of exchange trading days')
    parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args()
    candidates=trading_candidates(args.days+45)
    archive={k:[] for k in other.PRODUCTS};errors=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending={pool.submit(one_day,ds):ds for ds in candidates}
        for done in as_completed(pending):
            ds=pending[done]
            try:
                result=done.result()
                for k,v in result.items():archive[k].append(v)
                if len(archive['silver'])%25==0:print('Downloaded:',len(archive['silver']),ds,flush=True)
            except Exception as e:errors.append([ds,str(e)[:160]])
    for k in archive:archive[k].sort(key=lambda x:x['date']);archive[k]=archive[k][-args.days:]
    if len(archive['silver'])<20:raise RuntimeError(f'Only {len(archive["silver"])} valid trading days; no file written. Errors: {errors[:5]}')
    archive['_meta']={'option_file_url':g.OPT,'future_file_url':g.FUT,
                      'method':'sum OPENINTEREST by contract C/P, check OPTIONSTYPE and official subtotal',
                      'price':'close of the most-held futures contract for each day'}
    path=other.HISTORY_FILE;temp=path+'.tmp'
    with open(temp,'w',encoding='utf-8') as f:json.dump(archive,f,ensure_ascii=False,separators=(',',':'))
    os.replace(temp,path)
    for k,items in archive.items():
        print(k,len(items),items[0]['date'],items[-1]['date'],'underlying prices',sum(x.get('underlying_close') is not None for x in items))
    print('Skipped',len(errors),'dates; examples',errors[:8])


if __name__=='__main__':main()

