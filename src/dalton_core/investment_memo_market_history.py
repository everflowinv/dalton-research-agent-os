"""Finite, source-bound price windows; no retrieval, portfolio or trade authority."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction
from collections.abc import Mapping

from .store import content_hash

CONTRACT_REF = 'investment-memo-market-history:0.1'
PREFIX = 'memo-market-history:'
MAX_EVENTS = 6
SUPPLEMENT_BYTES = 8000


def _calendar_binding(rows, calendar):
    for row in rows:
        row['market_history_proof']['calendar'] = (None if calendar is None else
            {'id':calendar['id'],'content_hash':calendar['content_hash']})
        row['hash']=content_hash(row['market_history_proof']);row['ref']=PREFIX+row['hash']
    return rows


def project(rows):
    """Independent finite supplement; never displaces existing selected citations."""
    def rank(row):
        req=row['market_history_proof']['requested']
        return (0 if req.get('years')==3 else 1 if req['kind']=='event_window' else 2,
                -int(req.get('years',0)),-_date(row['period'].split('/')[-1]).toordinal(),row['ref'])
    ordered=sorted(rows,key=rank);selected=[]
    for row in ordered:
        # The projection metadata has its own small bounded cost.
        if sum(len(_json(r)) for r in selected)+len(_json(row))+300*(len(selected)+1)>SUPPLEMENT_BYTES:continue
        selected.append(row)
    while selected:
        omitted=[r for r in ordered if r not in selected]
        scope={'contract_ref':CONTRACT_REF,'byte_limit':SUPPLEMENT_BYTES,
               'available_window_count':len(ordered),'selected_window_count':len(selected),
               'omitted_windows_hash':content_hash([(r['ref'],r['hash'])for r in omitted])}
        result=[{**r,'market_history_projection':scope}for r in selected]
        if len(_json(result))<=SUPPLEMENT_BYTES:return result
        selected.pop()
    return []


def _json(value):
    import json
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def _date(value):
    return date.fromisoformat(value)


def _number(value):
    if len(str(value))>128:raise ValueError('price text exceeds finite arithmetic bound')
    value = Decimal(str(value))
    if not value.is_finite() or value <= 0 or abs(value.adjusted())>128:
        raise ValueError('price window requires finite positive source prices')
    return value


def _bars(series):
    if not isinstance(series, Mapping) or series.get('content_hash') != content_hash(
            {k:v for k,v in series.items() if k != 'content_hash'}):
        raise ValueError('price series hash differs')
    bars = list(series.get('bars') or [])
    if len(bars)>6000:raise ValueError('price history exceeds source contract bound')
    dates = [b['date'] for b in bars]
    if dates != sorted(set(dates)):
        raise ValueError('price series dates duplicate or are unordered')
    # An intraday observation is never an event's final close.
    from .market_price import bar_is_provisional
    return [b for b in bars if not bar_is_provisional(b)]


def _years_back(day, years):
    try:return day.replace(year=day.year-years)
    except ValueError:return day.replace(year=day.year-years, day=28)


def _return(start, end, field):
    if start.get(field) is None or end.get(field) is None:return None
    value = (Fraction(_number(end[field])) / Fraction(_number(start[field])) - 1) * 100
    magnitude,remainder=divmod(abs(value.numerator)*1000000,value.denominator)
    magnitude+=int(remainder*2>=value.denominator)
    return ('-' if value<0 and magnitude else '')+str(magnitude//1000000)+'.'+str(magnitude%1000000).zfill(6)


def _binding(series):
    return {k:series[k] for k in ('id','content_hash','company_ref','source_ref','currency')}


def _endpoint(bar):
    return {'date':bar['date'],'close':bar['close'],'adj_close':bar.get('adj_close'),
            'source_bar_hash':content_hash(bar)}


def _window(series, bars, start, end, requested, benchmark, benchmark_bars, event=None):
    endpoints = [_endpoint(start),_endpoint(end)]
    result = {'close_price_return_percent':_return(start,end,'close'),
              'adjusted_price_return_percent':_return(start,end,'adj_close')}
    peer = None
    if benchmark is not None:
        if benchmark['currency'] != series['currency']:
            raise ValueError('benchmark price-window currency differs')
        by_date = {b['date']:b for b in benchmark_bars}
        if start['date'] in by_date and end['date'] in by_date:
            pair = [by_date[start['date']],by_date[end['date']]]
            peer = {'binding':_binding(benchmark),'endpoints':[_endpoint(b) for b in pair],
                    'close_price_return_percent':_return(*pair,'close'),
                    'adjusted_price_return_percent':_return(*pair,'adj_close')}
    proof = {'contract_ref':CONTRACT_REF,'series':_binding(series), 'requested':requested,
             'endpoints':endpoints,'result':result,'benchmark':peer,'event':event,
             'all_bars_hash':content_hash(series['bars']), 'source_bar_count':len(series['bars']),
             'selected_bar_count':2,'omitted_bar_count':len(series['bars'])-2}
    text = (f"价格窗口 {start['date']} 至 {end['date']}；币种 {series['currency']}；"
            f"原始收盘价 {start['close']} 至 {end['close']}；价格回报 {result['close_price_return_percent']}%；")
    if result['adjusted_price_return_percent'] is not None:
        text += f"复权价格回报 {result['adjusted_price_return_percent']}%（采用源 adj_close，不另加股息）；"
    else:text += '复权价格缺项，不能据此计算股东总回报；'
    if peer is not None:
        text += f"同日期基准 {benchmark['company_ref']} 价格回报 {peer['close_price_return_percent']}%；"
        if peer['adjusted_price_return_percent'] is not None:
            text += f"基准复权价格回报 {peer['adjusted_price_return_percent']}%；"
    else:text += '同日期基准缺项；'
    if event is not None:
        text += f"已确认事件日期 {event['date']}，类型 {event['kind']}；仅观察窗口，不能据此认定因果。"
    digest = content_hash(proof)
    return {'ref':PREFIX+digest,'hash':digest,'kind':'market_block','text':text,
            'source_kind':'market','period':start['date']+'/'+end['date'],
            'market_history_proof':proof}


def build(series, *, benchmark=None, calendar=None):
    """Three trailing windows and at most six independently confirmed event windows."""
    bars = _bars(series)
    peer_bars = [] if benchmark is None else _bars(benchmark)
    if not bars:return []
    end = bars[-1];last = _date(end['date']);rows=[]
    for years in (1,2,3):
        stop = _years_back(last, years)
        choices = [b for b in bars if _date(b['date']) <= stop]
        if not choices or (stop-_date(choices[-1]['date'])).days > 7:continue
        rows.append(_window(series,bars,choices[-1],end,
                            {'kind':'trailing_years','years':years,'anniversary':stop.isoformat()},
                            benchmark,peer_bars))
    if calendar is None:return _calendar_binding(rows,None)
    if calendar.get('company_ref') != series['company_ref'] or calendar.get('content_hash') != content_hash(
            {k:v for k,v in calendar.items() if k != 'content_hash'}):
        raise ValueError('event calendar company/hash differs')
    if len(calendar['entries'])>512:raise ValueError('event calendar exceeds summary bound')
    earliest = _years_back(last,3).isoformat()
    events = [e for e in calendar['entries'] if e.get('confidence')=='confirmed'
              and not e.get('disagreement') and earliest<=e['expected_date']<=end['date']]
    events.sort(key=lambda e:(e['expected_date'],e['entry_ref']),reverse=True)
    for e in events[:MAX_EVENTS]:
        positions=[i for i,b in enumerate(bars) if b['date']>=e['expected_date']]
        if not positions:continue
        i=positions[0]
        if i<1 or i+2>=len(bars) or (_date(bars[i]['date'])-_date(e['expected_date'])).days>7:continue
        event={'date':e['expected_date'],'kind':e['event_kind'],'entry_ref':e['entry_ref'],
               'calendar_ref':calendar['id'],'calendar_hash':calendar['content_hash'],
               'entry_hash':content_hash(e),'all_eligible_events_hash':content_hash(events),
               'eligible_event_count':len(events),'omitted_event_count':max(0,len(events)-MAX_EVENTS)}
        rows.append(_window(series,bars,bars[i-1],bars[i+2],
                    {'kind':'event_window','before_trading_days':1,'after_trading_days':3},
                    benchmark,peer_bars,event))
    return _calendar_binding(rows,calendar)


def _version(connection, table, ref, decode):
    from .market_price import MarketPriceError
    from .catalyst_calendar import CatalystCalendarError
    row=connection.execute(f'SELECT * FROM {table} WHERE version_id=?',(ref,)).fetchone()
    try:return decode(row,ref)
    except (MarketPriceError,CatalystCalendarError) as exc:raise ValueError(str(exc)) from exc


def rows_from(connection, company_ref, *, benchmark_ref=None):
    from .market_price import _decode
    from .catalyst_calendar import _decode as decode_calendar
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_price_series_versions'").fetchone():return []
    own=connection.execute('SELECT * FROM market_price_series_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1',(company_ref,)).fetchone()
    if own is None:return []
    series=_decode(own,company_ref);peer=None;calendar=None
    if benchmark_ref is None:
        from .market_data_block import BENCHMARK_TICKERS
        for ticker in BENCHMARK_TICKERS:
            ref='company:benchmark:'+ticker
            if connection.execute('SELECT 1 FROM market_price_series_versions WHERE company_ref=? LIMIT 1',(ref,)).fetchone():
                benchmark_ref=ref;break
    if benchmark_ref:
        row=connection.execute('SELECT * FROM market_price_series_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1',(benchmark_ref,)).fetchone()
        if row is not None:peer=_decode(row,benchmark_ref)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalyst_calendar_versions'").fetchone():
        row=connection.execute('SELECT * FROM catalyst_calendar_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1',(company_ref,)).fetchone()
        if row is not None:calendar=decode_calendar(row,company_ref)
    return build(series,benchmark=peer,calendar=calendar)


def _validate_rows(connection, material, company_ref):
    """Rebuild from exact immutable versions, including original calendar date authority."""
    from .market_price import _decode
    from .catalyst_calendar import _decode as decode_calendar
    cache={}
    for row in material:
        if not str(row.get('ref','')).startswith(PREFIX):
            if 'market_history_proof' in row:raise ValueError('history proof lacks its namespace')
            continue
        p=row.get('market_history_proof')
        if not isinstance(p,Mapping) or p.get('contract_ref')!=CONTRACT_REF or p['series']['company_ref']!=company_ref:
            raise ValueError('history proof contract/company differs')
        key=(p['series']['id'],None if p['benchmark'] is None else p['benchmark']['binding']['id'],
             None if p['calendar'] is None else p['calendar']['id'])
        if key not in cache:
            own=_version(connection,'market_price_series_versions',key[0],_decode)
            peer=None if key[1] is None else _version(connection,'market_price_series_versions',key[1],_decode)
            cal=None if key[2] is None else _version(connection,'catalyst_calendar_versions',key[2],decode_calendar)
            built=build(own,benchmark=peer,calendar=cal)
            cache[key]=({r['ref']:r for r in built},{r['ref']:r for r in project(built)})
        expected=cache[key][int('market_history_projection' in row)].get(row['ref'])
        if dict(row)!=expected:raise ValueError('history row does not replay exact source window')


def validate_rows(connection, material, company_ref):
    try:return _validate_rows(connection,material,company_ref)
    except (KeyError,TypeError) as exc:raise ValueError('history proof is structurally incomplete') from exc


def checked_material(store, material, company_ref):
    if not any(str(r.get('ref','')).startswith(PREFIX) or 'market_history_proof' in r for r in material):return
    if getattr(store,'connection',None) is not None:
        validate_rows(store.connection,material,company_ref)
    else:
        from .planner_paid_output_recovery import _authority_connection
        with _authority_connection(store.path) as connection:validate_rows(connection,material,company_ref)
