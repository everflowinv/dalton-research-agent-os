"""Authenticated capability IPC is not a model probe or journal operation."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.openclaw_broker_capabilities import read_profile_limits,CONTRACT_REF
from dalton_core.openclaw_model_adapter import canonical_hash,BrokerProtocolError
from tests.test_openclaw_model_adapter import FakeBroker

class BrokerCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='cap-proof-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.key=self.root/'auth.key';self.key.write_text('a'*64);self.key.chmod(0o600)
    def config(self,path):return {'broker_socket':str(path),'broker_client_id':'client:dalton-runtime','broker_auth_key':str(self.key),'expected_agent_id':'dalton-model-broker'}
    def response(self,request):
        self.assertIn('auth',request);self.assertTrue(request['readProfileLimits'])
        body={'schemaVersion':CONTRACT_REF,'brokerVersion':'test','runtimeVersion':'test','agentId':'dalton-model-broker','invocationId':request['invocationId'],
            'profileId':request['profileId'],'model':request['model'],'requestHash':canonical_hash({k:v for k,v in request.items()if k!='auth'}),
            'available':True,'limits':{'maxTokens':128000,'timeoutMs':1800000}}
        return {**body,'contentHash':canonical_hash(body)}
    def test_actual_authenticated_node_read_is_no_provider_and_no_journal(self):
        source=Path(__file__).resolve().parents[1]/'integrations/openclaw-model-broker'
        script='''import {ModelBroker} from './src/broker.mjs';import {BrokerServer} from './src/server.mjs';
const runtime={version:'test',llm:{complete(){throw Error('provider forbidden')}}};
const journal={get(){throw Error('journal forbidden')},claim(){throw Error('journal forbidden')}};
const server=new BrokerServer(new ModelBroker(runtime,{dedicatedAgentId:'dalton-model-broker',clientId:'client:dalton-runtime',socketName:'broker.sock',profiles:[{id:'profile:grok',model:'x-ai/grok-4-7',maxTokens:128000,timeoutMs:1800000}]},{journal}));
console.log(await server.start(process.env.DALTON_CAP_TEST_STATE));process.on('SIGTERM',async()=>{await server.stop();process.exit(0)});'''
        process=subprocess.Popen(['node','--input-type=module','-e',script],cwd=source,
            env={**os.environ,'DALTON_CAP_TEST_STATE':str(self.root)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            path=process.stdout.readline().strip();self.assertTrue(path)
            key=self.root/'broker.sock.auth.key'
            # BrokerServer chooses its owner-only auth path; locate only in this private fixture.
            keys=list(self.root.glob('*.key'));self.assertEqual(len(keys),2)
            key=next(k for k in keys if k!=self.key)
            config={**self.config(path),'broker_auth_key':str(key)}
            result=read_profile_limits(config,profile_id='profile:grok',model_ref='x-ai/grok-4-7')
            self.assertEqual(result['limits'],{'maxTokens':128000,'timeoutMs':1800000})
            self.assertFalse(any('journal' in p.name for p in self.root.iterdir()))
        finally:
            process.terminate();process.wait(timeout=5);process.stdout.close();process.stderr.close()
    def test_miss_tamper_request_identity_overflow_and_old_broker_rejected(self):
        for change in ('miss','hash','request','model','overflow','old'):
            def responder(request):
                result=self.response(request)
                if change=='miss':result['available']=False;result['limits']=None
                if change=='request':result['requestHash']='0'*64
                if change=='model':result['model']='other/model'
                if change=='overflow':result['limits']['timeoutMs']=1800001
                if change=='old':return {'schemaVersion':'0.1'}
                if change!='hash':result['contentHash']=canonical_hash({k:v for k,v in result.items()if k!='contentHash'})
                else:result['contentHash']='0'*64
                return result
            with self.subTest(change=change):
                broker=FakeBroker(self.root,responder)
                try:
                    with self.assertRaises(BrokerProtocolError):read_profile_limits(self.config(broker.path),profile_id='profile:grok',model_ref='x-ai/grok-4-7')
                finally:broker.close()
    def test_missing_key_is_rejected_without_connecting(self):
        self.key.chmod(0o644)
        with self.assertRaises(Exception):read_profile_limits(self.config(self.root/'absent.sock'),profile_id='profile:grok',model_ref='x-ai/grok-4-7')
