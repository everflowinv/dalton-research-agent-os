"""Exact formal-authority replay for CompanyDossier unit provenance."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.company_dossier_cli import validate_formal_unit_provenance
from dalton_core.company_dossier import CompanyDossierAuthority, UNITS
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.company_dossier_draft import (
    build_unit_prompt, build_verifier_prompt, draft_hash, parse_unit_output,
)
from dalton_core.store import canonical_json, content_hash
from tests.test_company_dossier import body, drafted
from tests.test_claim_index_entries import LedgerFixture
from tests.test_company_dossier_draft import STRUCTURE, material, reply, one_sentence
from tests.test_dossier_lane import bootstrap_method_authorities, mission_params


class DossierUnitProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.scheduler_path = Path(self.temp.name) / "scheduler.sqlite"
        self.router_path = Path(self.temp.name) / "router.sqlite"
        scheduler = sqlite3.connect(self.scheduler_path)
        scheduler.executescript("""
          CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,work_order_json TEXT,work_order_hash TEXT);
          CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT PRIMARY KEY,work_order_id TEXT,attempt_number INTEGER,result_envelope_json TEXT,result_envelope_hash TEXT,outcome TEXT,content_hash TEXT,created_at TEXT);
        """)
        router = sqlite3.connect(self.router_path)
        router.execute("CREATE TABLE model_route_decisions(decision_id TEXT PRIMARY KEY,work_order_id TEXT,work_order_hash TEXT,outcome TEXT,decision_json TEXT,decision_hash TEXT)")
        self.calls = {}
        self.unit="demand_drivers"
        self.company={"company_ref":"company:acn","ticker":"ACN"}
        self.parse_input={"structure":list(STRUCTURE),"material":list(material()),"prior_body":"",
            "profile":None,"profile_table":"","market_view_available":True,
            "classification":None}
        self.producer_text=reply([one_sentence("causal_chain:0", ["C1"]),
                                  {"slot_id":"causal_chain:1","unknown":"x"}])
        self.block=parse_unit_output(self.producer_text,unit=self.unit,structure=STRUCTURE,
                                     material=material())
        self.producer_prompt=build_unit_prompt(unit=self.unit,structure=STRUCTURE,
            material=material(),company=self.company)
        self.blocks={self.unit:self.block}
        self.verifier_prompt=build_verifier_prompt(self.blocks,company=self.company)
        self.producer_input={"unit":self.unit,"company":self.company,
            "prompt_sha":content_hash({"prompt":self.producer_prompt}),
            "mission":{"ref":"mission:v14","hash":"c"*64},
            "constitution":{"ref":"constitution-version:us-it-services:1","hash":"c"*64},
            "policy":{"ref":"dossier-policy:test:v1",
                      "hash":"ef91d84cfaf1c2c67d2482c4fe97ba8bd4ef373e9db503cc543654b8d3246cf5"},
            "parse_input":self.parse_input}
        self.verified_draft_hash=draft_hash(self.blocks)
        producer_route = self._call(scheduler, router, "producer", [])
        self._call(scheduler, router, "verifier", [producer_route])
        scheduler.commit(); router.commit(); scheduler.close(); router.close()
        self.provenance = {self.unit: {
            "input_fingerprint": content_hash(self.producer_input),
            "producer_input":self.producer_input,"producer_prior_version_ref": None,
            "resolved_classification": None,
            "verified_draft_hash":self.verified_draft_hash,
            "producer": self.calls["producer"], "verifier": self.calls["verifier"],
        }}

    def _install_mission(self, fixture):
        authority=CoverageMissionAuthority(fixture.store)
        state=bootstrap_method_authorities(fixture.store);self.method_state=state
        params=mission_params(state)
        if "dossier" not in params["autonomy"]["may_write"]:
            params["autonomy"]["may_write"].append("dossier")
        record=authority.create_mission(params.pop("mission_ref"),**params)
        self.producer_input["mission"]={"ref":record["id"],"hash":record["content_hash"]}
        self.provenance[self.unit]["input_fingerprint"]=content_hash(self.producer_input)
        scheduler=sqlite3.connect(self.scheduler_path)
        router=sqlite3.connect(self.router_path)
        for name in ("producer","verifier"):
            work_ref=f"work:{name}"
            row=scheduler.execute("SELECT work_order_json FROM scheduler_work_orders "
                                  "WHERE work_order_id=?",(work_ref,)).fetchone()
            work=json.loads(row[0]);work["metadata"].update({
                "mission_version_ref":record["id"],
                "mission_version_hash":record["content_hash"]})
            work_hash=content_hash(work)
            scheduler.execute("UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? "
                              "WHERE work_order_id=?",(canonical_json(work),work_hash,work_ref))
            router.execute("UPDATE model_route_decisions SET work_order_hash=? "
                           "WHERE work_order_id=?",(work_hash,work_ref))
        scheduler.commit();scheduler.close();router.commit();router.close()
        return record

    def _next_mission(self, fixture, prior):
        params=mission_params(self.method_state)
        params.update({"version_id":"mission-version:test:2",
                       "prior_version_ref":prior["id"],
                       "idempotency_key":"mission-test-v2",
                       "objective":params["objective"]+" revised"})
        if "dossier" not in params["autonomy"]["may_write"]:
            params["autonomy"]["may_write"].append("dossier")
        return CoverageMissionAuthority(fixture.store).create_mission(
            params.pop("mission_ref"),**params)

    def _additional_unit_proof(self, *, unit, mission, prior_ref):
        structure=[{"slot_id":unit,"prompt":"Describe it"}]
        rows=list(material())
        rows[0]={**rows[0],"ref":"claim-version:new-unit",
                 "text":"New evidence for the second dossier unit"}
        text=reply([one_sentence(unit,["C1"])])
        block=parse_unit_output(text,unit=unit,structure=structure,material=rows)
        prompt=build_unit_prompt(unit=unit,structure=structure,material=rows,
                                 company=self.company)
        parse_input={"structure":structure,"material":rows,"prior_body":"",
            "profile":None,"profile_table":"","market_view_available":True,
            "classification":None}
        frozen={"unit":unit,"company":self.company,
            "prompt_sha":content_hash({"prompt":prompt}),
            "mission":{"ref":mission["id"],"hash":mission["content_hash"]},
            "constitution":self.producer_input["constitution"],
            "policy":self.producer_input["policy"],"parse_input":parse_input}
        digest=draft_hash({unit:block})
        verifier_prompt=build_verifier_prompt({unit:block},company=self.company)
        scheduler=sqlite3.connect(self.scheduler_path);router=sqlite3.connect(self.router_path)
        original=(self.unit,self.producer_prompt,self.verifier_prompt,self.producer_text,
                  self.verified_draft_hash,self.producer_input)
        self.unit=unit;self.producer_prompt=prompt;self.verifier_prompt=verifier_prompt
        self.producer_text=text;self.verified_draft_hash=digest;self.producer_input=frozen
        producer_route=self._call(scheduler,router,"producer-2",[])
        self._call(scheduler,router,"verifier-2",[producer_route])
        scheduler.commit();router.commit();scheduler.close();router.close()
        calls={role:self.calls[f"{role}-2"] for role in ("producer","verifier")}
        (self.unit,self.producer_prompt,self.verifier_prompt,self.producer_text,
         self.verified_draft_hash,self.producer_input)=original
        return block,{"input_fingerprint":content_hash(frozen),"producer_input":frozen,
            "producer_prior_version_ref":prior_ref,"resolved_classification":None,
            "verified_draft_hash":digest,**calls}

    def _call(self, scheduler, router, name, producer_routes):
        work_ref=f"work:{name}"; route_ref=f"route-decision:{name}"; result_ref=f"result-envelope:{name}"; invocation_ref=f"invocation:{name}"
        from dalton_core.company_dossier_draft import verifier_prompt_contract_fingerprint
        is_producer=name.startswith("producer")
        prompt=self.producer_prompt if is_producer else self.verifier_prompt
        request_id=(content_hash({"unit":self.unit,"company":"company:acn","prompt_sha":content_hash(prompt)})[:32]
                    if is_producer else
                    f"verify-{self.verified_draft_hash[:24]}-{verifier_prompt_contract_fingerprint()[:16]}")
        work={"id":work_ref,"question":prompt,"metadata":{"purpose":"dossier" if is_producer else "dossier_verifier","request_id":request_id,"mission_version_ref":self.producer_input["mission"]["ref"],"mission_version_hash":self.producer_input["mission"]["hash"],"producer_route_decision_refs":producer_routes}}
        work_hash=content_hash(work)
        output=(self.producer_text if is_producer else '{"verdict":"pass","findings":[]}')
        envelope={"id":result_ref,"work_order_ref":work_ref,"invocation_ref":invocation_ref,"status":"succeeded","outputs":{"text":output},"metadata":{"route_decision_ref":route_ref}}
        decision={"id":route_ref}; decision["content_hash"]=content_hash(decision)
        scheduler.execute("INSERT INTO scheduler_work_orders VALUES(?,?,?)",(work_ref,canonical_json(work),work_hash))
        created="2026-09-10T00:00:00+00:00"; envelope_hash=content_hash(envelope)
        receipt_hash=content_hash({"result_envelope_id":result_ref,"work_order_id":work_ref,
            "attempt_number":1,"result_envelope_hash":envelope_hash,"outcome":"succeeded",
            "created_at":created})
        scheduler.execute("INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?,?,?,?,?)",(result_ref,work_ref,1,canonical_json(envelope),envelope_hash,"succeeded",receipt_hash,created))
        router.execute("INSERT INTO model_route_decisions VALUES(?,?,?,?,?,?)",(route_ref,work_ref,work_hash,"selected",canonical_json(decision),decision["content_hash"]))
        self.calls[name]={"work_order_ref":work_ref,"result_envelope_ref":result_ref,"invocation_ref":invocation_ref,"route_decision_ref":route_ref,"request_id":request_id,"prompt_hash":content_hash(prompt)}
        return route_ref

    def test_exact_formal_calls_resolve_and_tampering_refuses(self):
        validate_formal_unit_provenance(self.provenance, mission_ref="mission:v14", current_prior_ref=None, scheduler_db=self.scheduler_path, router_db=self.router_path)
        bad=json.loads(json.dumps(self.provenance));bad[self.unit]["producer"]["invocation_ref"]="invocation:wrong"
        with self.assertRaisesRegex(ValueError,"authority binding drifted"):
            validate_formal_unit_provenance(bad, mission_ref="mission:v14", current_prior_ref=None, scheduler_db=self.scheduler_path, router_db=self.router_path)

    def test_verifier_must_bind_the_exact_producer_route(self):
        connection=sqlite3.connect(self.scheduler_path)
        row=connection.execute("SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id='work:verifier'").fetchone()
        work=json.loads(row[0]);work["metadata"]["producer_route_decision_refs"]=[]
        new_hash=content_hash(work)
        connection.execute("UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=? WHERE work_order_id='work:verifier'",(canonical_json(work),new_hash))
        connection.commit();connection.close()
        router=sqlite3.connect(self.router_path)
        router.execute("UPDATE model_route_decisions SET work_order_hash=? WHERE work_order_id='work:verifier'",(new_hash,))
        router.commit();router.close()
        with self.assertRaisesRegex(ValueError,"did not bind its producer"):
            validate_formal_unit_provenance(self.provenance, mission_ref="mission:v14", current_prior_ref=None, scheduler_db=self.scheduler_path, router_db=self.router_path)

    def test_cross_company_and_cross_unit_producer_proof_is_refused(self):
        wrong_company=json.loads(json.dumps(self.provenance))
        wrong_company[self.unit]["producer_input"]["company"]["company_ref"]="company:other"
        wrong_company[self.unit]["input_fingerprint"]=content_hash(
            wrong_company[self.unit]["producer_input"])
        with self.assertRaisesRegex(ValueError,"company binding drifted"):
            validate_formal_unit_provenance(
                wrong_company, mission_ref="mission:v14", current_prior_ref=None,
                company_ref="company:acn", scheduler_db=self.scheduler_path,
                router_db=self.router_path)

    def test_nested_parse_input_and_governance_are_closed(self):
        fixture=LedgerFixture();self.addCleanup(fixture.close)
        mission=self._install_mission(fixture)
        authority=CompanyDossierAuthority(fixture.store)
        candidate=body(drafted_sections={self.unit:self.block},company_ref="company:acn")
        candidate["bindings"]["mission_version_ref"]=mission["id"]
        candidate["input_fingerprints"]={unit:None for unit in UNITS}
        candidate["unit_provenance"]={unit:None for unit in UNITS}
        item=json.loads(json.dumps(self.provenance[self.unit]))
        item["producer_input"]["parse_input"]["material"][0]["unused"]="x"
        item["input_fingerprint"]=content_hash(item["producer_input"])
        candidate["input_fingerprints"][self.unit]=item["input_fingerprint"]
        candidate["unit_provenance"][self.unit]=item
        with self.assertRaisesRegex(Exception,"not canonical"):
            authority.publish_verified(candidate,scheduler_db=self.scheduler_path,
                                       router_db=self.router_path)

        item=json.loads(json.dumps(self.provenance[self.unit]))
        item["producer_input"]["policy"]["hash"]="f"*64
        item["input_fingerprint"]=content_hash(item["producer_input"])
        candidate["input_fingerprints"][self.unit]=item["input_fingerprint"]
        candidate["unit_provenance"][self.unit]=item
        with self.assertRaisesRegex(ValueError,"governance binding drifted"):
            authority.publish_verified(candidate,scheduler_db=self.scheduler_path,
                                       router_db=self.router_path)
        wrong_unit={"business_model":json.loads(json.dumps(self.provenance[self.unit]))}
        wrong_unit["business_model"]["producer_input"]["unit"]="business_model"
        wrong_unit["business_model"]["input_fingerprint"]=content_hash(
            wrong_unit["business_model"]["producer_input"])
        with self.assertRaisesRegex(ValueError,"producer input binding drifted"):
            validate_formal_unit_provenance(
                wrong_unit, mission_ref=mission["id"], current_prior_ref=None,
                company_ref="company:acn", scheduler_db=self.scheduler_path,
                router_db=self.router_path)

    def test_verified_publish_round_trips_closed_v3_and_plain_publish_refuses(self):
        fixture=LedgerFixture();self.addCleanup(fixture.close)
        mission=self._install_mission(fixture)
        authority=CompanyDossierAuthority(fixture.store)
        candidate=body(drafted_sections={self.unit: self.block})
        candidate["company_ref"]="company:acn"
        candidate["bindings"]["mission_version_ref"]=mission["id"]
        candidate["input_fingerprints"]={unit:None for unit in UNITS}
        candidate["input_fingerprints"][self.unit]=content_hash(self.producer_input)
        candidate["unit_provenance"]={unit:None for unit in UNITS}
        candidate["unit_provenance"][self.unit]=self.provenance[self.unit]
        with self.assertRaisesRegex(Exception,"publish_verified"):
            authority.publish(candidate)
        published=authority.publish_verified(candidate,scheduler_db=self.scheduler_path,router_db=self.router_path)
        self.assertEqual(published["schema_version"],"0.3")
        self.assertEqual(authority.dossier(published["id"])["unit_provenance"],candidate["unit_provenance"])

    def test_recomputed_record_hash_cannot_publish_a_body_the_producer_did_not_make(self):
        fixture=LedgerFixture();self.addCleanup(fixture.close)
        mission=self._install_mission(fixture)
        authority=CompanyDossierAuthority(fixture.store)
        altered=json.loads(json.dumps(self.block))
        altered["slots"][0]["sentences"][0]["text"]="A different supported-looking sentence"
        candidate=body(drafted_sections={self.unit:altered})
        candidate["company_ref"]="company:acn"
        candidate["bindings"]["mission_version_ref"]=mission["id"]
        candidate["input_fingerprints"]={unit:None for unit in UNITS}
        candidate["input_fingerprints"][self.unit]=content_hash(self.producer_input)
        candidate["unit_provenance"]={unit:None for unit in UNITS}
        candidate["unit_provenance"][self.unit]=self.provenance[self.unit]
        with self.assertRaisesRegex(ValueError,"verified draft binding drifted"):
            authority.publish_verified(candidate,scheduler_db=self.scheduler_path,
                                       router_db=self.router_path)

    def test_legacy_drafted_unit_stays_null_when_one_new_unit_gains_proof(self):
        fixture=LedgerFixture();self.addCleanup(fixture.close)
        mission=self._install_mission(fixture)
        authority=CompanyDossierAuthority(fixture.store)
        legacy_block=drafted("business_model","claim-version:legacy")
        prior=authority.publish(body(drafted_sections={"business_model":legacy_block},
                                     company_ref="company:acn"))
        candidate=body(drafted_sections={"business_model":legacy_block,self.unit:self.block},
                       company_ref="company:acn",prior_ref=prior["id"])
        candidate["bindings"]["mission_version_ref"]=mission["id"]
        candidate["input_fingerprints"]={unit:None for unit in UNITS}
        candidate["input_fingerprints"][self.unit]=content_hash(self.producer_input)
        candidate["unit_provenance"]={unit:None for unit in UNITS}
        current=json.loads(json.dumps(self.provenance[self.unit]))
        current["producer_prior_version_ref"]=prior["id"]
        candidate["unit_provenance"][self.unit]=current
        published=authority.publish_verified(candidate,scheduler_db=self.scheduler_path,
                                             router_db=self.router_path)
        self.assertIsNone(published["unit_provenance"]["business_model"])
        self.assertEqual(published["sections"][0],legacy_block)

    def test_old_mission_proof_is_carried_while_a_new_unit_binds_the_new_mission(self):
        fixture=LedgerFixture();self.addCleanup(fixture.close)
        mission=self._install_mission(fixture)
        authority=CompanyDossierAuthority(fixture.store)
        first=body(drafted_sections={self.unit:self.block},company_ref="company:acn")
        first["bindings"]["mission_version_ref"]=mission["id"]
        first["input_fingerprints"]={unit:None for unit in UNITS}
        first["input_fingerprints"][self.unit]=content_hash(self.producer_input)
        first["unit_provenance"]={unit:None for unit in UNITS}
        first["unit_provenance"][self.unit]=self.provenance[self.unit]
        published=authority.publish_verified(first,scheduler_db=self.scheduler_path,
                                             router_db=self.router_path)
        second_mission=self._next_mission(fixture,mission)
        new_block,new_proof=self._additional_unit_proof(
            unit="business_model",mission=second_mission,prior_ref=published["id"])
        second=body(drafted_sections={self.unit:self.block,"business_model":new_block},company_ref="company:acn",
                    prior_ref=published["id"])
        second["bindings"]["mission_version_ref"]=second_mission["id"]
        second["input_fingerprints"]=published["input_fingerprints"]
        second["unit_provenance"]=published["unit_provenance"]
        second["input_fingerprints"]["business_model"]=new_proof["input_fingerprint"]
        second["unit_provenance"]["business_model"]=new_proof
        self.assertIsNotNone(second["unit_provenance"]["business_model"])
        carried=authority.publish_verified(second,scheduler_db=self.scheduler_path,
                                           router_db=self.router_path)
        self.assertEqual(carried["unit_provenance"][self.unit],
                         published["unit_provenance"][self.unit])
        self.assertEqual(carried["unit_provenance"][self.unit]["producer_input"]
                         ["mission"]["ref"],mission["id"])
        self.assertEqual(carried["unit_provenance"]["business_model"]["producer_input"]
                         ["mission"]["ref"],second_mission["id"])
        forged=json.loads(json.dumps(second))
        forged["unit_provenance"]["business_model"]["producer_prior_version_ref"]=None
        with self.assertRaisesRegex(Exception,"exact predecessor"):
            authority.publish_verified(forged,scheduler_db=self.scheduler_path,
                                       router_db=self.router_path)


if __name__ == "__main__": unittest.main()
