"""Tests for the supervised Telegram client.

Nothing here touches the network: the aiohttp layer is replaced by fakes that
never open a socket. Every wait is bounded by a condition the test controls,
and no test spends a real interval to synchronise.
"""

import asyncio
import inspect
import json
import logging
import time

import aiohttp
import pytest
from multidict import CIMultiDict
from yarl import URL as YarlURL

from module.supervisor import FatalConfigError
from module.telegram_bot import TelegramApiError, TelegramBot

# Obviously fake, but shaped like a real token so the URL built from it has the
# same shape as the real one. Never a real credential.
FAKE_TOKEN = "123456:FAKEfakeFAKEfakeFAKEfakeFAKEfake"


async def _noop_send(*args, **kwargs):
    return True


def _make(token="123:ABC", chat_id="1"):
    return TelegramBot(_noop_send, token, chat_id, None)


def _leaky(chat_id="1"):
    """A bot whose token is distinctive enough to search log and error text for."""
    return TelegramBot(_noop_send, FAKE_TOKEN, chat_id, None)


def test_exposes_the_managed_service_interface():
    bot = _make()
    assert bot.name == "telegram"
    for method in ("connect_once", "run", "teardown"):
        assert inspect.iscoroutinefunction(getattr(bot, method))


def test_self_managed_reconnection_is_gone():
    """Reconnection must have exactly one owner, the supervisor."""
    assert not hasattr(TelegramBot, "reconnect")
    assert not hasattr(TelegramBot, "handle_blocking")


def test_missing_configuration_is_fatal():
    for token, chat_id in (
        ("your_telegram_bot_token", "1"),
        ("123:ABC", "your_telegram_chat_id"),
        ("", "1"),
        ("123:ABC", ""),
    ):
        with pytest.raises(FatalConfigError):
            TelegramBot(_noop_send, token, chat_id, None).validate_config()


def test_valid_configuration_passes():
    _make().validate_config()


def test_priming_event_is_gone():
    assert not hasattr(_make(), "priming_event")


def test_dead_lifecycle_state_is_gone():
    """polling_task was assigned nowhere once run() called polling_loop
    directly, and keyboard_text lost its only consumer with the commented-out
    main menu layout. State that is never written reads as live code."""
    assert not hasattr(_make(), "polling_task")
    for command in TelegramBot.COMMANDS.values():
        assert "keyboard_text" not in command


async def test_notify_is_silent_when_the_session_is_unavailable():
    bot = _make()
    bot.session = None
    await bot.notify("test notification")


async def test_teardown_is_idempotent():
    bot = _make()
    await bot.teardown()
    await bot.teardown()
    assert bot.session is None


# --- Fakes and helpers for the cases beyond the interface itself -------------


class _FakeResponse:
    """One canned HTTP reply, shaped like aiohttp's response context manager."""

    def __init__(self, status=200, payload=None, body=None):
        self.status = status
        self._payload = {} if payload is None else payload
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload) if self._body is None else self._body


class _FakeSession:
    """Stands in for aiohttp.ClientSession. Opens nothing, closes cleanly."""

    def __init__(self, response=None):
        self.closed = False
        self.response = _FakeResponse() if response is None else response
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        return self.response

    def post(self, url, **kwargs):
        self.requests.append(("POST", url))
        return self.response

    async def close(self):
        self.closed = True


class _FailingResponse:
    """A request that fails the way aiohttp fails: on entering the context."""

    def __init__(self, error):
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *exc_info):
        return False


class _DecodeFailingResponse(_FakeResponse):
    """A 200 whose body will not decode, as a proxy's HTML error page does."""

    def __init__(self, error):
        super().__init__(status=200)
        self._error = error

    async def json(self):
        raise self._error


def _url_rendering_error(url, status=502):
    """A real aiohttp ContentTypeError, built the way aiohttp builds one.

    aiohttp appends the request URL to str() for anything derived from
    ClientResponseError, and every URL this module builds carries the token.
    """
    request_info = aiohttp.RequestInfo(YarlURL(url), "GET", CIMultiDict(), YarlURL(url))
    return aiohttp.ContentTypeError(
        request_info=request_info,
        history=(),
        status=status,
        message="Attempt to decode JSON with unexpected mimetype",
    )


