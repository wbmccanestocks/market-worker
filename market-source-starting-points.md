# Market Source Starting Points

Use these fixed public sources at the start of each hourly market-worker run before doing open-ended search. Prefer fetching source pages directly when available. Use search only to fill specific gaps after the fixed-source pass.

Before fetching public news, refresh Schwab holdings through the read-only `market_schwab.py sync` snapshot. Use held positions and the fixed core watchlist for the news keyword set. Discover sector candidates from ticker-shaped symbols found on the fixed Technology, Energy, Biotech, and other sector/news pages, validate them against cached Nasdaq market data when available, and pass discovered symbols into confirmation analysis. Do not treat holdings or watchlist membership itself as news or a catalyst. Never call broker order, trade, transfer, or watchlist-write endpoints from market-worker.

## Primary Fixed Sources

### Broad Market News

- MarketWatch Markets: https://www.marketwatch.com/markets
- MarketWatch Latest News: https://www.marketwatch.com/latest-news
- Yahoo Finance: https://finance.yahoo.com/
- Yahoo Finance News: https://finance.yahoo.com/news/
- Benzinga Markets: https://www.benzinga.com/markets
- Benzinga News: https://www.benzinga.com/news

### Government Trades

- Benzinga Govenrment Trades: https://www.benzinga.com/government-trades

### Biotech

- Benzinga Biotech: https://www.benzinga.com/general/biotech
- Yahoo Finance Biotechnology News: https://finance.yahoo.com/sectors/healthcare/biotechnology/

### Technology

- MarketWatch S&P 500 Technology: https://www.marketwatch.com/investing/index/sp500.45?countrycode=xx
- MarketWatch Technology Index: https://www.marketwatch.com/investing/index/inft
- Yahoo Finance Technology News: https://finance.yahoo.com/topic/tech/
- Yahoo Finance Technology News: https://finance.yahoo.com/sectors/technology/
- Benzinga Tech: https://www.benzinga.com/tech

### Energy

- MarketWatch Energy Index: https://www.marketwatch.com/investing/index/enrs
- Yahoo Finance Energy News: https://finance.yahoo.com/sectors/energy/
- Benzinga Energy: https://www.benzinga.com/topic/energy

### Earnings And Calendars

- Yahoo Finance Earnings Calendar: https://finance.yahoo.com/calendar/earnings
- MarketWatch Earnings Calendar: https://www.marketwatch.com/tools/earnings-calendar
- Benzinga Earnings: https://www.benzinga.com/calendars/earnings

### Movers And Watchlists

- Yahoo Finance Gainers: https://finance.yahoo.com/markets/stocks/gainers/
- Yahoo Finance Losers: https://finance.yahoo.com/markets/stocks/losers/
- Yahoo Finance Most Active: https://finance.yahoo.com/markets/stocks/most-active/
- MarketWatch Market Data Center: https://www.marketwatch.com/tools/markets
- Benzinga Stock Movers: https://www.benzinga.com/movers

### Curated Watch Ideas (Discovery Only)

- StocksToTrade Stocks to Watch: https://stockstotrade.com/stocks-to-watch/

StocksToTrade is a dated editorial watchlist with ticker-specific catalyst and
risk discussion. Use it only to discover candidates: it frequently highlights
low-float, penny-stock, and momentum names. A StocksToTrade mention is not an
independent catalyst verification and cannot by itself support a BUY. Every
candidate must still pass the existing liquidity, anti-chase, price/sector, and
separate current-source confirmation gates.

## Search Fallback

Only search after the fixed-source pass, and make each query narrow enough to fill a named blank. Prefer key-free search such as DuckDuckGo or a configured SearXNG instance. Do not use paid Gemini/Google grounding for this workflow unless the user explicitly re-enables it.

Useful fallback query shapes:

- `site:marketwatch.com technology stocks today catalyst`
- `site:finance.yahoo.com technology stocks earnings guidance`
- `site:benzinga.com technology stocks analyst rating`
- `site:marketwatch.com energy stocks today oil natural gas`
- `site:finance.yahoo.com energy stocks earnings guidance`
- `site:benzinga.com energy stocks analyst rating`
- `small cap technology stock catalyst today`
- `small cap energy stock catalyst today`

## RSS/Atom Listener

The deterministic listener reads `market-rss-feeds.json`. Add public feeds as
objects with `label`, `category`, and `url`, for example:

```json
{"label":"Example technology feed","category":"Technology","url":"https://publisher.example/feed.xml"}
```

Each scheduled worker run polls the configured feeds, deduplicates entries by
feed URL plus GUID/entry ID, and uses BeautifulSoup to convert HTML/XHTML entry
content to clean text. New, non-promotional records are sent to Luna first and
only Luna-retained records reach Terra. The deterministic pre-filter drops only
obvious promotional calls to action; it does not reject ordinary macro or
political items, because Luna remains the relevance authority. First run creates
a baseline (no historical flood); set `MARKET_WORKER_RSS_BACKFILL=1` only to
intentionally process already-seen entries.

## Run Order

1. Refresh the read-only Schwab position snapshot, then select held symbols and the fixed core watchlist for the news keyword set.
2. Read recent packet files and avoid duplicate URLs unless there is a clear update signal.
3. Fetch or inspect the fixed starting-point URLs above.
4. Extract ticker/catalyst/risk facts and discover analyzable symbols directly from fresh or changed sector/news items.
5. Identify missing buckets, such as no energy catalyst, no earnings item, no analyst action, or no mover explanation.
6. Use search fallback only for those missing buckets.
7. Write one compact packet with `sources`, `extracted_facts`, `ranking_notes`, and `data_gaps`.
