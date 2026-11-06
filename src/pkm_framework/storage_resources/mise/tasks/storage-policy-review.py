#!/usr/bin/env python
#MISE description="source/profile設定を確認し、明示承認でWiki policy hashを記録する"

from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_runner import run_storage_task

raise SystemExit(run_storage_task("storage-policy-review", sys.argv[1:], Path(__file__)))
