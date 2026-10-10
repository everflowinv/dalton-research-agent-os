"""Deterministic answer gates, bound to the exact question and formal claims."""
from __future__ import annotations
import json
import re
import sqlite3
from .research_question_identity import canonical_question_key, normalized, reporting_scope
from .store import canonical_json, content_hash

# Generic instruction verbs do not establish substantive criteria.
_STOP = set("claimversions recorded supported exhibit return official exact same comparison bounded public a an the and or of to from with in for at by is are be must should provide extract identify explain answer report source evidence claim verified least one two current question sufficient enough supported determine show whether what how why please include cite based document original statement criterion criteria supporting formal data".split())

def answer_acceptance(cursor, question, bindings):
    key = canonical_question_key(question["company_ref"], question["question"],
                                 question["answer_criteria"], question["source_refs"])
    if question.get("question_ref") and cursor.execute("SELECT 1 FROM sqlite_master WHERE name='backlog_questions'").fetchone():
        from .research_question_backlog import read_exact_backlog_question
        identity = read_exact_backlog_question(cursor, question["question_ref"])["identity"]
        if identity.get("canonical_key"):
            key = identity["canonical_key"]
    criteria = normalized(question["answer_criteria"])
    numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?", criteria))
    words = {w for w in re.findall(r"[a-z][a-z_]{3,}", criteria) if w not in _STOP}
    chinese_terms = {term for term in ("营业收入", "收入确认", "毛利率", "自由现金流", "资本开支", "净利润", "供应链", "产能", "订单", "需求强度", "供给约束", "竞争格局") if term in criteria}
    from .research_question_identity import _METRICS
    criteria_metrics = {metric for metric,patterns in _METRICS.items() if any(re.search(p, criteria) for p in patterns)}
    filing_list_required = bool(re.search(r"filing list|文件清单|申报清单", criteria))
    words -= {"annual", "quarter", "quarterly", "filing", "reported", "revenue", "sales", "growth"}
    claims = []
    doc_proofs = []
    for binding in bindings:
        row = cursor.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (binding["claim_version_ref"],)).fetchone()
        claim = json.loads(row[0])
        if claim.get("subject_ref") != question["company_ref"]:
            raise ValueError("answer issuer does not match the exact question")
        claims.append(claim)
    if any("filing" in str(c.get("metric_or_aspect", "")) and "count" in str(c.get("metric_or_aspect", "")) for c in claims):
        words.discard("list")
    text = normalized(" ".join(str(c.get("normalized_statement", "")) + " " + str(c.get("metric_or_aspect", "")) for c in claims)).replace("_", " ").replace("-", " ")
    # All observable criteria anchors must be present. No model assertion can
    # replace the formal text/value or exact source metadata.
    if not words and not numbers and not chinese_terms and not criteria_metrics and not filing_list_required:
        raise ValueError("answer criteria have no independently checkable content anchor")
    if any(term not in text for term in chinese_terms):
        raise ValueError("formal answer does not cover the Chinese answer criteria")
    if any(not any(re.search(p, text) for p in _METRICS[metric]) for metric in criteria_metrics):
        raise ValueError("formal answer does not cover the criterion metric")
    if filing_list_required and not any("filing" in str(c.get("metric_or_aspect", "")) and "count" in str(c.get("metric_or_aspect", "")) for c in claims):
        raise ValueError("formal answer does not bind a verified filing list/count")
    if words and not words.issubset(set(re.findall(r"[a-z][a-z_]{3,}", text))):
        raise ValueError("formal answer does not cover the stated answer criteria")
    answer_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?", text + " " + " ".join(str(c.get("value")) for c in claims)))
    period_numbers = set(re.findall(r"\d+", key["period"])) if key["period"] != "unspecified" else set()
    if numbers - period_numbers - answer_numbers:
        raise ValueError("formal answer omits numeric answer criteria")
    if key["metric"] != "unclassified":
        required = set(key["metric"].split(",")) - {"growth", "guidance", "forecast", "segment", "增速", "指引", "预期", "分部"}
        # Use the shared alias vocabulary, independent of period discovery.
        from .research_question_identity import _METRICS
        if any(not any(re.search(p, text) for p in _METRICS.get(metric, (re.escape(metric),))) for metric in required):
            raise ValueError("formal answer metric does not match the question")
    scope_text = reporting_scope(question["question"] + " " + question["answer_criteria"])
    annual_text = re.sub(r"q[1-4][ -]*(?:fy|fiscal)[ -]?(?:19|20)\d{2}|(?:fy|fiscal(?: year)?)[ -]?(?:19|20)\d{2}[ /-]*q[1-4]", " ", scope_text)
    annual_years = set(re.findall(r"(?:fy|fiscal year|年度|全年)[ -]?((?:19|20)\d{2})|((?:19|20)\d{2})[ 年]*(?:财年|年度|全年|年报)", annual_text))
    annual_years = {a or b for a,b in annual_years}
    annual_years |= set(re.findall(r"(?:^|,)fy((?:19|20)\d{2})(?:,|$)", key["period"]))
    fiscal_quarter = bool(re.search(r"fiscal|财季|财年|q[1-4][ -]*fy", scope_text) or re.search(r"fy(?:19|20)\d{2}q[1-4]", key["period"]))
    periods = key["period"].split(",") if key["period"] != "unspecified" else []
    periods += [year for year in sorted(annual_years) if year not in periods]
    matched_periods = {}
    for period in periods:
        target_year = re.search(r"(?:19|20)\d{2}", period).group(0)
        target_quarter = re.search(r"q([1-4])", period)
        def matches(c):
            value = c.get("period")
            if isinstance(value, str):
                raw = normalized(value).replace(" ", "")
                if target_quarter:
                    # Bare/calendar Q does not prove an explicitly fiscal Q.
                    return bool(re.fullmatch(r"(?:fy|fiscal)?" + target_year + r"[-/]?q" + target_quarter.group(1), raw)) and (not fiscal_quarter or raw.startswith(("fy", "fiscal")))
                if target_year in annual_years:
                    return raw in {target_year, "fy"+target_year, "fiscalyear"+target_year}
                return bool(re.fullmatch(r"(?:fy)?" + target_year + r"(?:q[1-4])?", raw))
            if not isinstance(value, dict): return False
            if target_quarter:
                # Dates alone never identify fiscal quarter numbering.
                label = normalized(str(value.get("label", ""))).replace(" ", "")
                return label in {"fy"+target_year+"q"+target_quarter.group(1), "fiscal"+target_year+"q"+target_quarter.group(1)} and value.get("kind") == "fiscal_quarter"
            if target_year in annual_years:
                return (value.get("kind") == "fiscal_year" and normalized(str(value.get("label", ""))).replace(" ", "") == "fy"+target_year)
            return all(str(value.get(k, "")).startswith(target_year+"-") for k in ("start", "end"))
        accepted = [c for c in claims if matches(c)]
        if not accepted:
            raise ValueError("formal answer lacks exact annual/quarter reporting-period authority")
        matched_periods[period] = {c["id"] for c in accepted}
    if "sec exhibit" in criteria:
        for binding in bindings:
            verified = False
            for evidence_row in cursor.execute("SELECT e.evidence_json,e.content_hash FROM evidence_relations r JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id WHERE r.claim_version_id=? AND r.relation='supports'", (binding["claim_version_ref"],)).fetchall():
                evidence = json.loads(evidence_row[0])
                if (canonical_json(evidence) != evidence_row[0] or evidence.get("content_hash") != evidence_row[1]
                        or content_hash({k:v for k,v in evidence.items() if k != "content_hash"}) != evidence_row[1]):
                    raise ValueError("answer provenance evidence drifted")
                if evidence.get("source_type") == "sec-filing-exhibit": verified = True
            if not verified:
                raise ValueError("answer lacks the recorded SEC exhibit required by its criteria")
    if key["doc_kinds"]:
        # Filing type comes only from recorded SEC metadata, never a guessed
        # filename or the question's own words.
        evidence_accessions = {}
        referenced_sources = []
        for binding in bindings:
            for evidence_row in cursor.execute("SELECT e.evidence_json,e.content_hash FROM evidence_relations r JOIN evidence_versions e ON e.evidence_version_id=r.evidence_version_id WHERE r.claim_version_id=? AND r.relation='supports'", (binding["claim_version_ref"],)).fetchall():
                evidence = json.loads(evidence_row[0])
                if (canonical_json(evidence) != evidence_row[0] or evidence.get("content_hash") != evidence_row[1]
                        or evidence_row[1] != content_hash({k:v for k,v in evidence.items() if k != "content_hash"})):
                    raise ValueError("answer source evidence hash drifted")
                # The company-facts producer already verifies the selected
                # datum's SEC form and same-accession pair under a closed,
                # versioned rule. Re-read that exact policy receipt and its
                # source envelope instead of treating query words as metadata.
                source_ref = evidence.get("source_envelope_ref")
                if source_ref and cursor.execute("SELECT 1 FROM sqlite_master WHERE name='connector_source_envelopes'").fetchone():
                    source_row = cursor.execute("SELECT record_json,content_hash FROM connector_source_envelopes WHERE source_envelope_id=?", (source_ref,)).fetchone()
                    if source_row:
                        source = json.loads(source_row[0])
                        if (canonical_json(source) != source_row[0] or source.get("id") != source_ref
                                or source.get("content_hash") != source_row[1] or source_row[1] != evidence.get("source_envelope_hash")
                                or content_hash({k:v for k,v in source.items() if k != "content_hash"}) != source_row[1]):
                            raise ValueError("answer source envelope binding drifted")
                        claim = next(c for c in claims if c["id"] == binding["claim_version_ref"])
                        receipt = cursor.execute("SELECT decision_json,result_json FROM reviewed_candidate_commits WHERE review_decision_ref=? AND candidate_claim_ref=?", (claim.get("semantic_review_ref"), claim.get("candidate_origin_ref"))).fetchone()
                        if receipt and source.get("source") == "source:sec-edgar" and source.get("operation") == "get_company_facts":
                            from .research_auto_commit import validate_policy_commit_decision, COMPANY_FACTS_RULE_REFS
                            decision = validate_policy_commit_decision(json.loads(receipt[0])); result = json.loads(receipt[1])
                            form = next((kind for kind,rule in COMPANY_FACTS_RULE_REFS.items() if rule == decision["rule_ref"]), None)
                            semantic_fields = ("subject_ref","metric_or_aspect","period","basis","normalized_statement")
                            bound = (form and canonical_json(decision) == receipt[0]
                                and decision["content_hash"] == claim.get("semantic_review_hash") == evidence.get("review_decision_hash")
                                and decision["candidate_claim_hash"] == claim.get("candidate_origin_hash")
                                and decision["candidate_evidence_hash"] == evidence.get("candidate_origin_hash")
                                and result.get("claim_version_ref") == claim["id"] and result.get("evidence_version_ref") == evidence["id"]
                                and all(decision["reviewed_semantics"].get(k) == claim.get(k) for k in semantic_fields))
                            actual_accessions = set(re.findall(r"\d{10}-\d{2}-\d{6}", canonical_json(source.get("source_record_refs", []))))
                            if bound and len(actual_accessions) == 1:
                                evidence_accessions.setdefault(claim["id"],set()).update(actual_accessions)
                                period_value = claim.get("period")
                                period_end = period_value.get("end") if isinstance(period_value,dict) else str(period_value).split("..")[-1]
                                doc_proofs.append({"accession":next(iter(actual_accessions)),"form":form,"report_date":period_end,
                                    "authority":"verified_same_filing_company_facts","claim_version_ref":claim["id"],"source_envelope_hash":source_row[1],"policy_decision_hash":decision["content_hash"]})
                source_wire = canonical_json(evidence)
                referenced_sources.append(source_wire)
                evidence_accessions.setdefault(binding["claim_version_ref"], set()).update(re.findall(r"\d{10}-\d{2}-\d{6}", source_wire))
        accession = re.findall(r"\d{10}-\d{2}-\d{6}", " ".join(referenced_sources))
        for table in ("coverage_mission_statement_filings",):
            for acc in accession:
                try:
                    rows = cursor.execute(f"SELECT accession,form,report_date FROM {table} WHERE company_ref=? AND accession=?", (question["company_ref"], acc)).fetchall()
                except sqlite3.OperationalError: continue
                doc_proofs.extend(dict(row) for row in rows)
        for kind in key["doc_kinds"]:
            for period in periods or [None]:
                eligible_claims = matched_periods[period] if period else {c["id"] for c in claims}
                def source_period_matches(row, claim_ref):
                    if period is None:
                        return True
                    claim = next(c for c in claims if c["id"] == claim_ref)
                    claim_period = claim.get("period")
                    # SEC reporting dates must match the exact formal range.
                    # A fiscal label without an end-date binding cannot prove
                    # that this filing covers the requested period.
                    return isinstance(claim_period, dict) and str(claim_period.get("end", ""))[:10] == row["report_date"]
                if not any(row["form"] == kind and row["accession"] in evidence_accessions.get(claim_ref, set())
                           and source_period_matches(row, claim_ref)
                           for row in doc_proofs for claim_ref in eligible_claims):
                    raise ValueError("required document type/period is not bound to an accepted claim's evidence")
    proof = {"question_version_ref": question["id"], "question_version_hash": question["content_hash"],
             "answer_criteria_hash": content_hash(question["answer_criteria"]), "issuer": question["company_ref"],
             "criteria_terms": sorted(words | chinese_terms | criteria_metrics), "metric": key["metric"], "period": key["period"],
             "document_metadata": doc_proofs, "claim_version_hashes": [b["claim_version_hash"] for b in bindings]}
    return {**proof, "content_hash": content_hash(proof)}
