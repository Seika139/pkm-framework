#!/usr/bin/env python
#MISE description="許可されたStorage領域をcommitしてGit remoteと同期する"

from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_runner import run_storage_task

raise SystemExit(run_storage_task("sync", sys.argv[1:], Path(__file__)))
