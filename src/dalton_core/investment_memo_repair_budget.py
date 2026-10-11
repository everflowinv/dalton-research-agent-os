"""Release memo repair headroom only after exact durable fee settlement."""
from __future__ import annotations

from .investment_memo_quiesce import settled_call


class MemoRepairBudget:
    def __init__(self, model, *, run_micros, producer_micros, verifier_micros):
        self.model = model
        self.run_micros = run_micros
        self.producer_micros = producer_micros
        self.verifier_micros = verifier_micros
        self.accounted_micros = 0
        self.proofs = []

    def __getattr__(self, name):
        return getattr(self.model, name)

    def call(self, **kwargs):
        # A failed or unproved call retains its full reservation. Its returned
        # prose and reported cost alone cannot authorize another paid call.
        reserved = self.producer_micros
        try:
            call = self.model.call(**kwargs)
        except BaseException:
            self.accounted_micros += reserved
            raise
        reported = max(0, int(call.get('cost_micros') or 0))
        try:
            proof = settled_call(self.model, call, kwargs['mission'], purpose=kwargs['purpose'])
            charged = proof['accounted_micros']
            if not isinstance(charged, int) or isinstance(charged, bool) or charged < 0:
                raise ValueError('invalid settled cost')
        except Exception:
            self.accounted_micros += max(reserved, reported)
            self.proofs.append({'work_order_ref': call.get('work_order_ref'),
                                'status': 'reservation_retained'})
        else:
            self.accounted_micros += max(charged, reported)
            self.proofs.append(proof)
        return call

    def can_repair(self, *, remaining_groups):
        needed = self.accounted_micros + (remaining_groups + 1) * self.producer_micros + self.verifier_micros
        return needed <= self.run_micros
