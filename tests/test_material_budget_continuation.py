from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core.needs_human_ack import acknowledge_entry, condition_hash, hides
from tests.test_material_acquisition import Fixture, FakeRung, NOW, later


class MaterialBudgetContinuationTests(Fixture):
    def approve(self, card_ref, sequence=1):
        self.core_execute(
            "INSERT INTO needs_human_acknowledgements(ack_id,sequence,card_ref,kind,choice,created_at) "
            "VALUES (?, ?, ?, 'material_budget_exceeded', 'approve_spend', ?)",
            (f"ack:{sequence}", sequence, card_ref, NOW))

    def test_prior_approval_survives_and_only_missing_calls_are_requested(self):
        req = self.request(need={"kind": "answer", "question": "Issuer filing question"},
                           source_priority=["web"], budget={"max_calls": 8, "max_usd": "0.50"})
        ma.ensure_schema(self.state)
        self.approve(ma.budget_card_ref("request", req["id"], 500000, 3))
        ma._record_spend(self.state, req["id"], "web", 11, 105000, now=NOW)
        web = FakeRung("web", [ma.RungResult("accepted", calls=3, cost_micros=105000)], calls=3)
        before = ma.serve(self.state, req["id"], rungs={"web": web}, now=NOW)
        self.assertEqual(before["outcome"], "budget_blocked")
        self.assertEqual(web.runs, 0)
        card = ma.owner_door_cards(self.state, now=NOW)[0]
        self.assertEqual((card["detail"]["approve_usd"], card["detail"]["approve_calls"]), ("0.00", 3))
        self.assertIn("此前已批准追加 0.50 美元、3 次", card["why"])
        self.assertIn("0.895", card["why"])
        self.assertEqual(acknowledge_entry(card)["choices"][0]["label"], "批准追加检索次数")
        self.assertEqual(ma.request_caps(self.state, req), {"usd_micros": 1000000, "calls": 11})
        self.approve(card["ref"], 2)
        # Even before the next worker tick, the old refusal is no longer a card.
        self.assertFalse(ma.owner_door_cards(self.state, now=NOW))
        self.assertEqual(ma.request_caps(self.state, req), {"usd_micros": 1000000, "calls": 14})
        self.assertEqual(ma.serve(self.state, req["id"], rungs={"web": web}, now=later(hours=1))["outcome"], "found")
        self.assertEqual(ma.request_spend(self.state, req["id"]), {"calls": 14, "cost_micros": 210000})

    def test_first_proposal_covers_remaining_sources_without_extra_money(self):
        req = self.request(need={"kind": "answer", "question": "Issuer filing question"},
                           source_priority=["alphaengine", "guidepoint", "web"],
                           budget={"max_calls": 8, "max_usd": "0.50"})
        ma.ensure_schema(self.state)
        ma._record_spend(self.state, req["id"], "web", 7, 105000, now=NOW)
        rungs = {name: FakeRung(name, [ma.RungResult("empty", calls=calls,
                         cost_micros=105000 if name == "web" else 0)], calls=calls)
                 for name, calls in [("alphaengine", 3), ("guidepoint", 1), ("web", 3)]}
        blocked = ma.serve(self.state, req["id"], rungs=rungs, now=NOW)
        self.assertEqual(blocked["budget"]["approve_calls"], 6)
        self.assertEqual(blocked["budget"]["approve_usd_micros"], 0)
        self.assertEqual(sum(r.runs for r in rungs.values()), 0)
        card = ma.owner_door_cards(self.state, now=NOW)[0]
        self.approve(card["ref"])
        result = ma.serve(self.state, req["id"], rungs=rungs, now=later(hours=1))
        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual([r.runs for r in rungs.values()], [1, 1, 1])
        self.assertEqual(ma.request_caps(self.state, req), {"usd_micros": 500000, "calls": 14})
        self.assertFalse([c for c in ma.owner_door_cards(self.state, now=NOW) if c["kind"] == "material_budget_exceeded"])

    def test_new_round_shortfall_is_distinct_from_the_approved_refusal(self):
        req = self.request(source_priority=["web"], budget={"max_calls": 0, "max_usd": "1.00"})
        web = FakeRung("web", [ma.RungResult("empty", calls=3, cost_micros=105000)], calls=3)
        ma.serve(self.state, req["id"], rungs={"web": web}, now=NOW)
        card = ma.owner_door_cards(self.state, now=NOW)[0]
        self.approve(card["ref"])
        ma.serve(self.state, req["id"], rungs={"web": web}, now=later(hours=1))
        mr.reopen(self.state, req["id"], reason="new filing", now=later(hours=2))
        ma.serve(self.state, req["id"], rungs={"web": web}, now=later(hours=2))
        new = next(c for c in ma.owner_door_cards(self.state, now=later(hours=2)) if c["kind"] == "material_budget_exceeded")
        self.assertNotEqual(card["ref"], new["ref"])
        self.assertFalse(hides(new, {"choice": "approve_spend", "condition_hash": condition_hash(card)}))

    def test_fractional_cent_shortfall_is_rounded_up_and_old_refs_parse(self):
        req = self.request(source_priority=["web"], budget={"max_calls": 10, "max_usd": "0.10"})
        web = FakeRung("web", [], calls=3)
        ma.serve(self.state, req["id"], rungs={"web": web}, now=NOW)
        card = ma.owner_door_cards(self.state, now=NOW)[0]
        self.assertEqual((card["detail"]["approve_usd"], card["detail"]["approve_calls"]), ("0.01", 0))
        self.approve(card["ref"])
        self.assertIsNone(ma._budget_check(self.state, req, 3, 105000, now=NOW))
        self.assertEqual(ma.parse_budget_card_ref("material-budget:request:x:0.50:3")["usd_micros"], 500000)
