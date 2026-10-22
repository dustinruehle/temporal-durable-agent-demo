"""AgentCore runtime app (Serverless Worker host).

The shim is the channel gateway with different paths and a health endpoint, and
the Temporal Worker started in-process. It reuses src/channel's Signal-With-Start
path rather than writing a second one. The point of the beat: AgentCore ends a
session after idle, and the Workflow survives it, so a later message resumes the
same Workflow Id with conversation state intact. The runtime's session ceiling
stops bounding the conversation.
"""