class _RecordingHealth:
    """Records what the polling loop reports, without any of the real logic.

    settle_after fires an event once the loop has refreshed the snapshot that
    many times, which is what lets a test stop on the very thing it asserts
    rather than on a proxy for it.
    """

    def __init__(self, settle_after=None):
        self.marked_up = []
        self.marked_down = []
        self.refreshes = 0
        self.settled = asyncio.Event()
        self._settle_after = settle_after

    def mark_up(self, name):
        self.marked_up.append(name)

    def mark_down(self, name, error=None):
        self.marked_down.append(name)

    def refresh_file(self):
        self.refreshes += 1
        if self._settle_after is not None and self.refreshes >= self._settle_after:
            self.settled.set()


class _LogCapture:
    """Collect this module's log records without printing them."""

    def __init__(self):
        from module import telegram_bot as tb_module

        self._logger = tb_module.logger
        self._handler = logging.Handler()
        self.records = []
        self._handler.emit = self.records.append
        self._level = self._logger.level

    def __enter__(self):
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        return False

    @property
    def text(self):
        return " ".join(record.getMessage() for record in self.records)


# --- The crashloop versus silent death boundary ------------------------------


async def test_a_malformed_token_is_fatal_before_a_session_is_opened():
    """A token that was never configured cannot be fixed by restarting, so this
    is the one failure allowed to terminate the process. Rejecting it before the
    session exists also means the fatal path leaks nothing."""
    bot = _make(token="your_telegram_bot_token")
    with pytest.raises(FatalConfigError):
        await bot.connect_once()
    assert bot.session is None


async def test_an_unreachable_api_is_not_fatal():
    """The other half of the same boundary. An outbound proxy may come up after
    this process does, so a connection error is a dependency that is not ready
    yet; raising FatalConfigError here would kill a process that only had to
    wait."""
    bot = _make()

    async def unreachable():
        raise aiohttp.ClientConnectionError("proxy is not up yet")

    bot.verify_connection = unreachable
    try:
        with pytest.raises(aiohttp.ClientConnectionError) as info:
            await bot.connect_once()
        assert not isinstance(info.value, FatalConfigError)
    finally:
        await bot.teardown()


async def test_a_refused_handshake_is_not_fatal():
    """The API answering with something other than a healthy getMe is also
    retryable: a token can be valid while the service is degraded."""
    bot = _make()

    async def refuse():
        return False

    bot.verify_connection = refuse
    try:
        with pytest.raises(Exception) as info:
            await bot.connect_once()
        assert not isinstance(info.value, FatalConfigError)
    finally:
        await bot.teardown()


# --- Resource release --------------------------------------------------------


async def test_a_failed_connect_still_leaves_the_session_for_teardown():
    """connect_once opens the session before it can know the API answers, so a
    failure there hands an open session to teardown. An aiohttp session that is
    never closed leaks connections on every retry."""
    bot = _make()

    async def refuse():
        return False

    bot.verify_connection = refuse
    with pytest.raises(Exception):
        await bot.connect_once()

    session = bot.session
    assert session is not None
    assert session.closed is False

    await bot.teardown()
    assert session.closed is True
    assert bot.session is None


async def test_teardown_closes_an_open_session():
    bot = _make()
    session = _FakeSession()
    bot.session = session
    await bot.teardown()
    assert session.closed is True
    assert bot.session is None


async def test_teardown_without_a_session_does_nothing():
    """teardown() runs after every failed attempt, including ones where
    connect_once() never opened anything."""
    bot = _make()
    await bot.teardown()
    assert bot.session is None
    assert bot.is_running is False


async def test_teardown_does_not_raise_when_closing_fails():
    bot = _make()

    class _BrokenSession(_FakeSession):
        async def close(self):
            raise OSError("transport already gone")

    bot.session = _BrokenSession()
    await bot.teardown()
    assert bot.session is None


