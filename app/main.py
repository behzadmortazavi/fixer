from __future__ import annotations
import base64, os, re, asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
SOLANA_RPC = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com').strip()
GECKO = 'https://api.geckoterminal.com/api/v2'
app = FastAPI(title='HolderScope', version='0.5.0')
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')

BLOCKSCOUT = {
    'ethereum': ('https://eth.blockscout.com', 'https://etherscan.io', 'eth'),
    'base': ('https://base.blockscout.com', 'https://basescan.org', 'base'),
    'optimism': ('https://optimism.blockscout.com', 'https://optimistic.etherscan.io', 'optimism'),
    'gnosis': ('https://gnosis.blockscout.com', 'https://gnosisscan.io', 'xdai'),
}
GECKO_NETWORKS = {
    'ethereum':'eth','base':'base','optimism':'optimism','gnosis':'xdai',
    'bsc':'bsc','polygon':'polygon_pos','arbitrum':'arbitrum','avalanche':'avax',
    'linea':'linea','fantom':'ftm','ronin':'ronin','solana':'solana'
}
EXPLORERS = {
    'ethereum':'https://etherscan.io','base':'https://basescan.org','optimism':'https://optimistic.etherscan.io',
    'gnosis':'https://gnosisscan.io','bsc':'https://bscscan.com','polygon':'https://polygonscan.com',
    'arbitrum':'https://arbiscan.io','avalanche':'https://snowtrace.io','linea':'https://lineascan.build',
    'fantom':'https://ftmscan.com','ronin':'https://app.roninchain.com','solana':'https://solscan.io'
}

@app.get('/')
def index(): return FileResponse(BASE_DIR / 'static' / 'index.html')

@app.get('/health')
def health(): return {'status':'ok','providers':['Blockscout public API','Solana public RPC','GeckoTerminal public API']}

async def request_json(url: str, params: dict | None = None):
    headers={'accept':'application/json','user-agent':'HolderScope/0.6 (public-data explorer)'}
    # A small retry/backoff helps transient public API throttling, without creating request storms.
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            r=await client.get(url,params=params)
        if r.status_code == 429 and attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            detail=r.text[:350].replace('\n',' ')
            if r.status_code == 429:
                detail='The public API rate limit is reached. Wait a little and try again; public sources are shared.'
            raise HTTPException(502,f'Public data provider returned HTTP {r.status_code}: {detail}')
        try: return r.json()
        except ValueError: raise HTTPException(502,'Public data provider returned invalid JSON.')
    raise HTTPException(502,'Public data source is temporarily rate-limited. Please retry later.')

@app.get('/api/search')
async def search_tokens(q: str = Query(..., min_length=1, max_length=100)):
    """Search public GeckoTerminal pools by token name, symbol or address."""
    data=await request_json(f'{GECKO}/search/pools', {'query':q.strip(),'include':'base_token,quote_token,dex'})
    included={x.get('id'):x for x in data.get('included',[]) if x.get('type')=='token'}
    results=[]; seen=set()
    for pool in data.get('data',[]):
        attrs=pool.get('attributes') or {}; rel=pool.get('relationships') or {}
        net=pool.get('relationships',{}).get('network',{}).get('data',{}).get('id')
        pool_id=pool.get('id',''); net=net or (pool_id.split('_',1)[0] if '_' in pool_id else '')
        # GeckoTerminal pool search includes the tokens as relationships.
        token_candidates=[]
        for side in ('base_token','quote_token'):
            tid=(rel.get(side) or {}).get('data',{}).get('id')
            tok=included.get(tid)
            if tok:
                ta=tok.get('attributes') or {}
                token_candidates.append((tid,ta))
        for tid,ta in token_candidates:
            key=(net,tid)
            if key in seen: continue
            seen.add(key)
            network=next((k for k,v in GECKO_NETWORKS.items() if v==net),net)
            results.append({'name':ta.get('name') or 'Unknown token','symbol':ta.get('symbol') or '',
                'address':ta.get('address') or tid.split('_',1)[-1], 'network':network,
                'network_id':net,'pool_address':attrs.get('address') or pool_id.split('_',1)[-1],
                'pool_name':attrs.get('name') or '', 'price_usd':attrs.get('base_token_price_usd'),
                'volume_24h':(attrs.get('volume_usd') or {}).get('h24') if isinstance(attrs.get('volume_usd'),dict) else None,
                'explorer_url':f"{EXPLORERS.get(network,'https://geckoterminal.com')}/token/{ta.get('address') or tid.split('_',1)[-1]}"})
    return {'results':results[:30], 'provider':'GeckoTerminal public API', 'note':'Search results are pool-derived. Verify network and contract address before analysis.'}

