"""Release memo repair headroom only after exact durable fee settlement."""
from __future__ import annotations

from .investment_memo_quiesce import settled_call
from .investment_memo_macro_accounting import AUDIT_ERRORS


class MemoRepairBudget:
    def __init__(self, model, *, run_micros, producer_micros, verifier_micros):
        self.model = model
        self.run_micros = run_micros
        self.producer_micros = producer_micros
        self.verifier_micros = verifier_micros
        self.accounted_micros = 0
        self.proofs = []
        self.completed_calls = []
        self.technical_slot_consumed = False
        self.stage = None
        self.macro = None
        self.macro_error = None
        self.pending_repair = None
        self.selected_maintenance = None
        self.maintenance_hold = None
        self.maintenance_enabled = False

    def begin_stage(self, *, max_units, allow_technical, quiesce=None, repair=False, maintenance=None):
        self.stage = {"max_units":max_units,"allow_technical":allow_technical,"quiesce":quiesce,"repair":repair,"maintenance":maintenance}

    def bind_macro(self, prompts, mission, *, allow_maintenance=False):
        self.maintenance_enabled = allow_maintenance is True
        # Injected non-transport test doubles retain their old small interface.
        # Every actual CockpitModel has a Scheduler path and must bind here.
        if getattr(self.model, 'scheduler_db', None) is None:
            return
        from .investment_memo_macro_accounting import MemoMacroAccounting
        try:
            self.macro = MemoMacroAccounting(self.model, prompts, mission)
        except AUDIT_ERRORS as exc:
            self.macro_error = str(exc)

    def prepare_repair(self, kwargs):
        self.pending_repair = kwargs

    def _refresh_macro(self):
        if self.macro is None:
            return
        try:
            _, _, total = self.macro.snapshot()
            self.accounted_micros = total
            self.macro_error = None
        except AUDIT_ERRORS as exc:
            self.macro_error = str(exc)

    def __getattr__(self, name):
        return getattr(self.model, name)

    def call(self, **kwargs):
        # A failed or unproved call retains its full reservation. Its returned
        # prose and reported cost alone cannot authorize another paid call.
        reserved = self.producer_micros
        cached = None; cached_failure = None
        if self.macro_error is not None and self.macro is None:
            from .cockpit_model import CockpitModelError
            raise CockpitModelError('memo_macro_fee_audit_required: '+self.macro_error)
        if self.macro is not None:
            from .cockpit_model import CockpitModelError
            try:
                maintenance = None if not self.stage else self.stage.get('maintenance')
                if not maintenance and not (self.stage and self.stage.get('repair')):
                    from .investment_memo_group_maintenance import free_revalidated_parent
                    cached = free_revalidated_parent(self.macro,kwargs)
                    if cached is None:
                        kwargs, maintenance = self.macro.group_request(kwargs)
                self.selected_maintenance = maintenance
                if maintenance is not None:
                    if self.stage and self.stage.get('maintenance') and not self.maintenance_enabled:
                        raise ValueError('memo group maintenance is disabled by the current repair policy')
                    self.macro.register_maintenance(maintenance,kwargs)
                elif self.stage and self.stage.get('repair'):
                    if self.macro_error is not None:
                        raise ValueError(self.macro_error)
                    self.macro.register(role='semantic', kwargs=kwargs)
                try:
                    if cached is None:
                        cached = self.macro.replay(kwargs)
                except CockpitModelError as exc:
                    cached_failure = exc
                    from .investment_memo_failure_cost import MemoFailureCostReporter, failure_cost
                    if isinstance(self.model, MemoFailureCostReporter):
                        proof = failure_cost(self.model.model, exc, kwargs, watermark=self.model.watermark)
                        self.model.failures[proof['work_order_ref'] or ('unbound', kwargs['purpose'], kwargs['request_id'])] = proof
                if cached is None and cached_failure is None:
                    if maintenance is not None and not self.maintenance_enabled:
                        raise ValueError('memo group maintenance is disabled by the current repair policy')
                    self._refresh_macro()
                    if self.macro_error is not None:
                        raise ValueError(self.macro_error)
                    self.macro.before_paid(kwargs, run_micros=self.run_micros,
                        producer_micros=self.producer_micros, verifier_micros=self.verifier_micros,
                        role='maintenance' if maintenance is not None else (
                            'semantic' if self.stage and self.stage.get('repair') else 'normal'))
            except AUDIT_ERRORS as exc:
                self.macro_error = str(exc)
                raise CockpitModelError('memo_macro_fee_audit_required: '+str(exc)) from exc
        try:
            if cached_failure is not None:
                raise cached_failure
            if cached is not None:
                call = cached
            else:
                from .investment_memo_terminal_maintenance import MARKER,call_existing
                from .investment_memo_profile_maintenance import MARKER as PROFILE_MARKER,call_existing as profile_call
                if PROFILE_MARKER in kwargs.get('request_id',''):
                    call=profile_call(self,kwargs)
                else:
                    call = call_existing(self,kwargs) if MARKER in kwargs.get('request_id','') else self.model.call(**kwargs)
        except BaseException as exc:
            self.accounted_micros += reserved
            from .cockpit_model import CockpitModelError
            if not isinstance(exc, CockpitModelError):
                raise
            self._refresh_macro()
            if self.macro_error is not None:
                raise
            from .investment_memo_timeout_recovery import recover
            from .investment_memo_profile_maintenance import recover as profile_recover
            call = profile_recover(self, exc, kwargs)
            if call is None:call = recover(self, exc, kwargs)
            if call is None:
                from .investment_memo_terminal_maintenance import recover as terminal_recover
                call = terminal_recover(self, exc, kwargs)
            if call is None:
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
        self.completed_calls.append({key:call.get(key) for key in (
            'work_order_ref','result_envelope_ref','route_decision_ref','invocation_ref','cost_status')})
        self._refresh_macro()
        if self.macro is not None and getattr(self.macro, 'extra', None) is not None:
            self.technical_slot_consumed |= self.macro.extra.get('role') == 'technical'
        return call

    def can_repair(self, *, remaining_groups):
        if self.technical_slot_consumed:
            return False
        if self.macro_error is not None:
            return False
        if self.macro is not None:
            if not self.macro.complete or self.pending_repair is None:
                return False
            try:
                from .investment_memo_draft import GROUPS
                total, reserved = self.macro.room(group_index=len(GROUPS)-remaining_groups-1,
                    producer_micros=self.producer_micros, verifier_micros=self.verifier_micros)
                self.accounted_micros = total
                extra = self.macro.extra
                if extra is not None:
                    if extra.get('role') != 'semantic' or extra.get('request_id') != self.pending_repair['request_id']:
                        return False
                    answer = self.macro.replay(self.pending_repair)
                    if answer is not None:
                        return True
                return total + reserved + self.producer_micros <= self.run_micros
            except Exception as exc:
                self.macro_error = str(exc)
                return False
        needed = self.accounted_micros + (remaining_groups + 1) * self.producer_micros + self.verifier_micros
        return needed <= self.run_micros

    def prepare_maintenance(self, *, context, outcome, remaining_groups):
        """A new fixed permission only after the original slot is consumed."""
        self.maintenance_hold = None
        if (not self.maintenance_enabled or self.macro is None or self.macro_error is not None
                or self.selected_maintenance is not None):
            return None
        try:
            body = self.macro.prepare_maintenance(context,outcome)
            total, reserved = self.macro.room(group_index=3-remaining_groups,
                producer_micros=self.producer_micros,verifier_micros=self.verifier_micros)
            # Conservatively retain every remaining group and the independent
            # verifier, even when an old provider success may later validate.
            reserved = max(reserved,remaining_groups*self.producer_micros+self.verifier_micros)
            if total+reserved+self.producer_micros > self.run_micros:
                self.maintenance_hold = {"status":"budget_refused", "accounted_micros":total,
                    "future_reserved_micros":reserved, "run_ceiling_micros":self.run_micros}
                return None
            return body
        except AUDIT_ERRORS as exc:
            self.maintenance_hold = {"status":"qualification_unproved","reason":str(exc)}
            # Qualification failure is not a provider failure and cannot
            # purchase a correction. Preserve the actual content diagnostic.
            return None


