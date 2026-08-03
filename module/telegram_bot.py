# Telegram Bot API reference: https://core.telegram.org/bots/api
#
# Note: user-facing Telegram strings stay in the operator's language;
# only code comments and log messages are English.
#
# Nothing here may reproduce a message. This channel carries one-time codes and
# bank notifications, so log lines report counts, lengths, chat identity and
# status, never content.

import aiohttp
import asyncio
import json
import re
import html
from typing import Optional, Callable, Dict, Any
from logger import setup_logger
from module.supervisor import FatalConfigError

logger = setup_logger(__name__)


# What may leave this module inside an exception, and why.
#
# Every request built here carries the bot token in its path
# (https://api.telegram.org/bot<TOKEN>/...), and that token grants both read and
# write access to the chat. Several aiohttp exception types append the request
# URL to their own str(), so any of them escaping this module puts the token
# into whatever logs the failure - and the callers that report a failed
# component interpolate the exception they are handed.
#
# So no exception built from a request is ever allowed out. Failures leave as
# TelegramApiError, whose text this module writes itself, and any exception text
# that is echoed has the token removed from it first.


class TelegramApiError(Exception):
    """An API call failed, described without any part of the request."""


# aiohttp types that render the request URL in their own str(). These two cover
# all eight: ContentTypeError, ClientHttpProxyError, TooManyRedirects and
# WSServerHandshakeError derive from ClientResponseError, and the two
# InvalidUrl* errors derive from InvalidURL.
_URL_RENDERING_ERRORS = (aiohttp.ClientResponseError, aiohttp.InvalidURL)

# What a request can fail with. aiohttp.ClientError covers every type above.
_REQUEST_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)


def _scrub(text: str, token: str) -> str:
    """Remove the bot token from a string.

    An empty token is left alone rather than treated as absent: an empty needle
    matches between every character, which would rewrite the whole string.
    """
    if not token:
        return text
    return text.replace(token, "<token>")


