"""Authenticated, bounded IPC read of active broker limits; never a model call."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import uuid
from .openclaw_model_adapter import (
    OpenClawModelAdapter, canonical_hash, BrokerProtocolError,
    owner_only_secret_file_provider,
)

CONTRACT_REF = 'broker-profile-limits:0.1'
RESPONSE_KEYS = frozenset({'schemaVersion','brokerVersion','runtimeVersion',
    'agentId','invocationId','profileId','model','requestHash','available','limits','contentHash'})


def read_profile_limits(config, *, profile_id, model_ref):
    adapter = OpenClawModelAdapter(config['broker_socket'],route_resolver=lambda _ref:None,
        auth_client_id=config['broker_client_id'],
        auth_key_provider=owner_only_secret_file_provider(Path(config['broker_auth_key'])),
        expected_agent_id=config['expected_agent_id'],timeout_seconds=4,max_frame_bytes=4096)
    token=uuid.uuid4().hex
    request={'schemaVersion':'0.1','invocationId':'invocation:broker-limits-'+token,
        'workOrderId':'work:broker-limits-'+token,'profileId':profile_id,'model':model_ref,
        'prompt':'Read configured profile limits without invoking a model.',
        'maxTokens':1,'timeoutMs':1,'readProfileLimits':True}
    signed=adapter._authenticate_request(request,int(datetime.now(timezone.utc).timestamp()*1000))
    response=adapter._exchange(signed,4)
    body={k:v for k,v in response.items()if k!='contentHash'}
    if (set(response)!=RESPONSE_KEYS or response.get('schemaVersion')!=CONTRACT_REF
            or canonical_hash(body)!=response.get('contentHash')
            or response.get('requestHash')!=canonical_hash(request)
            or response.get('invocationId')!=request['invocationId']
            or response.get('agentId')!=config['expected_agent_id']
            or response.get('profileId')!=profile_id or response.get('model')!=model_ref
            or response.get('available') is not True
            or not isinstance(response.get('brokerVersion'),str)
            or not isinstance(response.get('runtimeVersion'),str)):
        raise BrokerProtocolError('active broker capability proof unavailable or differs')
    limits=response.get('limits')
    if (not isinstance(limits,dict) or set(limits)!={'maxTokens','timeoutMs'}
            or any(type(limits[k])is not int or limits[k]<=0 for k in limits)
            or limits['maxTokens']>1_000_000 or limits['timeoutMs']>1_800_000):
        raise BrokerProtocolError('active broker capability limits are invalid')
    # Stable facts are re-read on every admission; an ephemeral invocation id
    # cannot become a new paid maintenance identity or reset its fixed grant.
    return {'contract_ref':CONTRACT_REF,'profile_id':profile_id,'model':model_ref,
            'limits':limits,'broker_version':response['brokerVersion'],
            'runtime_version':response['runtimeVersion']}