class MemoMacroVerifier:
    """Track the original single independent verifier in the same macro."""
    def __init__(self, model, wrapper):
        self.model, self.wrapper = model, wrapper

    def __getattr__(self, name):
        return getattr(self.model, name)

    def call(self, *, producer_route_decision_refs=None, **kwargs):
        from .cockpit_model import CockpitModelError, independent_model_call
        macro = self.wrapper.macro
        if macro is None:
            return independent_model_call(self.model, producer_route_decision_refs=producer_route_decision_refs, **kwargs)
        if (str(self.model.scheduler_db) != str(macro.model.scheduler_db)
                or any(self.model.config[key] != macro.model.config[key]
                       for key in ('model_router_db','budget_db'))):
            raise CockpitModelError('memo macro verifier must share the exact Scheduler/router/fee authority')
        supplied = {**kwargs, 'producer_route_decision_refs': producer_route_decision_refs}
        try:
            macro.register(role='verifier', kwargs=kwargs)
            cached = macro.replay(supplied)
            if cached is not None:
                return cached
            macro.before_paid(kwargs, run_micros=self.wrapper.run_micros,
                producer_micros=self.wrapper.producer_micros, verifier_micros=self.wrapper.verifier_micros,
                role='verifier')
        except AUDIT_ERRORS as exc:
            raise CockpitModelError('memo_macro_fee_audit_required: '+str(exc)) from exc
        try:
            return independent_model_call(self.model, producer_route_decision_refs=producer_route_decision_refs, **kwargs)
        finally:
            self.wrapper._refresh_macro()