class TelegramBot:
    """The Telegram component: one API session and everything on top of it.

    Supervised through the ManagedService contract (connect_once / run /
    teardown), so reconnection has exactly one owner and lives elsewhere.
    """

    # Command definitions.
    COMMANDS = {
        'start': {
            'command': '/start',
            'description': '开始使用机器人',
            'help_text': '显示欢迎消息并初始化机器人'
        },
        'help': {
            'command': '/help',
            'description': '显示帮助信息',
            'help_text': '显示所有可用的命令和使用说明'
        },
        'sendsms': {
            'command': '/sendsms',
            'description': '发送短信',
            'help_text': '开始发送短信的流程'
        }
    }

    # Inline keyboard layouts.
    KEYBOARD_LAYOUTS = {
        'cancel': [
            [{'text': '✖️ 取消操作', 'callback_data': 'cancel_sms'}]
        ],
        'sms_reply': [
            [
                {'text': '↩️ 回复', 'callback_data': 'reply_{}'},
            ]
        ]
    }

    def __init__(self, send_sms_callback: Callable, bot_token: str, chat_id: str,
                 proxy_url: Optional[str], health=None):
        """
        Set up the Telegram component.

        :param send_sms_callback: called to send a message through the modem
        :param bot_token: Telegram Bot API token
        :param chat_id: the chat this bot talks to
        :param proxy_url: outbound proxy, or None
        :param health: HealthState whose snapshot file this keeps fresh
        """
        self.send_sms_callback = send_sms_callback
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy_url = proxy_url
        self.base_url = f'https://api.telegram.org/bot{self.bot_token}/'

        # Connection parameters.
        self.max_retries = 3
        self.retry_delay = 5
        self.timeout = aiohttp.ClientTimeout(total=60)

        # Running state.
        self.is_running = False

        # Session management.
        self.session: Optional[aiohttp.ClientSession] = None
        self.session_lock = asyncio.Lock()
        self.offset = 0
        self.user_state: Dict[str, Dict[str, Any]] = {}

        self.name = "telegram"
        self.health = health

    def _describe_error(self, exc: BaseException) -> str:
        """Describe a failure without reproducing any part of the request.

        For a type that renders the URL, only the class name and the status are
        echoed; both come from aiohttp's own vocabulary rather than from the
        request, which is what makes them safe. Every other exception keeps its
        message, because that is where the useful detail lives - a refused proxy
        names the address it could not reach - with the token removed in case
        one ever carries it. The allow-list decides what may be quoted; the
        scrub is a second guard for types not anticipated here.
        """
        detail = type(exc).__name__
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            detail = f"{detail} (HTTP {status})"
        if not isinstance(exc, _URL_RENDERING_ERRORS):
            text = _scrub(str(exc), self.bot_token)
            if text:
                detail = f"{detail}: {text}"
        return detail

    def validate_config(self) -> None:
        """Reject placeholder or missing credentials.

        This raises FatalConfigError rather than a retryable error because no
        amount of restarting will supply a token that was never configured.
        """
        if not self.bot_token or self.bot_token == "your_telegram_bot_token":
            raise FatalConfigError("BOT_TOKEN is not configured")
        if not self.chat_id or self.chat_id == "your_telegram_chat_id":
            raise FatalConfigError("CHAT_ID is not configured")

    async def connect_once(self) -> None:
        """Establish one session. Failure raises; the supervisor backs off.

        An outbound proxy may well come up after this process does, so an
        unreachable API is a dependency that is not ready yet, not a reason to
        terminate. The configuration check runs first, before anything is
        opened, so the one path that does terminate holds no resources.
        """
        self.validate_config()
        async with self.session_lock:
            if self.session is not None and not self.session.closed:
                await self.session.close()
            self.session = aiohttp.ClientSession(timeout=self.timeout)

        if not await self.verify_connection():
            raise ConnectionError("Telegram API is not reachable")

        await self.setup_commands()
        self.is_running = True
        logger.info("Connected to the Telegram API")

    async def run(self) -> None:
        """Long polling. Errors propagate so the supervisor reconnects."""
        await self.polling_loop()

    async def teardown(self) -> None:
        """Release the session. Idempotent, never raises."""
        self.is_running = False
        try:
            if self.session is not None and not self.session.closed:
                await self.session.close()
        except Exception as exc:
            logger.warning(f"Error closing the Telegram session: {self._describe_error(exc)}")
        self.session = None

    async def notify(self, text: str) -> None:
        """Report component state outward. Silent when the channel is down."""
        if self.session is None or self.session.closed:
            logger.debug("Telegram session unavailable, dropping notification")
            return
        try:
            await self.send_message(text)
        except Exception as exc:
            logger.warning(f"Could not send notification: {self._describe_error(exc)}")

    async def setup_commands(self) -> None:
        """Publish the bot's command list."""
        commands = [
            {
                "command": cmd_info['command'].strip('/'),
                "description": cmd_info['description']
            }
            for cmd_info in self.COMMANDS.values()
        ]

        url = f'{self.base_url}setMyCommands'
        try:
            async with self.session.post(url, json={'commands': commands}, proxy=self.proxy_url) as response:
                if response.status == 200:
                    logger.info("Command list published")
                else:
                    logger.error(f"Could not publish the command list: HTTP {response.status}")
        except Exception as e:
            # Not fatal to the session: a bot whose command menu is stale still
            # forwards messages.
            logger.error(f"Error publishing the command list: {self._describe_error(e)}")

    async def verify_connection(self) -> bool:
        """Confirm the API answers for this token."""
        url = f'{self.base_url}getMe'

        try:
            async with self.session.get(url, proxy=self.proxy_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        # The bot's own identity, not anyone's message.
                        username = data.get('result', {}).get('username')
                        logger.debug(f"Connected to Telegram bot: {username}")
                        return True
                logger.error(f"Connection check failed: HTTP {response.status}")
        except _REQUEST_ERRORS as exc:
            # Detached from the original with "from None" so no traceback or
            # chained context can carry the URL onward either. The response body
            # is decoded inside this block on purpose: a proxy answering with an
            # HTML error page makes json() raise ContentTypeError, which renders
            # the URL.
            raise TelegramApiError(
                f"getMe failed: {self._describe_error(exc)}"
            ) from None
        return False

    async def polling_loop(self) -> None:
        """Long polling loop. Any error propagates to the supervisor.

        Nothing is retried here. Counting errors and backing off is the
        supervisor's job, and a second copy of that policy inside this loop
        would compound with it: two delays multiplied together can stall the
        bot for far longer than either was meant to allow.
        """
        logger.info("Long polling started")
        self.is_running = True

        while self.is_running:
            updates = await self.get_updates()
            for update in updates:
                await self.process_update(update)
            # Refreshing only, never mark_up(): see Supervisor._serve_session
            # for why that decision cannot be made from inside a component's
            # own loop.
            if self.health is not None:
                self.health.refresh_file()
            if not updates:
                # The one path that can finish an iteration having done no
                # work. Without this wait an API that answers instantly turns
                # the loop into a full-speed one that never yields.
                await asyncio.sleep(2)

        logger.info("Long polling stopped")

    async def get_updates(self) -> list:
        """Fetch pending updates from the API."""
        url = f'{self.base_url}getUpdates'
        params = {'offset': self.offset, 'timeout': 50}
        try:
            async with self.session.get(url, params=params, proxy=self.proxy_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('result', [])
                # The body is not quoted: an error reply is an unbounded
                # server-controlled string, and its size is enough to tell an
                # empty answer from a real one. Raising this rather than letting
                # aiohttp raise for status matters just as much: a
                # ClientResponseError carries the request it came from and
                # prints the URL, so a routine 409 or 401 would publish the
                # token on every attempt.
                error_text = await response.text()
                raise TelegramApiError(
                    f"Could not fetch updates: HTTP {response.status}, "
                    f"{len(error_text)} byte reply"
                )
        except _REQUEST_ERRORS as exc:
            raise TelegramApiError(
                f"Could not fetch updates: {self._describe_error(exc)}"
            ) from None

    async def process_update(self, update: dict) -> None:
        """Route one update, once it is known to come from the configured chat.

        Authorisation lives here, at the routing point, so every branch that can
        reach a handler is guarded by the same check in one readable place.
        """
        self.offset = max(self.offset, update['update_id'] + 1)

        if 'message' in update and 'text' in update['message']:
            message = update['message']
            chat_id = str(message['chat']['id'])
            if chat_id == self.chat_id:
                await self.handle_message(message['text'], chat_id)
            else:
                logger.warning(f"Message from an unauthorised chat: {chat_id}")
        elif 'callback_query' in update:
            callback_query = update['callback_query']
            # A button press is as unauthenticated as a message: anyone who can
            # reach the bot can send one, and the handler writes to user_state
            # under the presser's own chat id. Navigated with get() because a
            # callback need not carry a message at all.
            chat_id = str(callback_query.get('message', {}).get('chat', {}).get('id'))
            if chat_id == self.chat_id:
                await self.process_callback_query(callback_query)
            else:
                logger.warning(f"Callback from an unauthorised chat: {chat_id}")
        else:
            # Field names only. Their values may hold a message body.
            logger.warning(f"Update of an unhandled kind: {sorted(update.keys())}")

    async def handle_message(self, text: str, chat_id: str) -> None:
        """
        Handle one inbound message.

        :param text: the message text
        :param chat_id: the chat it came from
        """
        # The text itself never reaches the log: an inbound message may be a
        # forwarded code being replied to, or anything else a person typed.
        # Chat identity and length are enough to follow the flow.
        logger.info(f"Message received from {chat_id}: {len(text)} character(s)")

        if text.startswith('/'):
            await self.handle_command(text, chat_id)
        else:
            await self.handle_sms_input(text, chat_id)

    async def handle_command(self, text: str, chat_id: str) -> None:
        """
        Handle a slash command.

        :param text: the command text
        :param chat_id: the chat it came from
        """
        command = text.split()[0].lower().lstrip('/')

        if command in [cmd_info['command'].lstrip('/') for cmd_info in self.COMMANDS.values()]:
            if command == 'start':
                await self.send_welcome_message()
            elif command == 'help':
                await self.send_help_message()
            elif command == 'sendsms':
                await self.send_number_reception(chat_id)
        else:
            await self.send_message("🤔 未知命令。请使用 /help 查看可用命令。")

    async def handle_sms_input(self, text: str, chat_id: str) -> None:
        """Advance the guided send flow with whatever the user typed."""
        state = self.user_state.get(chat_id, {}).get('state')
        if state == 'awaiting_number':
            await self.handle_phone_number_input(text, chat_id)
        elif state == 'awaiting_content':
            await self.handle_sms_content_input(text, chat_id)
        else:
            await self.send_message("请使用 /sendsms 命令开始发送短信。")

    async def handle_phone_number_input(self, text: str, chat_id: str) -> None:
        """Accept a destination number, or ask for it again."""
        if not re.match(r'^\+?[0-9]+$', text):
            await self.send_message(
                "🔃 无效的电话号码。请输入正确的电话号码，仅包含数字和可选的前导加号。请重新输入：",
                reply_markup=self.get_keyboard('cancel')
            )
            return

        self.user_state[chat_id] = {'state': 'awaiting_content', 'number': text}
        await self.send_message(
            f"📄 电话号码已记录。请输入短信内容：",
            reply_markup=self.get_keyboard('cancel')
        )

    async def handle_sms_content_input(self, text: str, chat_id: str) -> None:
        """Send what the user typed through the modem."""
        number = self.user_state[chat_id]['number']
        success = await self.send_sms_callback(number, text)
        result_message = "✅ 短信已成功发送" if success else "❌ 发送短信失败"

        await self.send_message(
            result_message,
            reply_markup=self.get_keyboard('sms_reply', phone=number)
        )
        self.user_state.pop(chat_id, None)

    async def handle_forwarding_sms(self, phone_number: str, timestamp: str, content: str) -> bool:
        """
        Forward one received message to Telegram.

        :param phone_number: sender number
        :param timestamp: the carrier's timestamp
        :param content: the message text
        :return: whether the forward succeeded
        """
        safe_phone_number = html.escape(phone_number)

        message = (
            f"📩 <b>收到新短信</b>\n"
            f"📞 <b>发送者</b>: <code>{safe_phone_number}</code>\n"
            f"🕒 <b>时间</b>: {timestamp}\n"
            f"📄 <b>内容</b>:\n{html.escape(content)}"
        )

        reply_markup = self.get_keyboard('sms_reply', phone=safe_phone_number)

        return await self.send_message(
            message,
            parse_mode='HTML',
            reply_markup=reply_markup,
            disable_notification=False  # a received message should make a sound
        )

    async def process_callback_query(self, callback_query: dict) -> None:
        """Handle a button press."""
        query_id = str(callback_query['id'])
        chat_id = str(callback_query['message']['chat']['id'])
        data = callback_query['data']

        if data == 'cancel_sms':
            await self.cancel_operation(chat_id)
            await self.answer_callback_query(query_id, "已取消当前操作")
        elif data.startswith('reply_'):
            number = data.split('_')[1]
            self.user_state[chat_id] = {'state': 'awaiting_content', 'number': number}
            await self.answer_callback_query(query_id, "请输入回复内容")
            await self.send_message(
                f"📄 请输入要回复给 {number} 的短信内容：",
                reply_markup=self.get_keyboard('cancel')
            )
        else:
            # Callback data is written by this file and capped at 64 bytes by
            # the API, so it can never carry a message body.
            logger.warning(f"Unexpected callback data: {data}")

    async def cancel_operation(self, chat_id: str) -> None:
        """Drop any in-progress send flow for one chat."""
        assert isinstance(chat_id, str)
        if chat_id in self.user_state:
            del self.user_state[chat_id]
        await self.send_message("❎️ 已取消操作。")

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        """Acknowledge a button press so the client stops showing a spinner."""
        url = f'{self.base_url}answerCallbackQuery'
        data = {
            'callback_query_id': callback_query_id,
            'text': text
        }
        try:
            async with self.session.post(url, json=data, proxy=self.proxy_url) as response:
                if response.status != 200:
                    logger.warning(f'Could not answer a callback query: HTTP {response.status}')
        except _REQUEST_ERRORS as exc:
            # Propagates on purpose: this runs inside the polling loop, so a
            # session broken here has to reach the supervisor rather than being
            # swallowed by a button press.
            raise TelegramApiError(
                f"Could not answer a callback query: {self._describe_error(exc)}"
            ) from None

    async def send_welcome_message(self) -> None:
        """Send the welcome text."""
        welcome_message = (
            "👋 <b>欢迎使用 SMS 转发 Bot！</b>\n\n"
            "这个机器人可以帮助你发送短信和接收转发的短信。\n"
            "使用下方按钮或输入 /help 查看可用的功能。"
        )
        await self.send_message(
            welcome_message,
            parse_mode='HTML',
        )

    async def send_help_message(self) -> None:
        """Send the command reference."""
        help_sections = []
        for cmd_info in self.COMMANDS.values():
            help_sections.append(
                f"{cmd_info['command']} - {cmd_info['help_text']}"
            )

        help_message = (
                "<b>📚 可用命令:</b>\n\n" +
                "\n".join(help_sections) +
                "\n\n<i>更多功能开发中...</i>"
        )

        await self.send_message(
            help_message,
            parse_mode='HTML',
            disable_notification=True
        )

    async def send_number_reception(self, chat_id: str) -> None:
        """
        Start the guided send flow by asking for a destination number.

        :param chat_id: the chat to prompt
        """
        self.user_state[chat_id] = {'state': 'awaiting_number'}
        await self.send_message(
            "📱 请输入接收短信的电话号码：",
            reply_markup=self.get_keyboard('cancel'),
            disable_notification=True
        )

    async def send_message(self,
                           message: str,
                           parse_mode: Optional[str] = None,
                           reply_markup: Optional[dict] = None,
                           disable_notification: bool = False,
                           protect_content: bool = False) -> bool:
        """
        Send one message to the configured chat.

        :param message: the text to send
        :param parse_mode: HTML or Markdown, or None for plain text
        :param reply_markup: inline keyboard to attach
        :param disable_notification: deliver without a sound
        :param protect_content: forbid forwarding and saving
        :return: whether the message was accepted
        """
        url = f'{self.base_url}sendMessage'
        data = {
            'chat_id': self.chat_id,
            'text': message,
            'disable_notification': disable_notification,
            'protect_content': protect_content
        }

        if parse_mode:
            data['parse_mode'] = parse_mode
        if reply_markup:
            data['reply_markup'] = json.dumps(reply_markup)

        for attempt in range(self.max_retries):
            try:
                async with self.session.post(url, json=data, proxy=self.proxy_url) as response:
                    if response.status == 200:
                        logger.debug(f'Message sent, {len(message)} character(s)')
                        return True

                    logger.warning(
                        f'Send failed (attempt {attempt + 1}/{self.max_retries}): '
                        f'HTTP {response.status}'
                    )
                    # Read but never quoted. A successful reply echoes the whole
                    # message that was just sent, and an error reply is an
                    # unbounded server-controlled string; the size is enough to
                    # tell an empty answer from a real one. Draining it also
                    # lets the connection be reused.
                    body = await response.text()
                    logger.debug(f'Error reply: {len(body)} byte(s)')

            except Exception as e:
                logger.error(
                    f'Error sending a message '
                    f'(attempt {attempt + 1}/{self.max_retries}): '
                    f'{self._describe_error(e)}'
                )

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay)

        logger.error(f'Send failed after {self.max_retries} attempt(s)')
        return False

    def get_keyboard(self, keyboard_type: str, **kwargs) -> dict:
        """
        Build one of the keyboard layouts.

        :param keyboard_type: key into KEYBOARD_LAYOUTS
        :param kwargs: values substituted into button callback data
        :return: the inline keyboard, or an empty dict on failure
        """
        try:
            keyboard = self.KEYBOARD_LAYOUTS.get(keyboard_type, [])
            if not keyboard:
                logger.warning(f"No such keyboard layout: {keyboard_type}")
                return {}

            # Fill in the buttons that carry a placeholder.
            formatted_keyboard = []
            for row in keyboard:
                formatted_row = []
                for button in row:
                    new_button = button.copy()
                    if '{}' in button['callback_data']:
                        new_button['callback_data'] = button['callback_data'].format(*kwargs.values())
                    formatted_row.append(new_button)
                formatted_keyboard.append(formatted_row)

            return {'inline_keyboard': formatted_keyboard}
        except Exception as e:
            logger.error(f"Error building a keyboard layout: {self._describe_error(e)}")
            return {}
