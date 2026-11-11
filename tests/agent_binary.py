"""Optional: point the tests at an agent runtime binary outside the repo.

``NEUXMIND_TEST_AGENT_BINARY=/path/to/codex`` (the pinned Codex 0.146.1
app-server for this platform, e.g. from ``@openai/codex@0.146.1-linux-x64``)
makes the real-runtime tests run on machines without a packaged
``runtime/bin/codex`` or ``.build/agent-runtime/bin/codex``. Without the
variable nothing changes. Import before evaluating ``skipUnless`` decorators.
"""
import os
from pathlib import Path

from brainos import agent_runtime

_override = os.environ.get('NEUXMIND_TEST_AGENT_BINARY', '').strip()
if _override and Path(_override).is_file() and not agent_runtime.binary_path():
    agent_runtime.binary_path = lambda: Path(_override)
