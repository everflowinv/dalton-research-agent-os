import copy
import json
import sqlite3
import unittest
from datetime import date,timedelta
from types import SimpleNamespace
from contextlib import contextmanager
from dalton_core.store import content_hash,canonical_json
from dalton_core.investment_memo_market_history import build,validate_rows,rows_from,project,SUPPLEMENT_BYTES,CONTRACT_REF
from dalton_core.investment_memo_material import bound_material
from dalton_core.investment_memo_draft import citation_index,build_group_prompt
from dalton_core.investment_memo_cli import _validated_group


def seal(w):
    w.pop('content_hash',None);w['content_hash']=content_hash(w);return w


def series(company='company:ticker:other',currency='USD',factor=1):
    begin=date(2023,1,2);bars=[]
    for i in range(4*365):
        d=begin+timedelta(days=i)
        if d.weekday()<5:
            bars.append({'date':d.isoformat(),'close':str(100+i*factor),
                         'adj_close':str(100+i*factor*2),'volume':'1000'})
    return seal({'id':'market-price-series-version:'+company.rsplit(':',1)[-1],
                 'company_ref':company,'version':1,'currency':currency,'source_ref':'source:fixture',
                 'bars':bars,'first_bar_date':bars[0]['date'],'last_bar_date':bars[-1]['date']})


def calendar(company='company:ticker:other'):
    entries=[{'entry_ref':f'catalyst:{i}','event_kind':'earnings','expected_date':f'2026-0{i+1}-15',
              'confidence':'confirmed','disagreement':False,'sources':[{'ref':'sec:filing:actual'}]}
             for i in range(8)]
    return seal({'id':'catalyst-calendar-version:other','company_ref':company,'version':1,'entries':entries})


@contextmanager
def database(*records):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    for table in ('market_price_series_versions','catalyst_calendar_versions'):
        c.execute(f'CREATE TABLE {table}(version_id TEXT PRIMARY KEY,company_ref TEXT,version_number INTEGER,record_json TEXT,content_hash TEXT)')
    for w in records:
        table='market_price_series_versions' if 'bars' in w else 'catalyst_calendar_versions'
        c.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?)',(w['id'],w['company_ref'],1,canonical_json(w),w['content_hash']))
    try:yield c
    finally:c.close()


