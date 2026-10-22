"""Stage 1: stage 0 with exactly one skill call as a Standalone Activity.

The loop, the state dict, the retry wrapper, the semaphore, and the prompt are
untouched. Only check_financing_status is executed as a Standalone Activity from
the client. Kill the harness mid call and the Activity survives, retries, and
completes on the worker, even though the conversation itself is still lost.
"""
