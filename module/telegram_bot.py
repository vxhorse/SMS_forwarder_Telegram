# 官方文档 https://core.telegram.org/bots/api

import aiohttp
import asyncio
import json
import re
import html
import time
from typing import Optional, Callable, Dict, Any
from logger import setup_logger

logger = setup_logger(__name__)

class TelegramBot:
    # 命令配置
    COMMANDS = {
        'start': {
            'command': '/start',
            'description': '开始使用机器人',
            'help_text': '显示欢迎消息并初始化机器人',
            'keyboard_text': '🏠 主菜单'
        },
        'help': {
            'command': '/help',
            'description': '显示帮助信息',
            'help_text': '显示所有可用的命令和使用说明',
            'keyboard_text': '💁 帮助'
        },
        'sendsms': {
            'command': '/sendsms',
            'description': '发送短信',
            'help_text': '开始发送短信的流程',
            'keyboard_text': '📲 发送短信'
        }
    }

    # 默认键盘配置
    KEYBOARD_LAYOUTS = {
        # 'main_menu': [
        #     [
        #         {'text': COMMANDS['sendsms']['keyboard_text'], 
        #          'callback_data': 'sendsms'}
        #     ],
        #     [
        #         {'text': COMMANDS['help']['keyboard_text'], 
        #          'callback_data': 'help'}
        #     ]
        # ],
        'cancel': [
            [{'text': '✖️ 取消操作', 'callback_data': 'cancel_sms'}]
        ],
        'sms_reply': [
            [
                {'text': '↩️ 回复', 'callback_data': 'reply_{}'},
                # {'text': '🚫 屏蔽', 'callback_data': 'block_{}'}
            ]
        ]
    }

    def __init__(self, send_sms_callback: Callable, bot_token: str, chat_id: str, proxy_url: Optional[str]):
        """
        初始化 Telegram Bot 实例
        
        Args:
            send_sms_callback: 用于发送短信的回调函数
            bot_token: Telegram Bot API token
            chat_id: 目标聊天 ID
            proxy_url: 代理服务器地址(可选)
        """
        # 基础配置
        self.send_sms_callback = send_sms_callback
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy_url = proxy_url
        self.base_url = f'https://api.telegram.org/bot{self.bot_token}/'

        # 连接参数
        self.max_retries = 3
        self.retry_delay = 5
        self.timeout = aiohttp.ClientTimeout(total=60)

        # 运行状态
        self.is_running = False
        self.last_activity = time.time()
        self.exit_event = asyncio.Event()
        self.polling_task: Optional[asyncio.Task] = None

        # 会话管理
        self.session: Optional[aiohttp.ClientSession] = None
        self.session_lock = asyncio.Lock()
        self.offset = 0
        self.user_state: Dict[str, Dict[str, Any]] = {}

    async def connect(self) -> None:
        """连接到 Telegram API 并初始化机器人"""
        logger.info("正在连接到 Telegram API...")
        try:
            async with self.session_lock:
                self.session = aiohttp.ClientSession(timeout=self.timeout)

            if await self.verify_connection():
                await self.setup_commands()
                await self.send_welcome_message()
                self.is_running = True
                self.polling_task = asyncio.create_task(self.polling_loop())
                logger.info("成功连接到 Telegram API")
            else:
                raise ConnectionError("无法连接到 Telegram API")
        except Exception as e:
            logger.error(f"连接时发生错误: {e}")
            await self.close()

    async def reconnect(self) -> bool:
        """重新连接到 Telegram API
        """
        logger.info("尝试重新连接到 Telegram API...")
        try:
            async with self.session_lock:
                # 确保旧的session已关闭
                if self.session is not None and not self.session.closed:
                    await self.session.close()

                # 创建新的session
                self.session = aiohttp.ClientSession(timeout=self.timeout)
            return await self.verify_connection()
        except Exception as e:
            logger.error(f"重新连接时发生错误: {e}")
            return False

    async def setup_commands(self) -> None:
        """设置机器人的命令列表"""
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
                    logger.info("已成功设置机器人命令列表")
                else:
                    logger.error(f"设置命令列表失败: {response.status}")
        except Exception as e:
            logger.error(f"设置命令列表时发生错误: {e}")

    async def verify_connection(self) -> bool:
        """验证与 Telegram API 的连接
        """
        url = f'{self.base_url}getMe'

        async with self.session.get(url, proxy=self.proxy_url) as response:
            if response.status == 200:
                data = await response.json()
                if data.get('ok'):
                    logger.debug(f"成功连接到 Telegram Bot: {data['result']['username']}")
                    return True
        logger.error(f"检查连接失败，状态码: {response.status if response.status else 'empty'}")
        return False

    async def start(self) -> None:
        """
        启动 Telegram Bot 服务。
        """
        try:
            # 连接到 Telegram API
            await self.connect()
            # 等待退出事件
            await self.exit_event.wait()
        except Exception as e:
            logger.error(f"Telegram Bot 服务启动失败: {e}")
            self.is_running = False
            raise  # 向上级传递异常

    async def close(self) -> None:
        """关闭 Telegram Bot 连接"""
        logger.info("正在关闭 Telegram Bot 连接...")

        self.is_running = False
        try:
            # 取消轮询任务
            if self.polling_task and not self.polling_task.done():
                try:
                    # 使用cancel()而不是直接等待任务完成
                    self.polling_task.cancel()
                    # 记录日志但不等待任务
                    logger.warning("轮调任务被取消")
                except Exception as e:
                    logger.error(f"取消轮调任务时出错: {e}")
                finally:
                    self.polling_task = None
            
            # 关闭会话
            if self.session and not self.session.closed:
                try:
                    await asyncio.wait_for(self.session.close(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("关闭会话超时")
                except Exception as e:
                    logger.error(f"关闭会话时出错: {e}")
            
            # 设置退出事件
            self.exit_event.set()
        except Exception as e:
            logger.error(f"关闭连接时出现错误: {e}")
        finally:
            # 无论如何确保标记服务为已关闭
            self.is_running = False
            logger.info("Telegram Bot 连接已关闭")

    async def polling_loop(self) -> None:
        """
        长轮调循环，持续检查新的更新
        """
        if self.is_running:
            logger.warning("长轮询循环polling_loop已启动")

        max_consecutive_errors = 5  # 最大连续错误次数
        consecutive_errors = 0  # 当前连续错误次数

        while self.is_running:
            try:
                # 获取更新
                updates = await self.get_updates()

                if not updates:
                    # 如果没有更新，则稍作等待以避免频繁请求
                    await asyncio.sleep(2)
                for update in updates:
                    await self.process_update(update)

                # 更新最后活动时间，重置错误计数
                self.last_activity = time.time()
                consecutive_errors = 0
            except (asyncio.TimeoutError,
                    aiohttp.ClientConnectionError,
                    aiohttp.ClientResponseError) as e:
                consecutive_errors += 1
                logger.error(f"轮调过程中出现网络错误: {type(e).__name__}")

                # 如果连续错误次数超过阈值，停止服务
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"连续网络错误达到 {consecutive_errors} 次，停止服务")
                    self.is_running = False
                    # 确保抛出异常让上层知道服务已停止
                    raise RuntimeError(f"Telegram API 连接失败: {type(e).__name__}")
                
                # 否则尝试等待并重新连接
                retry_delay = min(30, consecutive_errors * 5)
                logger.debug(f"将在 {retry_delay} 分钟后重试连接")
                await asyncio.sleep(retry_delay * 60)
                
                if not await self.reconnect():
                    logger.error("重连失败，停止服务")
                    self.is_running = False
                    # 确保抛出异常让上层知道服务已停止
                    raise RuntimeError("Telegram API 重连失败")
            except asyncio.CancelledError:
                logger.warning("轮调任务被取消")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"轮调过程中出现未知错误: {e}")
                
                # 如果连续错误次数超过阈值，停止服务
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"连续未知错误达到 {consecutive_errors} 次，停止服务")
                    self.is_running = False
                    raise RuntimeError(f"Telegram Bot 服务出错: {e}")
                    
                await asyncio.sleep(10)

        logger.warning("长轮询循环polling_loop已关闭")

    async def get_updates(self) -> list:
        """
        从 Telegram API 获取更新
        """
        url = f'{self.base_url}getUpdates'
        params = {'offset': self.offset, 'timeout': 50}
        async with self.session.get(url, params=params, proxy=self.proxy_url) as response:
            if response.status == 200:
                data = await response.json()
                return data.get('result', [])
            else:
                error_text = await response.text()
                # 对于非 200 状态码，使用 ClientResponseError 更合适
                raise aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=response.status,
                    message=f"无法获取更新，状态码: {response.status}, 响应: {error_text}"
                )

    async def process_update(self, update: dict) -> None:
        """处理单个更新
        """
        self.offset = max(self.offset, update['update_id'] + 1)

        if 'message' in update and 'text' in update['message']:
            message = update['message']
            chat_id = str(message['chat']['id'])
            if chat_id == self.chat_id:
                await self.handle_message(message['text'], chat_id)
            else:
                logger.warning(f"收到来自未授权用户的消息: chat_id {chat_id}")
        elif 'callback_query' in update:
            await self.process_callback_query(update['callback_query'])
        else:
            logger.warning(f"收到未知类型的更新: {update.keys()}")

    async def handle_blocking(self) -> None:
        """关闭连接并尝试重新创建长轮询携程"""
        logger.warning("尝试重新创建长轮询携程...")

        # 尝试取消当前的轮询任务
        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                logger.warning("卡住的轮询任务已成功取消")

        # 重新连接
        if await self.reconnect():
            # 如果成功重建连接，重新启动轮询
            logger.info("重新连接成功，重新启动轮询任务")
            self.polling_task = asyncio.create_task(self.polling_loop())
        else:
            logger.error("重新连接失败，关闭 Telegram Bot")
            await self.close()

    async def handle_message(self, text: str, chat_id: str) -> None:
        """
        处理收到的消息
        
        Args:
            text: 消息文本
            chat_id: 聊天ID
        """
        logger.info(f"收到来自 {chat_id} 的消息: {text}")

        if text.startswith('/'):
            await self.handle_command(text, chat_id)
        else:
            await self.handle_sms_input(text, chat_id)

    async def handle_command(self, text: str, chat_id: str) -> None:
        """
        处理命令消息
        
        Args:
            text: 命令文本
            chat_id: 聊天ID
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
        """处理短信输入流程
        """
        state = self.user_state.get(chat_id, {}).get('state')
        if state == 'awaiting_number':
            await self.handle_phone_number_input(text, chat_id)
        elif state == 'awaiting_content':
            await self.handle_sms_content_input(text, chat_id)
        else:
            await self.send_message("请使用 /sendsms 命令开始发送短信。")

    async def handle_phone_number_input(self, text: str, chat_id: str) -> None:
        """处理电话号码输入"""
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
        """处理短信内容输入"""
        number = self.user_state[chat_id]['number']
        success = await self.send_sms_callback(number, text)
        result_message = "✅ 短信已成功发送" if success else "❌ 发送短信失败"

        await self.send_message(
            result_message,
            reply_markup=self.get_keyboard('sms_reply', phone=number)
        )
        self.user_state.pop(chat_id, None)

    # 判断广告和回复T
    async def handle_forwarding_sms(self, phone_number: str, timestamp: str, content: str) -> bool:
        """
        处理接收到的短信并转发到 Telegram
        
        Args:
            phone_number: 发送者号码
            timestamp: 发送时间戳
            content: 短信内容
            
        Returns:
            bool: 转发是否成功
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
            disable_notification=False  # 收到短信时启用通知提示音
        )

    async def process_callback_query(self, callback_query: dict) -> None:
        """处理回调查询"""
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
            logger.warning(f"回调查询出现意外参数: {data}")

    async def cancel_operation(self, chat_id: str) -> None:
        """取消当前操作
        """
        assert isinstance(chat_id, str)
        if chat_id in self.user_state:
            del self.user_state[chat_id]
        await self.send_message("❎️ 已取消操作。")

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        """回答回调查询"""
        url = f'{self.base_url}answerCallbackQuery'
        data = {
            'callback_query_id': callback_query_id,
            'text': text
        }
        async with self.session.post(url, json=data, proxy=self.proxy_url) as response:
            if response.status != 200:
                logger.warning(f'回答回调查询失败，状态码: {response.status}')

    async def send_welcome_message(self) -> None:
        """发送欢迎消息"""
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
        """发送帮助消息"""
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
        开始发送短信流程
        
        Args:
            chat_id: 聊天ID
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
        发送消息到 Telegram
        
        Args:
            message: 消息内容
            parse_mode: 解析模式(HTML/Markdown)
            reply_markup: 回复键盘标记
            disable_notification: 是否禁用通知声音
            protect_content: 是否禁止转发
            
        Returns:
            bool: 消息是否发送成功
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
                        logger.debug(f'消息发送成功, 长度: {len(message)} 字符')
                        return True

                    logger.warning(f'发送消息失败 (尝试 {attempt + 1}/{self.max_retries}): HTTP {response.status}')
                    response_text = await response.text()
                    logger.debug(f'API响应: {response_text}')

            except Exception as e:
                logger.error(f'发送消息时发生错误 (尝试 {attempt + 1}/{self.max_retries}): {str(e)}')

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay)

        logger.error(f'发送消息最终失败, 已重试 {self.max_retries} 次')
        return False

    def get_keyboard(self, keyboard_type: str, **kwargs) -> dict:
        """
        获取指定类型的键盘布局
        
        Args:
            keyboard_type: 键盘类型
            **kwargs: 用于格式化按钮文本的参数
            
        Returns:
            dict: 键盘配置
        """
        try:
            keyboard = self.KEYBOARD_LAYOUTS.get(keyboard_type, [])
            if not keyboard:
                logger.warning(f"未找到键盘类型: {keyboard_type}")
                return {}

            # 处理需要格式化的按钮
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
            logger.error(f"创建键盘布局时发生错误: {e}")
            return {}

    @staticmethod
    def create_inline_keyboard(buttons: list[list[str]], callback_data: list[list[str]]) -> dict:
        """创建内联键盘，确保按钮与回调数据匹配
        """
        if len(buttons) != len(callback_data):
            raise ValueError("按钮和回调数据的行数不一致，请确保它们的数量相同。")

        inline_keyboard = [
            [{'text': text, 'callback_data': data} for text, data in zip(row_buttons, row_data)]
            for row_buttons, row_data in zip(buttons, callback_data)
        ]

        return {'inline_keyboard': inline_keyboard}
