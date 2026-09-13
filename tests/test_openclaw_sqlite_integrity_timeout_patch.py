from pathlib import Path
import json,shutil,tempfile,unittest
from integrations.openclaw_host_patches.patch_sqlite_integrity_timeout import ORIGINAL,PATCHED,apply,target
INSTALLED=Path('/Users/everflow/.openclaw/tools/node-v26.8.2/lib/node_modules/openclaw')
class TestPatch(unittest.TestCase):
 def test_exact_patch_keeps_integrity_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/'dist').mkdir();shutil.copy2(INSTALLED/'package.json',root/'package.json')
   src=next((INSTALLED/'dist').glob('sqlite-readonly-worker-*.mjs'));dst=root/'dist'/src.name;wire=src.read_text();
   if PATCHED in wire: wire=wire.replace(PATCHED,ORIGINAL,1)
   dst.write_text(wire)
   self.assertTrue(apply(root));self.assertFalse(apply(root));out=target(root).read_text()
   self.assertIn(PATCHED,out);self.assertIn('SQLITE_INSPECTION_TIMEOUT_MS',out);self.assertNotIn(ORIGINAL,out)
 def test_wrong_version_refuses(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);(root/'dist').mkdir();(root/'package.json').write_text(json.dumps({'version':'x'}))
   with self.assertRaises(ValueError):target(root)
if __name__=='__main__':unittest.main()
