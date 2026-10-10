from __future__ import annotations
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.final_surface_products import _product,_sections
from dalton_core.research_source_projection import projection_slice,legacy_projection,publication_source_hash
from dalton_core.research_localization import build_prompt,localization_blockers
from dalton_core.research_output_preparation import chunks,run_chunk,_section_cache_key
from dalton_core.research_publication_limits import current_scope,PublicationLimits,PublicationParked

class ExactProjectionSliceTests(unittest.TestCase):
 def setUp(self):
  self.product=_product('surface_model_notes','company:synthetic','forecast-model:synthetic','a'*64,
   [*_sections('模型说明','Revenue 12 million','Operating expense 4 million','Diluted shares 3 million')])
 def rehash(self, value):
  import hashlib
  value['content_hash']=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='content_hash'},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 def test_whole_verified_slices_keep_source_positions_and_do_not_send_parent_body(self):
  before=copy.deepcopy(self.product); parts=list(chunks(self.product,60))
  self.assertGreater(len(parts),1)
  for start,product in parts:
   self.assertEqual(localization_blockers(product),[])
   self.assertEqual(product['sections'],self.product['sections'][start:start+len(product['sections'])])
   self.assertEqual(publication_source_hash(product),publication_source_hash(self.product))
   prompt=build_prompt(product)
   self.assertNotIn('projection_slice',prompt);self.assertNotIn('source_product',prompt)
   for row in self.product['sections']:
    self.assertEqual(row['body'] in prompt,row in product['sections'])
   self.assertNotIn('projection_slice',legacy_projection(product))
  self.assertEqual(before,self.product)
 def test_duplicate_reverse_outside_boolean_empty_indices_and_nested_slice_refuse(self):
  for indices in ([0,0],[1,0],[3],[True],[],[-1]):
   with self.subTest(indices=indices),self.assertRaises(ValueError):projection_slice(self.product,indices)
  with self.assertRaises(ValueError):projection_slice(projection_slice(self.product,[0]),[0])
 def test_bad_complete_source_hash_or_foreign_section_cannot_be_resealed_as_slice(self):
  broken=copy.deepcopy(self.product);broken['sections'][0]['body']='Foreign source'
  with self.assertRaises(ValueError):projection_slice(broken,[0])
  for mutate in ('section','index','parent','identity','hash'):
   sliced=projection_slice(self.product,[0])
   if mutate=='section':sliced['sections'][0]['body']='Foreign section'
   elif mutate=='index':sliced['projection_slice']['indices']=[1]
   elif mutate=='parent':sliced['projection_slice']['source_product']['sections'][2]['body']='Changed full source'
   elif mutate=='identity':sliced['version_ref']='forecast-model:foreign'
   else:sliced['content_hash']='f'*64
   if mutate!='hash':self.rehash(sliced)
   with self.subTest(mutate=mutate),self.assertRaises(ValueError):legacy_projection(sliced)
 def test_slice_uses_whole_budget_scope_even_without_explicit_outer_override(self):
  source=publication_source_hash(self.product);sliced=projection_slice(self.product,[1])
  with tempfile.TemporaryDirectory() as d:
   path=Path(d);ledger=PublicationLimits(path/'publication-limits.sqlite3')
   for n in range(2):ledger.admit(source,'old'+str(n),'oldstage'+str(n),kind='run')
   def governed(task,**kw):
    self.assertEqual(current_scope()[1],source)
    with self.assertRaises(PublicationParked):ledger.admit(current_scope()[1],'new','newstage',kind='run')
    return 'blocked'
   with patch('dalton_core.research_output_preparation._run_governed_chunk',side_effect=governed):
    self.assertEqual(run_chunk((0,1,sliced),work_dir=path),'blocked')
 def test_original_section_cache_key_is_preserved_and_exact_single_cache_projection_validates(self):
  before=_section_cache_key(self.product,self.product['sections'][1],[])
  sliced=projection_slice(self.product,[1])
  self.assertEqual(before,_section_cache_key(sliced,sliced['sections'][0],[]))
  self.assertEqual(localization_blockers(sliced),[])

 def test_framework_and_debate_valid_slices_pass_and_bad_slice_hash_refuses(self):
  from dalton_core.research_source_projection import PROJECTION_VERSION
  parents=[{'kind':'industry_framework','projection_contract':PROJECTION_VERSION,
          'version_ref':'framework:synthetic','content_hash':'b'*64,
          'sections':[{'title':'产业机制','body':'需求增长影响交付能力，而交付能力决定成本与收入确认节奏。','sources':[], 'numbers':[], 'gaps':[]}]},
          {'kind':'debate_map','projection_contract':PROJECTION_VERSION,
          'version_ref':'debate:synthetic','sections':[{'title':'市场观点','body':'尚未形成判断',
          'position':{'available':False,'lean':None},'sources':[],'gaps':[],
          'localization_role':'declared_absence'}]}]
  for parent in parents:
   with self.subTest(kind=parent['kind']):
    original=copy.deepcopy(parent);sliced=projection_slice(parent,[0])
    self.assertEqual(localization_blockers(sliced),[])
    self.assertEqual(publication_source_hash(sliced),publication_source_hash(parent))
    self.assertEqual(parent,original) # Old whole framework/absence shape and hash stay intact.
    sliced['content_hash']='f'*64
    with self.assertRaisesRegex(ValueError,'slice content hash'):legacy_projection(sliced)
