"""Installing a candidate wheel cannot be satisfied by a source egg-info."""
import ast
import base64
import csv
from io import StringIO
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
import build_release


class BuildImportIsolationTests(unittest.TestCase):
    def test_only_selected_subprocess_removes_import_overrides(self):
        completed=subprocess.CompletedProcess([],0,'ok','')
        with patch.dict(os.environ,{'PYTHONPATH':'src','PYTHONHOME':'invalid','KEEP_THIS':'preserve'}),patch.object(build_release.subprocess,'run',return_value=completed) as run:
            build_release.run(['python','-m','pip'],cwd=Path('/tmp'),clean_python_env=True)
            kwargs=run.call_args.kwargs
            self.assertNotIn('PYTHONPATH',kwargs['env']);self.assertNotIn('PYTHONHOME',kwargs['env'])
            self.assertEqual(kwargs['env']['KEEP_THIS'],'preserve');self.assertEqual(kwargs['cwd'],'/tmp')
            self.assertEqual(os.environ['PYTHONPATH'],'src')
            build_release.run(['git','rev-parse','HEAD'])
            self.assertNotIn('env',run.call_args.kwargs)

    def test_real_pip_ignores_source_egg_info_and_installs_console_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);venv=root/'venv'
            subprocess.run([sys.executable,'-m','venv',str(venv)],check=True,capture_output=True)
            leak=root/'leaked-src';egg=leak/'dalton_build_probe.egg-info';egg.mkdir(parents=True)
            (egg/'PKG-INFO').write_text('Metadata-Version: 2.1\nName: dalton-build-probe\nVersion: 1.0\n')
            (egg/'top_level.txt').write_text('dalton_build_probe\n')
            wheel=root/'dalton_build_probe-1.0-py3-none-any.whl';dist='dalton_build_probe-1.0.dist-info/'
            files={'dalton_build_probe.py':b'def main():\n    print("installed-from-wheel")\n',
                   dist+'METADATA':b'Metadata-Version: 2.1\nName: dalton-build-probe\nVersion: 1.0\n',
                   dist+'WHEEL':b'Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                   dist+'entry_points.txt':b'[console_scripts]\ndalton-build-probe = dalton_build_probe:main\n'}
            record=StringIO();writer=csv.writer(record)
            for name,data in files.items():writer.writerow([name,'sha256='+base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b'=').decode(),len(data)])
            writer.writerow([dist+'RECORD','','']);files[dist+'RECORD']=record.getvalue().encode()
            with zipfile.ZipFile(wheel,'w') as zipped:
                for name,data in files.items():zipped.writestr(name,data)
            command=[str(venv/'bin/python'),'-m','pip','install','--no-index','--no-deps',str(wheel)]
            with patch.dict(os.environ,{'PYTHONPATH':str(leak)}):
                # The actual old failure: pip treats the source egg-info as
                # same-version installation, leaving the candidate incomplete.
                old=build_release.run(command,cwd=root)
                self.assertIn('already installed',old)
                self.assertFalse((venv/'bin/dalton-build-probe').exists())
                # PYTHONHOME also cannot poison candidate Python startup.
                with patch.dict(os.environ,{'PYTHONHOME':str(root/'invalid-home')}):
                    build_release.run(command,cwd=root,clean_python_env=True)
            result=subprocess.run([str(venv/'bin/dalton-build-probe')],capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'installed-from-wheel')
            self.assertTrue(next(venv.glob('lib/python*/site-packages/dalton_build_probe-1.0.dist-info')).is_dir())

    def test_real_build_pip_call_uses_neutral_work_directory_and_clean_env(self):
        # Contract of the production build path, without running a real build.
        tree=ast.parse(Path(build_release.__file__).read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build')
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id=='run' and any(isinstance(x,ast.Constant) and x.value=='pip' for x in ast.walk(n.args[0]))]
        self.assertEqual(len(calls),1);kwargs={k.arg:k.value for k in calls[0].keywords}
        self.assertEqual(kwargs['cwd'].id,'work');self.assertTrue(kwargs['clean_python_env'].value)

if __name__=='__main__':unittest.main()
