# Agent progress

The work and activity of Claude and Codex on the architecture epic.

**Tick**: One runner invocation that selects at most one action. It may exit without starting a model session.

**Active tick**: A tick whose lock names a process still present on the host. It may be selecting work, calling GitHub, or running a model.

**Successful session**: A model invocation that exited successfully and was recorded by the runner. It does not prove completion of its task.

**Delivery progress**: Issue status, checked leaf checkboxes, and merged PRs recorded on GitHub. These are separate measures; merging does not prove production activation.

**Queued action**: Work the shared selector proposes from its latest GitHub snapshot. It may differ from the action a runner already started.

**Task path**: Where an action sits in the epic: epic, area, task, subtask, leaf and PR. It is read from issue titles, `Parent:` lines and PR `Issue:`/`Leaf IDs:` lines.

**Pause request**: The operator's request that runners stop starting new ticks. A tick already running may still be active.

Example: “Claude has an active tick reviewing Codex's PR. Its last successful session was also a review, but delivery progress only changes when GitHub records a result.”
