
<!-- second-brain-recall-note -->
## Persistent memory (second-brain)

This repo has a read-only recall hook installed (`hooks/inject_memory.py`,
registered in `.claude/settings.json`) that injects saved facts/decisions
from `jgra-source/second-brain` on every prompt -- but only if that repo is
ALSO attached as a sibling checkout in this session (the hook reads
`SECOND_BRAIN_ROOT`, default `../second-brain`). Without it, the hook fires
and silently injects nothing.

**At the start of any session here, attach `jgra-source/second-brain` (clone
it as a sibling directory) if you need saved context, prior decisions, or
project history.** Writing new facts back still only happens from a session
opened on second-brain itself -- this repo only reads.

