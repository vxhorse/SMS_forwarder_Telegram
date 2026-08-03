"""Entry point: assemble the components and let the supervisor drive them."""

import asyncio
import signal
import sys
import traceback

import config
from config import BOT_TOKEN, CHAT_ID, PROXY_URL
from logger import setup_logger
from module.device_manager import DeviceManager
from module.health import HealthState
from module.supervisor import FatalConfigError, Supervisor
from module.telegram_bot import TelegramBot

logger = setup_logger(__name__)

SERVICE_NAMES = ["device", "telegram"]

# Process exit codes. Anything non-zero asks the container runtime to restart
# everything, which is the right answer to a stuck process and the wrong one to
# a configuration that can never work - hence the separate code for the latter,
# so an operator reading `docker ps` can tell them apart.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

# Keyed by the supervisor's recorded reason for stopping. Both watchdog reasons
# share a code on purpose: a component that stopped answering and a snapshot
# that stopped being refreshed are the same request, which is to be restarted.
# The logs say which of the two it was.
EXIT_CODES = {
    None: EXIT_OK,
    "watchdog": EXIT_FAILURE,
    "stalled": EXIT_FAILURE,
    "fatal_config": EXIT_CONFIG,
}


def _bounded_notify(telegram: TelegramBot, timeout: float):
    """Wrap the outward notification channel with a deadline.

    Sending retries several times with a delay between attempts, so a
    notification issued while the messaging API is unreachable holds its caller
    far longer than one send would suggest. Two callers cannot afford that: the
    device reports every successful connection, which would slow each reconnect
    cycle, and it reports its own teardown, which runs while the process is
    stopping and the container runtime is already counting down to a kill.

    The device path treats a failed notification as nothing more than a failed
    notification, so expiring here costs a log line and no more.
    """

    async def notify(text: str) -> None:
        await asyncio.wait_for(telegram.notify(text), timeout=timeout)

    return notify


def _describe_crash(error: BaseException) -> str:
    """Describe an unexpected failure by type and location, never by message.

    Some client errors render the request they came from in their own str(),
    and that request carries a credential, so the message stays out. The frames
    are read from the traceback object rather than from the exception, which is
    what keeps it out while still saying where the process broke - and for a
    failure that is by definition unexplained, where is all there is to go on.
    """
    frames = "".join(traceback.format_tb(error.__traceback__)).rstrip()
    if not frames:
        return type(error).__name__
    return f"{type(error).__name__}\n{frames}"


def build_services():
    """Construct every component and wire them to each other.

    There is deliberately no startup deadline here. Each component waits for
    its own dependency for as long as it takes; a container may well start
    before its USB device has enumerated or its outbound proxy is ready.
    """
    health = HealthState(SERVICE_NAMES)
    shutdown_event = asyncio.Event()
    supervisor = Supervisor(health, shutdown_event)

    # Constructed with a placeholder callback and wired afterwards, because the
    # two components reference each other.
    telegram = TelegramBot(None, BOT_TOKEN, CHAT_ID, PROXY_URL, health=health)
    device = DeviceManager(
        telegram.handle_forwarding_sms,
        health=health,
        notify=_bounded_notify(telegram, config.NOTIFY_TIMEOUT),
    )
    telegram.send_sms_callback = device.handle_send_sms

    return health, supervisor, device, telegram, shutdown_event


