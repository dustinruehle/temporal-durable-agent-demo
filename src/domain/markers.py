"""Internal conversation markers, kept free of imports so a Workflow can use them.

These strings tag the messages the Workflow sends to the agent when a durable
follow-up fires or a back-office handoff resolves. The mock treats them as cues;
a real model reads them as ordinary context.
"""

from __future__ import annotations

# Placed on the message the Workflow sends when a durable follow-up fires.
FOLLOW_UP_DUE_MARKER = "[FOLLOW_UP_DUE]"

# Placed on the message the Workflow sends after a back-office handoff resolves.
HANDOFF_RESOLVED_MARKER = "[HANDOFF_RESOLVED]"
