"""Finite deep-gate paid boundaries around ordinary content-addressed calls."""
from __future__ import annotations

from copy import copy
from pathlib import Path
from .cockpit_model import CockpitModelError


class GateExecutionStopped(BaseException):
    def __init__(self,reason,proof):
        self.reason,self.proof=reason,proof
        super().__init__(reason)


class GateExecution:
    """A run-private wrapper; no shared CockpitModel field is ever mutated."""
    def __init__(self,model,accounting,*,run_micros,verifier_micros,quiesce=None):
        self.model=model;self.accounting=accounting
        self.run_micros=run_micros;self.verifier_micros=verifier_micros
        self.quiesce=quiesce;self.role=None;self.group=None
        self.future_groups=0
        self.new_cost_micros=0

    def __getattr__(self,name):
        return getattr(self.model,name)

    def stage(self,role,group='all',*,future_groups=0):
        self.role,self.group=role,group
        self.future_groups=future_groups
        return self

    def _stop(self,reason):
        proof=self.accounting.last or {'status':'accounting_unproved','scope_ref':self.accounting.ref}
        raise GateExecutionStopped(reason,proof)

    def call(self,*,purpose,request_id,prompt,mission,producer_route_decision_refs=()):
        from .host_research_reconcile import resolved_model_config
        kwargs={'purpose':purpose,'request_id':request_id,'prompt':prompt,'mission':mission}
        if producer_route_decision_refs:
            kwargs['producer_route_decision_refs']=producer_route_decision_refs
        if self.role is None:
            self._stop('deep-gate call has no explicitly selected run stage')
        role=self.role
        if role=='normal':
            entry=next((e for e in self.accounting.entries if e['group']==self.group),None)
            if entry is None:self._stop('deep-gate normal stage is outside the frozen group set')
            if request_id!=entry['request_id']:
                if not request_id.startswith('contract-repair:'):
                    self._stop('deep-gate group continuation is not its existing shape repair')
                role='shape'
        if role!='normal':
            try:self.accounting.register(role=role,group=self.group,kwargs=kwargs)
            except ValueError as exc:self._stop(str(exc))
        if self.quiesce is not None:self.quiesce.checkpoint()
        try:
            replay=self.accounting.replay(kwargs)
        except CockpitModelError:
            try:proof=self.accounting.snapshot()[2]
            except ValueError as exc:self._stop(str(exc))
            if not proof['unknown_work_refs']:raise
            # Only an explicit human increase can open this once-only child;
            # old unknown amounts continue to consume the same input scope.
            if role!='normal':
                self._stop('deep-gate old correction failure has no new repair authorization')
            try:
                grant=self.accounting.recovery_grant(kwargs,group=self.group)
                kwargs={**kwargs,'request_id':grant['request_id']}
                replay=self.accounting.replay(kwargs)
            except (CockpitModelError,ValueError)as exc:
                self._stop('deep-gate old TIMEOUT recovery is unavailable: '+str(exc))
        except ValueError as exc:self._stop(str(exc))
        if replay is not None:
            if self.quiesce is not None:
                self.quiesce.returned(self.model,replay,mission,purpose=purpose)
            return replay
        # Match the ordinary CockpitModel.call resolver, but keep one immutable
        # config for both this admission price check and this actual call. A
        # concurrent settings save cannot expand the call after the run gate.
        local=copy(self.model)
        local.config=dict(resolved_model_config(Path(local.scheduler_db).parent,
                                               local.config,purpose))
        call_budget=local.budget_for(purpose)
        ceiling=int(round(call_budget['max_cost_usd']*1_000_000))
        future=self.future_groups*int(round(local.budget_for('deep_insight_gate')['max_cost_usd']*1_000_000))
        if purpose!='deep_insight_gate_verifier':future+=self.verifier_micros
        try:self.accounting.before_paid(run_micros=self.run_micros,next_call_micros=ceiling,
                                        retained_future_micros=future)
        except ValueError as exc:self._stop(str(exc))
        if self.quiesce is not None:self.quiesce.checkpoint()
        def guard(*,work,attempt_number,reserved_micros,mission_binding):
            if (mission_binding.get('mission_version_ref')!=mission['id']
                    or mission_binding.get('mission_version_hash')!=mission['content_hash']):
                raise ValueError('deep-gate current admission mission differs')
            self.accounting.before_admission(work=work,attempt_number=attempt_number,
                reserved_micros=reserved_micros,mission=mission,run_micros=self.run_micros,
                retained_future_micros=future)
        local._run_cost_admission_guard=guard
        try:
            answer=local._call_resolved(**kwargs)
        except CockpitModelError:
            try:proof=self.accounting.snapshot()[2]
            except ValueError as exc:self._stop(str(exc))
            self.new_cost_micros=proof['current_run_cost']['settled_micros']
            if proof['unknown_work_refs']:
                self._stop('deep-gate strong systemic provider/transport unknown stops this round')
            raise
        self.new_cost_micros=int(answer.get('cost_micros')or 0)
        # Every successful paid call is checkpointed before parsing or any
        # possible shape/numbers/rubric correction can buy another call.
        try:
            try:proof=self.accounting.snapshot()[2]
            except ValueError as exc:self._stop(str(exc))
            self.new_cost_micros=proof['current_run_cost']['settled_micros']
        except ValueError as exc:self._stop(str(exc))
        if self.quiesce is not None:self.quiesce.returned(local,answer,mission,purpose=purpose)
        return answer
