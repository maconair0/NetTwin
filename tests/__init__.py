"""The tests read snapshots from a folder of their own: the committed samples only, never the live mirrors a
running gate saves into data/snapshots (a newer live `ip` mirror would otherwise stand in for the sample)."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DIR = tempfile.mkdtemp(prefix="netwins-tests-")
_tracked = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "data/snapshots"], capture_output=True, text=True).stdout.split()
for rel in _tracked or [str(p.relative_to(_ROOT)) for p in (_ROOT / "data" / "snapshots").glob("*.json")]:
    if (_ROOT / rel).exists():
        shutil.copy(_ROOT / rel, _DIR)
os.environ["NETWINS_SNAPSHOTS"] = _DIR
SAMPLES = _DIR