# --- The run body ------------------------------------------------------------


async def test_run_parks_while_polling_succeeds():
    """The supervisor treats a run body that returns as a failed session, and a
    session shorter than SERVICE_STABLE_SECONDS as flapping. A healthy body must
    therefore never end."""
    bot = _make()
    settled = asyncio.Event()
    polls = {"n": 0}

    async def poll():
        polls["n"] += 1
        if polls["n"] >= 3:
            settled.set()
        # A real poll suspends on network I/O; the fake has to as well, or the
        # loop would never hand the event loop back.
        await asyncio.sleep(0)
        return [{"update_id": polls["n"]}]

    async def swallow(update):
        return None

    bot.get_updates = poll
    bot.process_update = swallow

    task = asyncio.create_task(bot.run())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
        assert task.done() is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_an_idle_poll_waits_instead_of_spinning():
    """An empty result is the one path that can complete an iteration without
    doing any work. Without a wait there it becomes a full-speed loop that
    hammers the API and starves everything else on the event loop."""
    bot = _make()
    polls = {"n": 0}

    async def idle():
        polls["n"] += 1
        await asyncio.sleep(0)
        return []

    bot.get_updates = idle

    task = asyncio.create_task(bot.polling_loop())
    try:
        # Yielding cannot advance a timer, so a loop that waits between empty
        # polls stays at one poll while a spinning one climbs without bound.
        for _ in range(100):
            await asyncio.sleep(0)
        assert polls["n"] == 1
        assert task.done() is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_polling_error_propagates_instead_of_being_swallowed():
    """Catching and continuing at the top level is what hid failures before:
    the component looked alive while forwarding nothing."""
    bot = _make()

    async def broken():
        raise aiohttp.ClientConnectionError("the network went away")

    bot.get_updates = broken

    with pytest.raises(aiohttp.ClientConnectionError):
        await asyncio.wait_for(bot.run(), timeout=2.0)


