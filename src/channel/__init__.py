"""The channel gateway: the seam a real messaging or voice channel attaches to.

Inbound messages arrive as a channel-agnostic webhook and are mapped to a session
Workflow by Signal-With-Start. Outbound messages leave through a configurable URL,
which in the demo points at a local stub the dashboard renders. Provider-specific
payload parsing is an adapter the adopting team writes; it is out of scope here.
"""
