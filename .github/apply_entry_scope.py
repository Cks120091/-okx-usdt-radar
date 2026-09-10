"""Isolated review helper; never promoted to main."""
import base64
import hashlib
import subprocess
import zlib
from pathlib import Path

allowed = '''docs/SHORT_ENTRY_SCOPE_20260910.md
radar/decision.py
radar/entry_window.py
radar/market_story.py
radar/preflight.py
radar/public_payload.py
radar/repository.py
radar/scanner.py
radar/service.py
radar/strategy.py
tests/test_decision.py
tests/test_short_entry_window.py'''.splitlines()
encoded = ''.join(Path(f'.github/entry-scope-patch-{i}.b64').read_text().strip() for i in range(5))
assert len(encoded) == 18980
patch = zlib.decompress(base64.b64decode(encoded, validate=True))
assert len(patch) == 53646
assert hashlib.sha256(patch).hexdigest() == '247aebf91ee100c39e91d52c869eaf6b3133ce40dfeeddfe4c8a862db9d2fe29'
path = Path('/tmp/short-entry-scope.patch')
path.write_bytes(patch)
subprocess.run(['git', 'apply', '--check', str(path)], check=True)
subprocess.run(['git', 'apply', str(path)], check=True)
subprocess.run(['git', 'add', '-N', '--', *allowed], check=True)
changed = set(subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines())
assert changed == set(allowed), (changed, set(allowed))
Path('/tmp/entry-scope-allowed.txt').write_text('\n'.join(allowed) + '\n')
print('Applied checksum-verified patch to exactly 12 application/test/documentation files. No deployment files changed.')
