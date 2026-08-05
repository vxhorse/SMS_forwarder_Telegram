"""What the interpreter under the image has to be true of, and who agrees on it.

Nothing here tests this project's own code. These are guards on the runtime it
is shipped on, kept together because they answer one question: is the thing CI
runs the tests on the thing the image runs the service on, and is that thing
free of a defect this service would be hurt by.
"""
import asyncio
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


async def test_the_interpreter_does_not_swallow_a_cancellation():
    """asyncio.wait_for must re-raise a cancellation, never absorb it.

    Up to and including Python 3.11, wait_for wraps its awaitable in a separate
    task and waits on an internal future:

        try:
            await waiter
        except CancelledError:
            if fut.done():
                return fut.result()      # <- the cancellation is dropped here
            ...
            raise

    If the wrapped task completes in the same event-loop iteration the
    cancellation arrives in, wait_for returns a value instead of re-raising and
    the caller carries on as though it had never been cancelled. 3.12 rebuilt
    wait_for on asyncio.timeout(), which wraps no task, so the branch is gone.

    This is not academic for this service. Supervisor cancels a component's
    task to end a session - on shutdown, and on every reconnect. The heartbeat
    loop spends nearly all of its time in _sleep, where a cancellation lands
    cleanly, but its probe is exactly the shape above: wait_for around a request
    that completes when the modem answers. A cancellation arriving in that
    window would be swallowed, and the loop would go on probing a connection
    that had already been torn down - a component task that cannot be ended,
    which teardown can then only wait out to STOP_BUDGET_SECONDS.

    The test is deliberately behavioural rather than a version comparison: what
    has to hold is that cancellation survives, not that a number is large
    enough. It fails on 3.11 and passes on 3.12 and later.
    """
    inner_started = asyncio.Event()
    answered = asyncio.Event()
    outcome = []

    async def request():
        inner_started.set()
        await answered.wait()
        return "answered"

    async def probe():
        try:
            await asyncio.wait_for(request(), timeout=1000)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        else:
            outcome.append("swallowed")

    task = asyncio.create_task(probe())
    # Both sides are parked before anything is triggered: the request on
    # answered.wait(), and the probe on the wait_for. No sleeping to get there,
    # so the ordering below is the loop's, not the wall clock's.
    await inner_started.wait()

    # The whole point, in two statements with no await between them: the
    # request completes and the probe is cancelled in the same iteration.
    answered.set()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert outcome == ["cancelled"], (
        "asyncio.wait_for absorbed a cancellation instead of re-raising it. "
        "This interpreter is older than 3.12; see the docstring for what that "
        "costs a service whose supervisor ends sessions by cancelling tasks."
    )


def test_ci_tests_the_interpreter_the_image_ships():
    """The suite has to run on whatever the Dockerfile puts under the service.

    This is the guard the project did not have when it needed one. The venv
    developers use is not the interpreter the image runs, and nothing connected
    the two, so a suite that had stopped passing on the shipped interpreter
    went unread across several CI runs - the hangs took each job to its own
    timeout, and GitHub reports that as "cancelled", which looks like a person
    stopping it rather than a failure.
    """
    dockerfile = (REPO / "Dockerfile").read_text()
    base = re.search(r"^FROM\s+python:(\d+\.\d+)-", dockerfile, re.MULTILINE)
    assert base, "could not find the python base image in the Dockerfile"
    shipped = base.group(1)

    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    matrix = re.search(r"^\s*python-version:\s*\[(.*?)\]", workflow, re.MULTILINE)
    assert matrix, "could not find the test matrix in ci.yml"
    tested = re.findall(r"\d+\.\d+", matrix.group(1))

    assert shipped in tested, (
        f"the image ships Python {shipped} but CI tests {tested}. "
        f"Either add {shipped} to the matrix or move the image onto one of "
        f"the versions already there - an interpreter nobody tests is one the "
        f"service meets first in production."
    )
