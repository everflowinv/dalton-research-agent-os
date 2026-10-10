"""One purpose editor, live confirmation and honest model-save feedback."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests import test_cockpit_v2_admin as fixture

JS = fixture.ADMIN_JS
RUN = r'''
const originalFetch = global.fetch;
global.fetch = async (path, opts={}) => {
 if (opts.method === "POST" && path === "/v2/data/admin/action") {
  const req=JSON.parse(opts.body); posts.push({body:req});
  const resultStatus=responses.testStatus || "applied";
  if(resultStatus==="applied" && req.payload.purpose && req.payload.chain){
   const p=responses["/v1/cockpit/models"].body.purposes.find(p=>p.purpose===req.payload.purpose);
   p.chain=req.payload.chain.map(model=>({model,display_name:model}));
  }
  return {ok:true,status:200,json:async()=>({status:"done",result:{status:resultStatus},receipt:{action:req.action,action_label:"调整模型顺序",reason:req.reason,at:new Date().toISOString(),status:resultStatus,environments:responses.testEnvironments || []}})};
 }
 return originalFetch(path,opts);
};
require(process.argv[4]);
const byText=(node,text)=>node.querySelectorAll("button").find(b=>b.textContent===text);
const ai=()=>root.querySelector("#adm-panel-ai");
const editor=()=>ai().querySelector("[data-role=purpose-editor]");
const selectPurpose=()=>editor().querySelectorAll("select").find(s=>s.attrs["aria-label"]==="选择要单独调整的环节");
async function confirmSave(){await settle(); const dlg=root.querySelector("[data-role=confirm]");if(!dlg)throw Error("confirmation missing");const input=dlg.querySelector("input");if(input){input.value="项目";input.dispatch("input")};dlg.querySelector("[data-role=confirm-ok]").click();await settle();}
(async()=>{await settle(); const out={};
BODY
console.log(JSON.stringify(out));})().catch(e=>{console.error(e.stack||e);process.exit(1)});
'''

def responses():
    choices=[{'model':'writer-a','display_name':'起草甲','verifier_eligible':False},
             {'model':'writer-b','display_name':'起草乙','verifier_eligible':False},
             {'model':'verify-a','display_name':'核验甲','verifier_eligible':True}]
    def purpose(key,label,tier,model,**extra):
        return {'purpose':key,'label':label,'tier':tier,'tier_label':tier,'editable':True,
                'thinking_level':'low' if tier=='verifier' else 'medium',
                'thinking_level_choices':['low'] if tier=='verifier' else ['low','medium'],
                'mode':'explicit','mode_label':'指定顺序',
                'chain':[{'model':model,'display_name':model}],**extra}
    models={'available':True,'choices':choices,'purposes':[
        purpose('draft','起草','reasoning','writer-a'),
        purpose('draft_verifier','核验','verifier','verify-a'),
        purpose('old','已退役步骤','reasoning','writer-a',retired_by='ADR-0010')],
        'tier_cards':[{'tier':'reasoning','label':'起草档位','purposes':['draft'],'chain':[
            {'model':'writer-a','display_name':'起草甲'},{'model':'writer-b','display_name':'起草乙'}]}]}
    verifier={'available':True,'environments':[{'name':'项目','available':True,'conflicts':1}],
              'rows':[{'bound_anywhere':True,'draft_purpose':'draft','draft_label':'起草',
                'draft_tier_label':'推理','in_conflict':True,
                'pairs':[{'verifier_purpose':'draft_verifier','verifier_label':'核验','environments':[
                    {'name':'项目','draft_chain':['起草甲'],'verifier_chain':['核验甲'],
                     'status':'independent','status_label':'独立'}]}],
                'editor':{'recommended':{'status':'proposed','chain':['writer-b'],'names':['起草乙'],'note':'不同家族'},'default_reason':'调整核验独立性'}}]}
    return {'/v1/cockpit/models':{'status':200,'body':models},
            '/v1/cockpit/verifier_independence':{'status':200,'body':verifier},
            '/v2/data/admin/summary':{'status':200,'body':{'environment':{'name':'项目'},'writer':{},'levers':{}}},
            '/v2/data/admin/actions':{'status':200,'body':{'csrf_token':'test'}}}


class ModelSettingsBehaviorTests(unittest.TestCase):
    def run_js(self,body,*,data=None):
        node=fixture._node()
        if not node:self.skipTest('node unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'responses.json').write_text(json.dumps(data or responses(),ensure_ascii=False))
            (root/'test.js').write_text(fixture.NODE_DOM+RUN.replace('BODY',body))
            result=subprocess.run([node,str(root/'test.js'),str(root/'responses.json'),'standalone',str(JS)],text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_one_editor_and_both_independence_links_keep_checks_visible(self):
        out=self.run_js(r'''
out.editors=ai().querySelectorAll("[data-role=purpose-editor]").length;
out.checks=ai().textContent.includes("起草顺序")&&ai().textContent.includes("核验顺序");
out.retiredOptions=selectPurpose().querySelectorAll("option").filter(o=>o.value==="old").length;
out.duplicateSaves=ai().querySelectorAll("button").filter(b=>b.textContent==="保存起草顺序（同步到全部研究项目）").length;
byText(ai(),'修改「起草」的顺序').click();out.draft=selectPurpose().value;
byText(ai(),"修改核验顺序：核验").click();out.verifier=selectPurpose().value;
const add=editor().querySelectorAll("select").find(s=>s.attrs["aria-label"].endsWith("添加备用服务"));
out.disabled=add.querySelectorAll("option").filter(o=>o.disabled).map(o=>o.value);
out.thinking=editor().textContent.includes("独立核验固定使用低强度");
out.posts=posts.length;
''')
        self.assertEqual(out['editors'],1);self.assertTrue(out['checks']);self.assertEqual(out['retiredOptions'],0)
        self.assertEqual(out['duplicateSaves'],0);self.assertEqual(out['draft'],'draft');self.assertEqual(out['verifier'],'draft_verifier')
        self.assertEqual(out['disabled'],['writer-a','writer-b']);self.assertTrue(out['thinking']);self.assertEqual(out['posts'],0)

    def test_recommendation_is_prefill_only_and_save_keeps_selection_receipt(self):
        out=self.run_js(r'''
byText(ai(),"在统一编辑入口填入推荐顺序（不会保存）").click();out.before=posts.length;
out.prefilled=editor().querySelector("ol").textContent.includes("起草乙");
const save=byText(editor(),"为「起草」单独指定顺序");save.click();await settle();
out.confirm=root.querySelector("[data-role=confirm]").textContent;await confirmSave();
out.payload=posts[0].body.payload;out.selected=selectPurpose().value;
out.savedChain=editor().querySelector("ol").textContent;out.receipt=ai().textContent.includes("配置已保存");
''')
        self.assertEqual(out['before'],0);self.assertTrue(out['prefilled']);self.assertIn('起草乙',out['confirm'])
        self.assertEqual(out['payload'],{'purpose':'draft','mode':'explicit','chain':['writer-b']})
        self.assertEqual(out['selected'],'draft');self.assertIn('起草乙',out['savedChain']);self.assertTrue(out['receipt'])

    def test_tier_confirmation_uses_current_reordered_chain(self):
        out=self.run_js(r'''
const row=ai().querySelectorAll("[data-tier=reasoning]")[0];
row.querySelectorAll("button").find(b=>b.attrs["aria-label"]==="上移 起草乙").click();
const reason=row.querySelectorAll("textarea").find(t=>t.attrs["aria-label"].startsWith("保存「"));reason.value="调整顺序";
row.querySelectorAll("button").find(b=>b.textContent.startsWith("保存「")).click();await settle();
out.confirm=root.querySelector("[data-role=confirm]").textContent;await confirmSave();out.chain=posts[0].body.payload.chain;
''')
        self.assertIn('起草乙 → 起草甲',out['confirm']);self.assertEqual(out['chain'],['writer-b','writer-a'])

    def test_confirmed_chain_is_same_frozen_payload_even_if_editor_changes(self):
        out=self.run_js(r'''
byText(ai(),'修改「起草」的顺序').click();
editor().querySelectorAll("textarea").find(t=>t.attrs["aria-label"].startsWith("为「")).value="确认顺序";
byText(editor(),"为「起草」单独指定顺序").click();await settle();
out.confirm=root.querySelector("[data-role=confirm]").textContent;
const add=editor().querySelectorAll("select").find(s=>s.attrs["aria-label"].endsWith("添加备用服务"));
add.value="writer-b";byText(editor(),"添加备用服务").click();
await confirmSave();out.chain=posts[0].body.payload.chain;
''')
        self.assertIn('起草甲',out['confirm']);self.assertNotIn('起草乙',out['confirm'])
        self.assertEqual(out['chain'],['writer-a'])

    def test_independence_jump_expands_before_focus_and_uneditable_is_explained(self):
        data=responses();rows=data['/v1/cockpit/verifier_independence']['body']['rows']
        rows.append({**rows[0],'draft_purpose':'other_env_only','draft_label':'其它项目步骤','pairs':[]})
        out=self.run_js(r'''
let focusClosed=0,focused=0;
El.prototype.focus=function(){if(this.attrs["aria-label"]!=="选择要单独调整的环节")return;focused++;for(let n=this.parentNode;n;n=n.parentNode){if(n.tagName==="DETAILS"&&!n.open&&n.attrs.open===undefined)focusClosed++;}};
byText(ai(),'修改「起草」的顺序').click();
out.focusClosed=focusClosed;out.focused=focused;
const other=ai().querySelector("[data-purpose=other_env_only]");
out.disabled=byText(other,'修改「其它项目步骤」的顺序').disabled;
out.explained=other.textContent.includes("当前研究项目不能单独调整");out.posts=posts.length;
''',data=data)
        self.assertEqual(out['focusClosed'],0);self.assertEqual(out['focused'],1)
        self.assertTrue(out['disabled']);self.assertTrue(out['explained']);self.assertEqual(out['posts'],0)

    def test_same_name_choices_use_proved_presets_and_canonical_route(self):
        data=responses();choices=data['/v1/cockpit/models']['body']['choices']
        for c,level in zip(choices[:2],('low','high')):
            c.update(display_name='同名模型',broker_thinking_preset=level,broker_model_ref='gateway/same')
        out=self.run_js(r'''
out.options=editor().querySelectorAll("select").find(s=>s.attrs["aria-label"].endsWith("添加备用服务")).querySelectorAll("option").map(o=>o.textContent);
out.chain=editor().querySelector("ol").textContent;
''',data=data)
        self.assertTrue(any('网关预设：低强度（环节设置优先）' in o for o in out['options']))
        self.assertTrue(any('网关预设：高强度（环节设置优先）' in o for o in out['options']))
        self.assertTrue(any('接入通道：gateway/same' in o for o in out['options']))
        self.assertIn('网关预设：低强度',out['chain'])

    def test_same_route_same_preset_still_uses_exact_configuration_identity(self):
        data=responses();choices=data['/v1/cockpit/models']['body']['choices']
        for c,digest in zip(choices[:2],('a'*64,'b'*64)):
            c.update(display_name='同名模型',broker_thinking_preset='high',broker_model_ref='gateway/same',profile_hash=digest)
        out=self.run_js(r'''
out.options=editor().querySelectorAll("select").find(s=>s.attrs["aria-label"].endsWith("添加备用服务")).querySelectorAll("option").map(o=>o.textContent);
''',data=data)
        selected=[o for o in out['options'] if '同名模型' in o]
        self.assertEqual(len(set(selected)),2)
        self.assertTrue(all('网关预设：高强度' in o and '配置标识：' in o for o in selected))

    def test_missing_preset_does_not_guess_thinking_from_profile_id(self):
        data=responses();choices=data['/v1/cockpit/models']['body']['choices']
        for c,digest in zip(choices[:2],('a'*64,'b'*64)):
            c.update(display_name='同名模型',profile_hash=digest)
        choices[0]['model']='profile-name-high'
        out=self.run_js(r'''
out.options=editor().querySelectorAll("select").find(s=>s.attrs["aria-label"].endsWith("添加备用服务")).querySelectorAll("option").map(o=>o.textContent);
''',data=data)
        selected=[o for o in out['options'] if '同名模型' in o]
        self.assertEqual(len(set(selected)),2)
        self.assertTrue(all('配置标识：' in o for o in selected))
        self.assertTrue(all('网关预设' not in o for o in selected))

    def test_config_problem_is_not_assumed_same_family_and_length_scope_is_local(self):
        data=responses();draft=data['/v1/cockpit/models']['body']['purposes'][0]
        draft['call_budget']={'editable':True,'effective':{'max_output_tokens':4096,'max_input_tokens':24000,'timeout_seconds':120},'config_hash':'a'*64}
        out=self.run_js(r'''
out.issue=ai().querySelector("[data-role=issues]").textContent;
const row=ai().querySelector("[data-purpose=draft]");out.title=row.querySelector("h3").textContent;
byText(ai(),'修改「起草」的顺序').click();out.editor=editor().textContent;
''',data=data)
        self.assertIn('核验配置有问题',out['title']);self.assertNotIn('同家族',out['title'])
        self.assertNotIn('属于同一家族',out['issue']);self.assertIn('仅当前研究项目',out['editor'])

    def test_tier_default_lists_explicit_stages_and_keeps_stage_reset_control(self):
        out=self.run_js(r'''
out.text=ai().textContent;
byText(ai(),'修改「起草」的顺序').click();out.reset=!!byText(editor(),"使用档位默认顺序");
''')
        self.assertIn('单独指定、不随档位默认更新：起草',out['text'])
        self.assertIn('单独指定的环节保留原顺序',out['text']);self.assertTrue(out['reset'])

    def test_unavailable_independence_is_not_painted_as_independent(self):
        data=responses();row=data['/v1/cockpit/verifier_independence']['body']['rows'][0]
        row['in_conflict']=False;row['pairs'][0]['environments'][0].update(status='unavailable',status_label='无法核对')
        out=self.run_js(r'''
const row=ai().querySelector("[data-purpose=draft]");out.title=row.querySelector("h3").textContent;out.text=row.textContent;
''',data=data)
        self.assertIn('待核对',out['title']);self.assertNotIn('） 独立',out['title']);self.assertIn('无法核对',out['text'])

    def test_unknown_model_save_status_is_not_claimed_saved(self):
        data=responses();data['testStatus']='done'
        out=self.run_js(r'''
byText(ai(),'修改「起草」的顺序').click();editor().querySelectorAll("textarea").find(t=>t.attrs["aria-label"].startsWith("为「")).value="修改";
byText(editor(),"为「起草」单独指定顺序").click();await confirmSave();out.text=ai().textContent;
''',data=data)
        self.assertIn('保存结果待确认',out['text']);self.assertNotIn('配置已保存',out['text'])

    def test_partial_sync_and_refusal_are_not_reported_as_completed(self):
        for status,envs,expected in [('applied',[{'name':'另一项目','result':'failed','note':'未同步'}],'部分研究项目未同步'),('refused:authority',[],'没有生效')]:
            with self.subTest(status=status):
                data=responses();data['testStatus']=status;data['testEnvironments']=envs
                out=self.run_js(r'''
byText(ai(),'修改「起草」的顺序').click();const e=editor();e.querySelectorAll("textarea").find(t=>t.attrs["aria-label"].startsWith("为「")).value="修改";
byText(e,"为「起草」单独指定顺序").click();await confirmSave();out.text=ai().textContent;
''',data=data)
                self.assertIn(expected,out['text']);self.assertNotIn('配置已保存',out['text'])