class MemoMarketHistoryTests(unittest.TestCase):
    def test_two_companies_exact_returns_bounded_prompt_and_frozen_proof(self):
        for company,currency in [('company:ticker:other','USD'),('company:sec-cik:0000000001','EUR')]:
            s=series(company,currency);p=series('company:benchmark:SPY',currency,3)
            rows=build(s,benchmark=p)
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[-1]['market_history_proof']['requested']['years'],3)
            proof=rows[-1]['market_history_proof']
            self.assertNotEqual(proof['result']['close_price_return_percent'],proof['result']['adjusted_price_return_percent'])
            with database(s,p) as c:
                validate_rows(c,rows,company)
                compact=bound_material(rows,company_ref=company)
                self.assertEqual(compact,project(rows))
                validate_rows(c,compact,company)
                self.assertEqual(set(citation_index(compact)['numbers']),{r['ref']for r in rows})
                prompt=build_group_prompt(group='view',section_titles=['S'],questions=[],material=compact,company={'company_ref':company})
                self.assertLess(len(prompt.encode()),22000)
                self.assertIn('never approve a trade',prompt)
                self.assertNotIn('"bars":',prompt)

    def test_tampered_value_window_source_rule_company_and_currency_refused(self):
        s=series();p=series('company:benchmark:SPY');rows=build(s,benchmark=p)
        with database(s,p) as c:
            for field in ('text','hash','period'):
                altered=copy.deepcopy(rows);altered[0][field]='changed'
                with self.assertRaises(ValueError):validate_rows(c,altered,s['company_ref'])
            altered=copy.deepcopy(rows);altered[0]['market_history_proof']['contract_ref']='unknown'
            with self.assertRaises(ValueError):validate_rows(c,altered,s['company_ref'])
            with self.assertRaises(ValueError):validate_rows(c,rows,'company:ticker:foreign')
        with self.assertRaises(ValueError):build(s,benchmark=series('company:benchmark:SPY','EUR'))

    def test_missing_or_short_history_adjustment_and_provisional_are_not_invented(self):
        s=series();s['bars']=s['bars'][-60:];seal(s)
        self.assertEqual(build(s),[])
        s=series();s['bars'][-1]['adj_close']=None;seal(s)
        row=build(s)[0]
        self.assertIsNone(row['market_history_proof']['result']['adjusted_price_return_percent'])
        self.assertIn('不能据此计算股东总回报',row['text'])
        s=series();s['bars'][-1]['captured_at']=s['bars'][-1]['date']+'T16:00:00+00:00';seal(s)
        self.assertNotEqual(build(s)[0]['market_history_proof']['endpoints'][-1]['date'],s['bars'][-1]['date'])
        s['bars'][-2]['close']='0';seal(s)
        with self.assertRaises(ValueError):build(s)
        s=series()
        s['bars'].append(dict(s['bars'][-1]));seal(s)
        with self.assertRaises(ValueError):build(s)

    def test_event_dates_are_confirmed_source_bound_finite_and_not_causal_claims(self):
        s=series();cal=calendar();rows=build(s,calendar=cal)
        events=[r for r in rows if r['market_history_proof']['event']]
        self.assertEqual(len(events),6)
        self.assertEqual(events[0]['market_history_proof']['event']['omitted_event_count'],2)
        self.assertIn('不能据此认定因果',events[0]['text'])
        with database(s,cal) as c:validate_rows(c,rows,s['company_ref'])
        cal['entries'][0]['confidence']='estimated';cal['entries'][1]['disagreement']=True;seal(cal)
        selected=build(s,calendar=cal)
        self.assertTrue(all(r['market_history_proof']['event']['entry_ref']not in {'catalyst:0','catalyst:1'}for r in selected if r['market_history_proof']['event']))
        cal['company_ref']='company:ticker:foreign';seal(cal)
        with self.assertRaises(ValueError):build(s,calendar=cal)

    def test_actual_adapter_boundary_checks_history_before_any_section_acceptance(self):
        s=series();rows=build(s)
        with database(s) as c:
            altered=copy.deepcopy(rows);altered[0]['text']='价格回报 999%'
            outcome={'status':'drafted','sections':[],'key_questions':[]}
            result=_validated_group(SimpleNamespace(connection=c,path=':memory:'),outcome,altered,company_ref=s['company_ref'])
            self.assertEqual(result['status'],'refused')
            self.assertEqual(result['failure_class'],'source_unavailable')
            self.assertFalse(result['repairable'])
            accepted=_validated_group(SimpleNamespace(connection=c,path=':memory:'),outcome,rows,company_ref=s['company_ref'])
            self.assertEqual(accepted['status'],'drafted')
            row=rows[-1]
            section={'title':'历史价格','body':row['text'],'claim_refs':[],
                     'numbers':[{'text':row['text'],'number_ref':row['ref'],'period':row['period']}],'gaps':[]}
            accepted=_validated_group(SimpleNamespace(connection=c,path=':memory:'),
                {**outcome,'sections':[section]},rows,company_ref=s['company_ref'])
            self.assertEqual(accepted['status'],'drafted')
            section['body']+=' 收益率 99999%。'
            refused=_validated_group(SimpleNamespace(connection=c,path=':memory:'),
                {**outcome,'sections':[section]},rows,company_ref=s['company_ref'])
            self.assertEqual(refused['failure_class'],'content')

    def test_exact_fraction_rounding_does_not_double_count_dividends(self):
        from dalton_core.investment_memo_market_history import _return
        self.assertEqual(_return({'close':'200000000'},{'close':'200000001'},'close'),'0.000001')
        self.assertEqual(_return({'close':'200000000'},{'close':'199999999'},'close'),'-0.000001')

    def test_corrupt_history_is_rejected_before_first_model_call(self):
        from dalton_core.investment_memo_cli import run_memo
        s=series();rows=build(s);rows[0]['hash']='a'*64
        class NeverCall:
            def call(self,**kwargs):raise AssertionError('corrupt source must not buy a model')
        with database(s) as c:
            result=run_memo(store=SimpleNamespace(connection=c,path=':memory:'),
                frozen={'status':'ready','mission':{},'playbook':{},'company':{'company_ref':s['company_ref']},'material':rows},
                model=NeverCall(),verifier_model=NeverCall())
            self.assertEqual(result['status'],'held')
            self.assertEqual(result['memo_status'],'source_unavailable')
            self.assertEqual(result['cost_micros'],0)

    def test_exact_old_source_still_replays_after_latest_series_changes(self):
        s=series();rows=build(s)
        with database(s) as c:
            newer=copy.deepcopy(s);newer['id']='market-price-series-version:newer';newer['version']=2;newer['bars'][-1]['close']='8888';seal(newer)
            c.execute('INSERT INTO market_price_series_versions VALUES (?,?,?,?,?)',(newer['id'],s['company_ref'],2,canonical_json(newer),newer['content_hash']))
            validate_rows(c,rows,s['company_ref'])
            self.assertNotEqual(rows,rows_from(c,s['company_ref']))
            c.execute('UPDATE market_price_series_versions SET record_json=? WHERE version_id=?',(canonical_json(newer),s['id']))
            with self.assertRaises(ValueError):validate_rows(c,rows,s['company_ref'])

    def test_legacy_material_and_prompt_bytes_unchanged_without_new_proof(self):
        rows=[{'ref':'market-block:legacy','hash':'a'*64,'kind':'market_block','text':'收盘价 100 USD'}]
        self.assertEqual(bound_material(rows,company_ref='company:ticker:other'),rows)
        with database() as c:validate_rows(c,rows,'company:ticker:other')
        p=build_group_prompt(group='view',section_titles=['S'],questions=[],material=rows,company={'company_ref':'company:ticker:other'})
        self.assertNotIn('Source-bound price-window rows',p)

    def test_finite_supplement_preserves_every_original_selected_citation(self):
        s=series();cal=calendar();history=build(s,calendar=cal)
        originals=[{'ref':f'pack-number:{i}','kind':'pack_number','hash':'a'*64,
                    'text':'原申报收入 100 USD；'+('original citation '*50)}for i in range(60)]
        prior=bound_material(originals,company_ref=s['company_ref'])
        current=bound_material(history+originals,company_ref=s['company_ref'])
        self.assertEqual([r for r in current if not r.get('market_history_proof')],prior)
        selected=[r for r in current if r.get('market_history_proof')]
        self.assertLessEqual(len(canonical_json(selected).encode()),SUPPLEMENT_BYTES)
        self.assertTrue(any(r['market_history_proof']['requested'].get('years')==3 for r in selected))
        self.assertLess(len(selected),len(history))
        with database(s,cal) as c:
            validate_rows(c,selected,s['company_ref'])
            altered=copy.deepcopy(selected);altered[0]['market_history_projection']['omitted_windows_hash']='a'*64
            with self.assertRaises(ValueError):validate_rows(c,altered,s['company_ref'])
            altered=copy.deepcopy(selected);del altered[0]['market_history_proof']['series']
            with self.assertRaises(ValueError):validate_rows(c,altered,s['company_ref'])


if __name__=='__main__':unittest.main()
