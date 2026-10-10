"""Retained tools keep their current targets and allow isolated portable inputs."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location('portable_' + name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HomeDefaultsTests(unittest.TestCase):
    def test_existing_strict_helper_defaults_follow_home_without_relaxing_identity(self):
        with tempfile.TemporaryDirectory(prefix='retained-home-') as directory:
            home = Path(directory).resolve()/'synthetic owner home'
            with patch.object(Path, 'home', return_value=home):
                config = load_script('prepare_successor_config_transition')
                publication = load_script('successor_research_publication_transition')
                stage = load_script('stage_r11a_ops_candidate')
                transcript = load_script('run_alphaengine_transcript_polish_canary')
            self.assertEqual(str(home/'Projects/dalton-research-agent-os/integrations/openclaw-web-search-broker'),
                             config.OPENCLAW_WEB_SEARCH_LEGACY_PATH)
            self.assertEqual(home/'Projects/dalton-owner-activation-20260910', publication._OWNER_ROOT)
            self.assertEqual(home/'Projects/dalton-foundation-followup-r11a-acceptance-worktree', stage.SOURCE)
            self.assertEqual('424c22529d5317c4c5c9dc8ba88f37299cea3f49', stage.COMMIT)
            self.assertEqual(home/'.openclaw/dalton-model-broker.sock', transcript.DEFAULT_BROKER_SOCKET)
            self.assertEqual(home/'.openclaw/dalton-model-broker.sock.key', transcript.DEFAULT_BROKER_AUTH_KEY)

    def test_readonly_canary_cli_defaults_follow_home_and_explicit_inputs_still_win(self):
        with tempfile.TemporaryDirectory(prefix='retained-canary-') as directory:
            home = Path(directory).resolve()/'synthetic owner home'
            for name, arity in [('run_p8b_company_research_view_canary',1),
                                ('run_p8c_bounded_planner_canary',2)]:
                with self.subTest(name=name):
                    module = load_script(name)
                    with patch.object(Path, 'home', return_value=home), \
                         patch.object(module, 'run', return_value={'status':'isolated_args_only'}) as run, \
                         patch.object(sys, 'argv', [name]), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0,module.main())
                    state=home/'Library/Application Support/Dalton/state/dalton-core'
                    expected=[state/'core.sqlite'] + ([state/'scheduler.sqlite'] if arity==2 else [])
                    self.assertEqual(tuple(expected),run.call_args.args)
                    explicit=[home/'custom core.sqlite'] + ([home/'custom scheduler.sqlite'] if arity==2 else [])
                    args=[name,'--source-core',str(explicit[0])]
                    if arity==2:args += ['--source-scheduler',str(explicit[1])]
                    with patch.object(module,'run',return_value={}) as run, \
                         patch.object(sys,'argv',args),contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0,module.main())
                    self.assertEqual(tuple(explicit),run.call_args.args)


@unittest.skipUnless(shutil.which('zsh'),'zsh is unavailable')
class PortableLanguageCapTests(unittest.TestCase):
    def test_default_home_preview_is_readonly_and_explicit_config_apply_is_local(self):
        with tempfile.TemporaryDirectory(prefix='retained-cap-') as directory:
            home=Path(directory)
            default=home/'Library/Application Support/Dalton/state/dalton-core/research-publication-worker-config.json'
            default.parent.mkdir(parents=True)
            original={'purpose_daily_cap_usd':{'research_language_revision':7},'fixture_unrelated':'kept'}
            default.write_text(json.dumps(original))
            before=default.read_bytes()
            env={**os.environ,'HOME':str(home)};env.pop('WORKER_CONFIG',None)
            script=ROOT/'docs/ops/raise-language-revision-cap.sh'
            preview=subprocess.run(['zsh',str(script),'50'],env=env,capture_output=True,text=True)
            self.assertEqual(0,preview.returncode,preview.stderr)
            self.assertEqual(before,default.read_bytes())
            explicit=home/'alternate config.json';explicit.write_bytes(before)
            applied=subprocess.run(['zsh',str(script),'50','apply'],
                                   env={**env,'WORKER_CONFIG':str(explicit)},capture_output=True,text=True)
            self.assertEqual(0,applied.returncode,applied.stderr)
            self.assertEqual(before,default.read_bytes())
            self.assertEqual({'purpose_daily_cap_usd':{'research_language_revision':50.0},
                              'fixture_unrelated':'kept'},json.loads(explicit.read_text()))
            self.assertEqual(1,len(list(home.glob('alternate config.json.bak-*'))))


if __name__=='__main__':unittest.main()