async def test_polling_refreshes_the_health_file_without_marking_up():
    """Reporting "up" once per poll would re-stamp the health timestamp on every
    cycle, so a component that keeps reconnecting could never accumulate enough
    continuous downtime for the watchdog to notice. Whether a session counts as
    a recovery is the supervisor's decision."""
    health = _RecordingHealth(settle_after=3)
    bot = TelegramBot(_noop_send, "123:ABC", "1", None, health=health)
    polls = {"n": 0}

    async def poll():
        polls["n"] += 1
        await asyncio.sleep(0)
        return [{"update_id": polls["n"]}]

    async def swallow(update):
        return None

    bot.get_updates = poll
    bot.process_update = swallow

    task = asyncio.create_task(bot.run())
    try:
        await asyncio.wait_for(health.settled.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert health.refreshes >= 3
    assert health.marked_up == []


async def test_activity_is_stamped_with_a_monotonic_clock():
    """Wall clock is not usable for durations here: a host without a
    battery-backed clock jumps years forward the moment time is synchronised."""
    bot = _make()
    assert bot.last_activity <= time.monotonic()


# --- Nothing in the log may carry a message -----------------------------------


async def test_an_inbound_message_is_logged_without_its_text():
    """Inbound text is a reply the operator typed, and the same handler sees
    whatever anyone sends. The channel carries one-time codes."""
    marker = "QQZZWWXX"
    bot = _make()
    replies = []

    async def capture(text, **kwargs):
        replies.append(text)
        return True

    bot.send_message = capture

    with _LogCapture() as captured:
        await bot.handle_message(marker, "1")

    assert marker not in captured.text
    # The diagnostics that replaced it must still be there.
    assert "1" in captured.text
    assert f"{len(marker)} character(s)" in captured.text
    assert replies  # the handler still ran


async def test_a_failed_send_does_not_log_the_api_response_body():
    """The response body is server controlled and, on a success, is the message
    that was just sent echoed back."""
    marker = "QQZZWWXX"
    bot = _make()
    # One attempt: the retry policy is not what this test is about, and a retry
    # would spend a real interval waiting.
    bot.max_retries = 1
    bot.session = _FakeSession(
        _FakeResponse(status=400, body=json.dumps({"description": marker}))
    )

    with _LogCapture() as captured:
        assert await bot.send_message("outbound") is False

    assert marker not in captured.text
    assert "400" in captured.text


async def test_a_successful_send_logs_only_a_length():
    marker = "QQZZWWXX"
    bot = _make()
    bot.session = _FakeSession(_FakeResponse(status=200))

    with _LogCapture() as captured:
        assert await bot.send_message(marker) is True

    assert marker not in captured.text
    assert f"{len(marker)}" in captured.text


# --- Nothing leaving this module may carry the bot token ----------------------
#
# Every request URL here is https://api.telegram.org/bot<TOKEN>/..., and the
# token grants read and write access to the chat. Exceptions raised out of this
# module are interpolated into a log line by the supervisor and by the health
# tracker, neither of which can be edited from here, so the exception itself has
# to be safe.


def _assert_carries_no_credential(rendered, token=FAKE_TOKEN):
    assert token not in rendered
    assert "api.telegram.org" not in rendered
    # A partial token is still a partial credential.
    assert token.split(":")[1][:12] not in rendered


async def test_a_failed_fetch_raises_without_the_token():
    """The regression this round fixes. get_updates used to raise a
    ClientResponseError built from the response, and that type prints
    url='...' from its own __str__. A 409 from two pollers overlapping across a
    restart, or a 401 on a revoked token, published the credential on every
    single attempt, forever, under an always-restart policy."""
    bot = _leaky()
    bot.session = _FakeSession(_FakeResponse(status=409, body="x" * 120))

    with pytest.raises(TelegramApiError) as info:
        await bot.get_updates()

    rendered = str(info.value)
    _assert_carries_no_credential(rendered)
    # The diagnostics that make it actionable are still there.
    assert "409" in rendered
    assert "120 byte" in rendered


async def test_a_url_rendering_aiohttp_error_is_not_passed_outward():
    """aiohttp raises these itself, so scrubbing only what this module
    constructs is not enough. ContentTypeError is the everyday one: a proxy
    that answers with an HTML error page makes response.json() raise it."""
    bot = _leaky()
    original = _url_rendering_error(f"{bot.base_url}getMe")
    # The fixture is genuinely dangerous, which is what makes the test mean
    # something: aiohttp's own rendering carries the credential.
    assert FAKE_TOKEN in str(original)

    bot.session = _FakeSession(_DecodeFailingResponse(original))

    with pytest.raises(TelegramApiError) as info:
        await bot.verify_connection()

    rendered = str(info.value)
    _assert_carries_no_credential(rendered)
    assert "ContentTypeError" in rendered
    assert "502" in rendered


async def test_the_sanitised_error_chains_nothing_that_carries_the_token():
    """"raise ... from None" matters here: a chained cause is still reachable
    from the exception and renders with the URL under any traceback-printing
    handler."""
    bot = _leaky()
    bot.session = _FakeSession(
        _DecodeFailingResponse(_url_rendering_error(f"{bot.base_url}getMe"))
    )

    with pytest.raises(TelegramApiError) as info:
        await bot.verify_connection()

    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


async def test_an_ordinary_connection_error_keeps_its_detail():
    """Sanitising must not blind the operator. A refused proxy is the common
    failure here and its message names the address that refused, which does not
    come from the request URL and must survive."""
    bot = _leaky()
    bot.session = _FakeSession(
        _FailingResponse(
            aiohttp.ClientConnectionError("Cannot connect to host 127.0.0.1:7890")
        )
    )

    with pytest.raises(TelegramApiError) as info:
        await bot.get_updates()

    rendered = str(info.value)
    _assert_carries_no_credential(rendered)
    assert "127.0.0.1:7890" in rendered


async def test_a_token_is_scrubbed_from_an_exception_that_carries_it():
    """The allow-list covers the types aiohttp is known to render URLs for. The
    scrub is the second guard, for a type this file did not anticipate."""
    bot = _leaky()
    bot.session = _FakeSession(
        _FailingResponse(aiohttp.ClientConnectionError(f"failed on bot{FAKE_TOKEN}/x"))
    )

    with pytest.raises(TelegramApiError) as info:
        await bot.get_updates()

    _assert_carries_no_credential(str(info.value))


async def test_an_empty_token_does_not_turn_the_scrub_into_a_rewrite():
    """str.replace with an empty needle matches between every character."""
    from module.telegram_bot import _scrub

    assert _scrub("Cannot connect to host 127.0.0.1:7890", "") == (
        "Cannot connect to host 127.0.0.1:7890"
    )


async def test_a_send_failure_is_logged_without_the_token():
    """send_message catches its own errors, so this one reaches a log line
    directly rather than through the supervisor."""
    bot = _leaky()
    bot.max_retries = 1
    bot.session = _FakeSession(
        _FailingResponse(_url_rendering_error(f"{bot.base_url}sendMessage"))
    )

    with _LogCapture() as captured:
        assert await bot.send_message("outbound") is False

    _assert_carries_no_credential(captured.text)
    assert "ContentTypeError" in captured.text


async def test_a_callback_failure_is_raised_without_the_token():
    """answer_callback_query does not catch, so its failure runs up through the
    polling loop to the supervisor's log."""
    bot = _leaky()
    bot.session = _FakeSession(
        _FailingResponse(_url_rendering_error(f"{bot.base_url}answerCallbackQuery"))
    )

    with pytest.raises(TelegramApiError) as info:
        await bot.answer_callback_query("q1", "acknowledged")

    _assert_carries_no_credential(str(info.value))


# --- Only the configured chat may reach a handler -----------------------------


async def test_a_callback_from_another_chat_is_ignored():
    """The message branch checked the chat id; the callback branch did not, so
    anyone who could press a button seeded user_state under their own id."""
    bot = _make(chat_id="1")
    handled = []

    async def record(callback_query):
        handled.append(callback_query)

    bot.process_callback_query = record

    await bot.process_update({
        "update_id": 1,
        "callback_query": {
            "id": "q1",
            "data": "cancel_sms",
            "message": {"chat": {"id": "999"}},
        },
    })

    assert handled == []
    assert bot.user_state == {}


async def test_a_callback_from_the_configured_chat_is_handled():
    """The other direction: the check must not lock out the real operator."""
    bot = _make(chat_id="1")
    handled = []

    async def record(callback_query):
        handled.append(callback_query)

    bot.process_callback_query = record

    await bot.process_update({
        "update_id": 1,
        "callback_query": {
            "id": "q1",
            "data": "cancel_sms",
            "message": {"chat": {"id": "1"}},
        },
    })

    assert len(handled) == 1


async def test_a_callback_without_a_message_is_ignored():
    """A callback need not carry a message, and the missing chat must not be
    read as a match."""
    bot = _make(chat_id="1")
    handled = []

    async def record(callback_query):
        handled.append(callback_query)

    bot.process_callback_query = record

    await bot.process_update({
        "update_id": 1,
        "callback_query": {"id": "q1", "data": "cancel_sms"},
    })

    assert handled == []


# --- notify() ------------------------------------------------------------------


async def test_notify_sends_when_the_session_is_available():
    bot = _make()
    bot.session = _FakeSession()
    sent = []

    async def capture(text, **kwargs):
        sent.append(text)
        return True

    bot.send_message = capture
    await bot.notify("component state changed")

    assert sent == ["component state changed"]


async def test_notify_swallows_a_send_failure():
    """DeviceManager.teardown() awaits this. A raise here would break the
    device's own release path, which is the one thing that must always finish."""
    bot = _make()
    bot.session = _FakeSession()

    async def boom(text, **kwargs):
        raise RuntimeError("channel is down")

    bot.send_message = boom
    await bot.notify("component state changed")

    assert bot.session.closed is False
