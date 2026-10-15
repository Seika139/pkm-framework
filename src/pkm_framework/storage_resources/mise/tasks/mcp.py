#!/usr/bin/env python
#MISE description="MCP serverを起動する"

from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_runner import run_storage_task

raise SystemExit(run_storage_task("mcp", sys.argv[1:], Path(__file__)))
