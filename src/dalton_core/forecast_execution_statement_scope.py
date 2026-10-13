"""Source-bound execution partitions; stored v2 proofs retain their algorithm."""
from . import forecast_execution_statement_scope_legacy as legacy
from .store import content_hash

RULE_REF = "rule:forecast-execution-statement-scope:3"
LEGACY_RULE_REF = legacy.RULE_REF
SUPPORTED_RULE_REFS = frozenset({RULE_REF, LEGACY_RULE_REF})
_wire, _roots, _refuse = legacy._wire, legacy._roots, legacy._refuse


def execution_scope(record, rows, source_groups, *, prior_partition_scope=None,
                    rule_ref=RULE_REF):
    kept, old = legacy.execution_scope(record, rows, source_groups,
                                      prior_partition_scope=prior_partition_scope)
    if rule_ref == LEGACY_RULE_REF:
        return kept, old
    if rule_ref != RULE_REF:
        _refuse("unknown execution rule")
    from .forecast_source_family_partition import partitions
    manifest = {k: v for k, v in old.items() if k != "content_hash"}
    manifest.update(rule_ref=RULE_REF,
                    arithmetic_partitions=partitions(record, rows, old["consumed_families"]))
    return kept, {**manifest, "content_hash": content_hash(manifest)}


def evaluate_execution(record, *, statement_rows=(), solver_results=(), scope):
    if scope.get("rule_ref") == LEGACY_RULE_REF:
        return legacy.evaluate_execution(record, statement_rows=statement_rows,
                                         solver_results=solver_results, scope=scope)
    if (scope.get("rule_ref") != RULE_REF
            or scope.get("content_hash") != content_hash({k: v for k, v in scope.items() if k != "content_hash"})
            or scope.get("source_rows_hash") != content_hash(_wire(statement_rows))
            or scope.get("roots_hash") != _roots(record)[1]):
        _refuse("execution scope does not bind this candidate and its full source rows")
    from .forecast_source_family_partition import partitions
    rebuilt = partitions(record, statement_rows, scope["consumed_families"])
    if rebuilt != scope.get("arithmetic_partitions"):
        _refuse("source-family arithmetic and all origin references do not replay")
    from .economic_invariants import forecast_subject, evaluate, FORECAST_MODEL
    subject = forecast_subject(record, statement_rows=statement_rows, solver_results=solver_results)
    subject["segments"] = [entry["group"] for entry in rebuilt]
    report = evaluate(output_kind=FORECAST_MODEL,
        output_ref=str(record.get("id") or record.get("model_ref") or "forecast-model"),
        company_ref=str(record.get("company_ref")), subject=subject)
    # Original precision authority still consumes unmodified source rows.
    grouped = {}
    for row in statement_rows:
        key = (row.get("ingest_id"), row.get("statement_context_ref"), row.get("statement"))
        grouped.setdefault(key, []).append(row)
    selected = [(tuple(entry["source_identity"]), entry["group"]) for entry in rebuilt]
    from .forecast_source_accuracy import evaluate_segment_accuracy
    return evaluate_segment_accuracy(record, subject, grouped, selected, report)