@app.get('/api/holders')
async def holders(chain: str = Query(...), address: str = Query(..., min_length=20, max_length=100), limit: int = Query(20, ge=1, le=50)):
    chain,address=chain.lower().strip(),address.strip()
    if chain=='solana':
        if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',address): raise HTTPException(400,'Enter a valid Solana token mint address.')
        return await solana_holders(address,limit)
    if chain not in BLOCKSCOUT: raise HTTPException(400,'Free Blockscout holder support currently covers Ethereum, Base, Optimism and Gnosis. Other networks may support trades/search only.')
    if not re.fullmatch(r'0x[a-fA-F0-9]{40}',address): raise HTTPException(400,'Enter a valid EVM token contract address.')
    base,explorer,_=BLOCKSCOUT[chain]
    token,holder_data=await __load_evm(base,address)
    decimals=int(token.get('decimals') or 0); raw_supply=token.get('total_supply'); supply=None
    if raw_supply is not None:
        try: supply=Decimal(str(raw_supply))/(Decimal(10)**decimals)
        except (InvalidOperation,ValueError): pass
    items=holder_data.get('items',[]) if isinstance(holder_data,dict) else []; rows=[]
    for item in items:
        ao=item.get('address') or {}; owner=ao.get('hash') if isinstance(ao,dict) else ao
        raw=item.get('value') or item.get('token_balance') or '0'
        if not owner: continue
        try:
            bal=Decimal(str(raw))/(Decimal(10)**decimals)
            pct=float(Decimal(str(raw))/Decimal(str(raw_supply))*100) if raw_supply and Decimal(str(raw_supply)) else None
        except (InvalidOperation,ValueError,ZeroDivisionError): bal,pct=Decimal(0),None
        rows.append({'rank':len(rows)+1,'address':owner,'balance':str(bal),'percentage':pct,'explorer_url':f'{explorer}/address/{owner}'})
        if len(rows)>=limit: break
    return {'chain':chain,'address':address,'total_supply':str(supply) if supply is not None else None,'decimals':decimals,'holders':rows,'returned':len(rows),'provider':'Blockscout public API','limit_note':'Provider response may be partial and does not necessarily represent every holder.'}

async def __load_evm(base: str,address: str):
    token_url=f'{base}/api/v2/tokens/{address}'; holders_url=f'{token_url}/holders'
    async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers={'accept':'application/json','user-agent':'HolderScope/0.4'}) as client:
        tr=await client.get(token_url)
        if tr.status_code>=400: raise HTTPException(502,f'Token lookup failed (HTTP {tr.status_code}): {tr.text[:350]}')
        hr=await client.get(holders_url,params={'items_count':50})
        if hr.status_code>=400: raise HTTPException(502,f'Blockscout holders endpoint failed (HTTP {hr.status_code}): {hr.text[:350]}')
        return tr.json(),hr.json()


@app.get('/api/sells')
async def big_sells(chain: str=Query(...), address: str=Query(...,min_length=20,max_length=100), days: int=Query(7,ge=1,le=7), sort: str=Query('usd',pattern='^(usd|amount)$')):
    return await _big_trades('sells', chain, address, days, sort)

@app.get('/api/buys')
async def big_buys(chain: str=Query(...), address: str=Query(...,min_length=20,max_length=100), days: int=Query(7,ge=1,le=7), sort: str=Query('usd',pattern='^(usd|amount)$')):
    return await _big_trades('buys', chain, address, days, sort)

