# HolderScope — Free Vercel MVP

## Features
- Token/pool discovery by name, symbol or contract via GeckoTerminal public API.
- Holder lookup: Blockscout public APIs for Ethereum, Base, Optimism and Gnosis; Solana public RPC for top token accounts.
- DEX sell activity: identifies swaps where the searched token is the token sold; shows individual trades and aggregates by sender wallet; sort by USD value or token amount.
- No Moralis key or paid API key required.

## Important data coverage limitation
GeckoTerminal's free v2 `/networks/{network}/pools/{pool_address}/trades` endpoint documents past-24-hour trades (up to 300 per pool). The UI filters returned records against the requested seven-day window but **cannot provide complete seven-day history** from this free endpoint. Results are best-effort and the coverage warning is displayed. A genuine complete rolling 7-day dataset requires persistent storage plus scheduled ingestion, or a provider/API plan with historical trade pagination.

## Deploy to Vercel
Import the repository containing `vercel.json`, `api/`, and `app/` at its root. Python runtime is configured through `api/index.py`. No environment variables are required for default public endpoints. Optional `SOLANA_RPC_URL` can override the public Solana RPC.

## Local run
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API routes
- `GET /api/search?q=PEPE`
- `GET /api/holders?chain=ethereum&address=0x...&limit=20`
- `GET /api/sells?chain=ethereum&address=0x...&days=7&sort=usd`
- `GET /health`

Public endpoints are rate-limited and may change. Token search results are pool-derived and should be verified by network and contract address.
