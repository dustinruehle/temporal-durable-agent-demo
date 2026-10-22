"""Stage 0: a plain harness with no Temporal.

Conversation state in a dict keyed by session id, a hand-rolled retry wrapper, a
token-per-minute semaphore, and no scheduler. Kill it mid tool call and the
session is gone. This is the starting architecture, in a team's own shape.
"""
