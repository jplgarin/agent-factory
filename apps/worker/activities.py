from temporalio import activity


@activity.defn(name="af.ping")
async def ping() -> str:
    """Liveness probe for the task queue. Has no side effects."""
    return "pong"