async def _big_trades(side: str, chain: str, address: str, days: int, sort: str):
    chain=chain.lower().strip(); address=address.strip(); net=GECKO_NETWORKS.get(chain)
    if not net: raise HTTPException(400,'Unsupported network for DEX trade data.')
    if chain!='solana' and not re.fullmatch(r'0x[a-fA-F0-9]{40}',address): raise HTTPException(400,'Invalid EVM token contract address.')
    if chain=='solana' and not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',address): raise HTTPException(400,'Invalid Solana mint address.')
    pools_data=await request_json(f'{GECKO}/networks/{net}/tokens/{address}/pools',{'page':1,'include':'base_token,quote_token,dex'})
    pools=(pools_data.get('data') or [])[:2]
    if not pools: return {'trades':[],'wallets':[],'returned':0,'coverage_hours':24,'requested_days':days,'coverage_note':'No indexed DEX pools were returned for this token.'}
    cutoff=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).timestamp()-days*86400
    async def pool_trades(pool):
        pa=(pool.get('attributes') or {}).get('address') or pool.get('id','').split('_',1)[-1]
        if not pa: return []
        try:
            payload=await request_json(f'{GECKO}/networks/{net}/pools/{pa}/trades')
            return payload.get('data') or []
        except HTTPException: return []
    trade_sets=[]
    # Fetch pools sequentially to reduce burst pressure on free public endpoints.
    for p in pools:
        trade_sets.append(await pool_trades(p))
        await asyncio.sleep(0.35)
    seen=set(); trades=[]
    for pool,items in zip(pools,trade_sets):
        pa=(pool.get('attributes') or {}).get('address') or pool.get('id','').split('_',1)[-1]
        for item in items:
            a=item.get('attributes') or {}; target_field = 'token_sold_address' if side == 'sells' else 'token_bought_address'
            if (a.get(target_field) or '').lower()!=address.lower(): continue
            tx=a.get('tx_hash') or a.get('transaction_hash') or ''
            key=(tx,pa)
            if key in seen: continue
            seen.add(key)
            ts=a.get('block_timestamp') or a.get('timestamp')
            try:
                parsed=__import__('datetime').datetime.fromisoformat(str(ts).replace('Z','+00:00')).timestamp()
            except Exception: continue
            if parsed<cutoff: continue
            amount=(a.get('token_sold_amount') or a.get('from_token_amount') or a.get('amount') or '0') if side == 'sells' else (a.get('token_bought_amount') or a.get('to_token_amount') or a.get('amount') or '0')
            usd=a.get('volume_in_usd') or '0'
            try: amount_d=Decimal(str(amount)); usd_d=Decimal(str(usd))
            except InvalidOperation: continue
            wallet=a.get('tx_from_address') or a.get('from_address') or ''
            trades.append({'tx_hash':tx,'wallet':wallet,'amount':str(amount_d),'usd':str(usd_d),'timestamp':ts,
                'pool':pa,'pool_name':(pool.get('attributes') or {}).get('name') or '',
                'explorer_url':f"{EXPLORERS.get(chain,'https://geckoterminal.com')}/tx/{tx}" if chain!='solana' else f'https://solscan.io/tx/{tx}'})
    # Free GeckoTerminal v2 trade endpoint only exposes up to 300 trades per pool in the past 24h.
    # Therefore this seven-day view is explicitly best-effort and cannot claim complete 7d coverage.
    trades.sort(key=lambda x: Decimal(x['usd'] if sort=='usd' else x['amount']),reverse=True)
    grouped={}
    for t in trades:
        w=t['wallet'] or 'Unknown sender'
        g=grouped.setdefault(w,{'wallet':w,'trade_count':0,'amount':Decimal(0),'usd':Decimal(0),'trades':[]})
        g['trade_count']+=1; g['amount']+=Decimal(t['amount']); g['usd']+=Decimal(t['usd']); g['trades'].append(t)
    wallets=[{'wallet':w,'trade_count':g['trade_count'],'amount':str(g['amount']),'usd':str(g['usd']),
              'largest_trade_usd':str(max((Decimal(x['usd']) for x in g['trades']),default=Decimal(0))),
              'explorer_url':(g['trades'][0]['explorer_url'])} for w,g in grouped.items()]
    wallets.sort(key=lambda x:Decimal(x['usd'] if sort=='usd' else x['amount']),reverse=True)
    return {'chain':chain,'address':address,'trades':trades[:100],'wallets':wallets[:100], 'returned':len(trades),
            'coverage_hours':24,'requested_days':days,'side':side,'provider':'GeckoTerminal public API',
            'coverage_note':'Important: GeckoTerminal public trades data is limited (up to 300 recent trades per pool, generally within 24 hours). The requested 7-day filter is applied to available records, but complete seven-day coverage is not guaranteed.'}

ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def b58encode(data: bytes) -> str:
    zeros=len(data)-len(data.lstrip(b'\x00')); n=int.from_bytes(data,'big'); out=''
    while n: n,rem=divmod(n,58); out=ALPHABET[rem]+out
    return '1'*zeros+(out or ('' if zeros else '1'))
async def sol_rpc(method: str,params: list[Any]):
    async with httpx.AsyncClient(timeout=25) as client: r=await client.post(SOLANA_RPC,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
    if r.status_code>=400: raise HTTPException(502,f'Solana RPC HTTP {r.status_code}: {r.text[:300]}')
    data=r.json()
    if data.get('error'): raise HTTPException(502,f"Solana RPC error: {data['error'].get('message','unknown error')}")
    return data.get('result')
async def solana_holders(mint: str,limit: int):
    largest,supply=await asyncio.gather(sol_rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}]),sol_rpc('getTokenSupply',[mint,{'commitment':'confirmed'}]))
    accounts=(largest or {}).get('value',[]); sv=(supply or {}).get('value',{}); decimals=int(sv.get('decimals',0)); raw_supply=Decimal(str(sv.get('amount','0'))); total=raw_supply/(Decimal(10)**decimals)
    keys=[x['address'] for x in accounts if x.get('address')]; accs=await sol_rpc('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'confirmed'}]) if keys else {'value':[]}
    grouped={}
    for raw in (accs or {}).get('value',[]):
        if not raw or not raw.get('data'): continue
        try:
            b=base64.b64decode(raw['data'][0]); owner=b58encode(b[32:64]); amount=Decimal(int.from_bytes(b[64:72],'little'))/(Decimal(10)**decimals); grouped[owner]=grouped.get(owner,Decimal(0))+amount
        except Exception: continue
    sorted_rows=sorted(grouped.items(),key=lambda x:x[1],reverse=True)[:min(limit,20)]
    rows=[{'rank':i+1,'address':owner,'balance':str(amount),'percentage':float(amount/total*100) if total else None,'explorer_url':f'https://solscan.io/account/{owner}'} for i,(owner,amount) in enumerate(sorted_rows)]
    return {'chain':'solana','address':mint,'total_supply':str(total),'decimals':decimals,'holders':rows,'returned':len(rows),'provider':'Solana public RPC','limit_note':'Solana public RPC exposes up to 20 largest token accounts; this is not a complete holder list.'}