async def run() -> int:
    """Run until shutdown is requested. Returns the process exit code."""
    # config.py cannot log a clamped setting itself - it cannot import the
    # logger without a circular import, since logger.py reads its level from
    # config - so it only records what got clamped. Logged here, once, before
    # anything the clamped values might affect gets built.
    for notice in config.CLAMP_NOTICES:
        logger.warning(f"Configuration adjusted: {notice}")

    health, supervisor, device, telegram, shutdown_event = build_services()

    # Cleared on the way in as well as on the way out. A process that was
    # killed rather than stopped leaves its last snapshot behind, and until
    # that file goes stale the healthcheck reads it as proof that every
    # component is up. Nothing else would correct it: the snapshot is only
    # rewritten once every component really is up, which is exactly the state
    # the process has not reached yet.
    health.clear_file()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Setting the event is all the handler does. The shutdown itself
            # then runs on the loop, so a signal arriving twice cannot start a
            # second one, and a signal arriving during teardown cannot cut it
            # short.
            loop.add_signal_handler(sig, shutdown_event.set)

    tasks = [
        asyncio.create_task(supervisor.run_service(device), name="supervise-device"),
        asyncio.create_task(supervisor.run_service(telegram), name="supervise-telegram"),
        asyncio.create_task(supervisor.watchdog_loop(), name="watchdog"),
    ]

    # Watched alongside the supervision tasks so that a stop request is acted
    # on the moment it arrives. A supervision loop checks the event between
    # attempts and while backing off, but not while it is inside an attempt: a
    # component waiting for a device node to appear is parked inside
    # connect_once(), and the loop is parked awaiting it. Waiting for those
    # tasks to notice on their own would stretch a stop out to the length of a
    # full reconnection wait, and the container runtime kills a process that
    # outlives its stop grace period. Cancelling is what gets them out.
    stop_request = asyncio.ensure_future(shutdown_event.wait())

    unexpected_failure = False
    try:
        # Components are peers: neither waits for the other, and neither
        # failing to connect ends anything. This returns when a supervision
        # task ends, which only a fatal configuration error or a shutdown
        # does, or when a shutdown is requested from anywhere else - a signal,
        # or the watchdog.
        await asyncio.wait([*tasks, stop_request], return_when=asyncio.FIRST_COMPLETED)
    finally:
        shutdown_event.set()
        stop_request.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        # Each supervision loop releases its own component as it unwinds, and
        # does so without letting a second cancellation truncate it, so this
        # is where the components are actually torn down.
        await asyncio.gather(*tasks, stop_request, return_exceptions=True)

        # Read after the gather rather than from the wait above, so that a task
        # which failed at the same moment the shutdown was requested is still
        # seen: it would not be in that result set. A task still unwinding when
        # it was cancelled is not covered, because it ends up cancelled and the
        # loop below skips it. Nothing depends on that: a supervision loop
        # records its reason for stopping before it re-raises, so the exit code
        # comes from the recorded reason either way, and this loop only adds
        # the log line and the code for a failure with no reason recorded.
        for task in tasks:
            if task.cancelled():
                continue
            error = task.exception()
            if error is None:
                continue
            if isinstance(error, FatalConfigError):
                # Quoted in full because this project writes the text itself,
                # and it names a setting rather than its value.
                logger.error(
                    f"Configuration is invalid, restarting cannot fix it: {error}"
                )
            else:
                # Only the type is reported. Some client errors render the
                # request they came from in their own str(), and that request
                # carries a credential; the supervisor has already described
                # whatever actually broke.
                unexpected_failure = True
                logger.error(
                    f"Supervision task {task.get_name()} failed: "
                    f"{type(error).__name__}"
                )

        # Covers the one case a supervision loop cannot: a task cancelled
        # before its first step ever ran never reaches its own cleanup. Both
        # calls are idempotent.
        await device.teardown()
        await telegram.teardown()
        # A stopping process must stop claiming to be healthy.
        health.clear_file()

    exit_code = EXIT_CODES.get(supervisor.exit_reason, EXIT_FAILURE)
    if unexpected_failure and exit_code == EXIT_OK:
        exit_code = EXIT_FAILURE
    return exit_code


if __name__ == "__main__":
    logger.info("Starting up")
    code = EXIT_OK
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        # Only reachable before the signal handlers are installed; afterwards
        # an interrupt sets the shutdown event instead.
        logger.warning("Interrupted before shutdown could be requested")
    except Exception as error:
        logger.error(f"Fatal error: {_describe_crash(error)}")
        code = EXIT_FAILURE
    finally:
        logger.info(f"Exiting with code {code}")
        sys.exit(code)
