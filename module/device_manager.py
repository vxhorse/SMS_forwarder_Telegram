import serial
import serial_asyncio
import asyncio
import os
import time
import re
from datetime import datetime
from typing import Optional, Callable, Dict, Any
import config
from config import SMS_PORT, SMS_BAUDRATE
from logger import setup_logger
from module.discovery import discover_port
from module.supervisor import Backoff
from gsmmodem.pdu import encodeSmsSubmitPdu, decodeSmsPdu, Concatenation

logger = setup_logger(__name__)

# 长短信分片缓存的数据结构
class ConcatSmsBuffer:
    """长短信分片缓存"""
    def __init__(self, sender: str, ref_num: int, max_parts: int, timestamp: datetime):
        self.sender = sender
        self.ref_num = ref_num
        self.max_parts = max_parts
        self.timestamp = timestamp
        self.parts: Dict[int, str] = {}  # seq_num -> content
        self.first_received = datetime.now()
    
    def add_part(self, seq_num: int, content: str) -> None:
        """添加分片"""
        self.parts[seq_num] = content
    
    def is_complete(self) -> bool:
        """检查是否所有分片都已收到"""
        return len(self.parts) == self.max_parts
    
    def get_merged_content(self) -> str:
        """按序号合并所有分片内容"""
        return ''.join(self.parts[i] for i in sorted(self.parts.keys()))
    
    def is_expired(self, timeout_seconds: int = 60) -> bool:
        """检查缓存是否超时"""
        return (datetime.now() - self.first_received).total_seconds() > timeout_seconds


class DeviceManager:
    """
    设备管理类，用于检测和管理串口设备。
    """
    
    # 长短信缓存超时时间（秒）
    CONCAT_SMS_TIMEOUT = 60

    # Placeholder in the setup sequence. It is not an AT command: reaching it
    # runs _drain_stored_sms() instead.
    DRAIN_MARKER = "<DRAIN_STORED_SMS>"

    # The one destructive command in the sequence, named so the code that
    # reasons about it does not repeat the literal.
    ERASE_COMMAND = r'AT+CMGD=1,4'

    # Modem initialisation sequence.
    # One ordering constraint is load-bearing: DRAIN_MARKER must come after
    # AT+CMGF=0 and AT+CPMS (PDU mode and storage area must be selected before
    # the store can be read) and before AT+CMGD=1,4 (which erases it).
    SETUP_COMMANDS = [
        r'AT&F',                    # restore factory defaults
        r'ATE0',                    # disable echo
        r'AT+CFUN=1',               # full functionality
        r'AT+CMGF=0',               # PDU mode
        r'AT+CSCS="UCS2"',          # character set
        r'AT+CSMS=1',               # SMS service phase 2+
        r'AT+CREG=2',               # network registration URCs with location
        r'AT+CTZU=3',               # update clock and time zone from the network
        r'AT+CTZR=0',               # no time zone change reporting
        r'AT+QCFG="urc/cache",0',   # vendor specific (Quectel): no URC caching
        r'AT+QURCCFG="urcport","usbmodem"',  # vendor specific (Quectel): URC port
        r'AT+CPMS="ME","ME","ME"',  # message storage area
        DRAIN_MARKER,               # read out anything already stored, then continue
        ERASE_COMMAND,              # erase all stored messages
        r'AT+CNMI=2,2,0,0,0',       # deliver new messages straight to us
        r'AT+CSMP=17,167,0,8',      # text mode parameters, long message support
        r'AT+CSDH=1',               # verbose message headers
        r'AT+CMMS=2',               # keep the link up between messages
        r'AT&W',                    # persist settings
    ]

    # Commands the modem processes slowly enough to need a longer deadline.
    SLOW_COMMANDS = {r'AT&F', r'AT+CFUN=1', r'AT&W'}

    # Which setup failures are fatal, in full:
    #
    # A command counts as acknowledged only when its response contains OK.
    # _send_and_wait returns an empty list when the modem never answers and a
    # list ending in an error line when it refuses, so both forms of failure
    # are covered by the same test and neither is treated as success. For the
    # commands below that is fatal and setup raises; for every other command
    # it is a warning, because a module may refuse a vendor extension or a
    # convenience setting without a single message being lost.
    #
    # These four have no degraded mode. Without full functionality the radio
    # is off, without PDU mode nothing decodes, without a selected storage
    # area the store cannot be read or erased, and without new-message routing
    # nothing is ever handed to us. A process that finished setup regardless
    # would look healthy while forwarding nothing, which is the failure this
    # policy exists to prevent.
    #
    # There is a second abort path: _drain_stored_sms applies the same rule to
    # its own AT+CMGL=4. It is not listed here because it is not an entry in
    # SETUP_COMMANDS, but an unacknowledged listing is fatal for a sharper
    # reason - the next command in the sequence erases the store, so treating
    # a failed listing as an empty store destroys unread messages.
    REQUIRED_COMMANDS = {
        r'AT+CFUN=1',
        r'AT+CMGF=0',
        r'AT+CPMS="ME","ME","ME"',
        r'AT+CNMI=2,2,0,0,0',
    }

    def __init__(self, receive_sms_callback: Callable, port: Optional[str] = None, baudrate: Optional[int] = None, timeout: int = 2):
        """
        初始化设备管理器。

        :param receive_sms_callback: 接收短信时的回调函数
        :param port: 端口名称
        :param baudrate: 波特率
        :param timeout: 超时时间（秒）
        """
        
        self.receive_sms_callback = receive_sms_callback
        self.port = port or SMS_PORT
        self.baudrate = baudrate or SMS_BAUDRATE
        self.timeout = timeout
        
        self.max_retries = 3  # 最大重试次数
        self.retry_delay = 5  # 重试间隔时间（秒）
        
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        
        self.is_running = False
        self.exit_event = asyncio.Event()
        self.read_task: Optional[asyncio.Task] = None
        self.process_task: Optional[asyncio.Task] = None
        
        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.pending_sms = {"pdu": None, "expected_length": None}
        
        # 长短信分片缓存: key = (sender, ref_num)
        self.concat_sms_cache: Dict[tuple, ConcatSmsBuffer] = {}

        assert isinstance(self.baudrate, int), "波特率必须是整数类型"
        assert isinstance(self.port, str), "端口必须是字符串类型"
        
        # 验证短信是否成功发送的事件
        self.sms_sent_event = asyncio.Event()
        # 启动后的事件
        self.priming_event = asyncio.Event()

        # Injection points so tests do not have to touch the real filesystem.
        self._sleep = asyncio.sleep
        self._port_exists = os.path.exists
        self.probe_timeout = config.AT_COMMAND_TIMEOUT

    def send_at_command(self, port: str, command: str) -> Optional[list]:
        """
        通过已连接的串口发送AT指令并检查响应。

        :param port: 串口端口名称
        :param command: 要发送的AT指令
        :param retries: 重试次数，默认3次
        :return: 返回响应内容的列表，如果响应中包含期望内容，返回响应内容，否则返回None
        """
        try:
            with serial.Serial(port, baudrate=self.baudrate, timeout=self.timeout) as ser:
                for _ in range(self.max_retries):
                    # 清空缓冲区
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()

                    # 发送AT指令
                    ser.write(f'{command}\r'.encode())
                    time.sleep(0.5)
                    response = ser.read(ser.in_waiting).decode('utf-8')
                    response_parts = [part.strip() for part in response.split('\r\n') if part.strip()]
                    logger.debug(f"端口 {port} 命令 '{command}' 的响应: {response_parts}")
                    return response_parts
        except Exception as e:
            logger.warning(f"端口 {port} 命令 '{command}' 出现错误: {e}")
        return None
    
    async def send_at_command_async(self, command: str) -> None:
        """
        通过已连接的串口异步发送AT命令。
        
        :param command: 要发送的AT命令
        """
        if self.writer is None:
            raise ValueError("串口写入器未初始化")
        
        try:
            self.writer.write(f"{command}\r\n".encode())
            await self.writer.drain()
        except Exception as e:
            logger.warning(f"串口写入器发送 {command} 出现错误: {e}")
        else:
            logger.debug(f"串口写入器发送命令: {command}")

    async def resolve_port(self) -> str:
        """Return the port to use: the configured one, or a discovered one.

        An explicit setting always wins, which keeps multi-modem and unusual
        layouts working. Leaving it empty is the normal case.
        """
        if self.port:
            return self.port

        found = await discover_port(
            config.SMS_DEV_ROOT, self.baudrate, config.PORT_PROBE_TIMEOUT
        )
        if found is None:
            raise RuntimeError(
                f"No modem AT port found under {config.SMS_DEV_ROOT}; "
                f"set SMS_PORT explicitly if the device lives elsewhere"
            )
        return found

    async def _wait_for_port(self, path: str) -> None:
        """Wait for the device node to appear. No deadline, by design.

        A container can be created before its USB device finishes enumerating,
        so waiting is unbounded on purpose: there is no correct timeout for
        "the hardware is not here yet". Visibility comes from the healthcheck
        and from the notification channel instead.
        """
        backoff = Backoff(
            minimum=config.RECONNECT_BACKOFF_MIN,
            maximum=config.RECONNECT_BACKOFF_MAX,
        )
        attempts = 0
        # Every check that comes back negative is followed by a wait, and the
        # loop is the only place the node is tested, so a present node costs
        # nothing and an absent one can never fall out of the loop early.
        while not self._port_exists(path):
            attempts += 1
            delay = backoff.next_delay()
            if attempts <= 5 or attempts % 20 == 0:
                logger.warning(
                    f"Device {path} is not present yet (check {attempts}); "
                    f"retrying in {delay:.1f}s"
                )
            await self._sleep(delay)

        if attempts:
            logger.info(f"Device {path} appeared after {attempts} failed check(s)")

    async def _send_and_wait(self, command: str, timeout: float) -> list:
        """Send one AT command and read until a terminating line or timeout.

        This replaces a fixed sleep after each command, which was wrong in both
        directions: across nineteen setup commands, two seconds each spent
        thirty-eight seconds waiting on modems that had already answered, while
        still being too short for a command that happened to run long.
        """
        if self.writer is None or self.reader is None:
            raise RuntimeError("Serial connection is not open")

        self.writer.write(f"{command}\r\n".encode())
        await self.writer.drain()

        # Monotonic: this runs on boards whose wall clock jumps once the time
        # is synchronised, which would corrupt any wall-clock deadline.
        deadline = time.monotonic() + timeout
        lines: list = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(f"{command} timed out after {len(lines)} line(s)")
                return lines
            try:
                raw = await asyncio.wait_for(self.reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning(f"{command} timed out after {len(lines)} line(s)")
                return lines

            if not raw:
                # An empty read means end of stream: the device is gone. A
                # closed stream yields it again immediately every time, so
                # treating it as a blank line would spin at full speed until
                # the deadline instead of reporting the loss.
                raise RuntimeError(f"Serial connection closed while awaiting {command}")

            line = raw.strip()
            if not line:
                continue
            lines.append(line)
            if line in (b"OK", b"ERROR") or line.startswith((b"+CME ERROR", b"+CMS ERROR")):
                return lines

    async def _probe_modem(self) -> None:
        """Confirm the modem actually responds.

        A device node existing does not mean the modem is ready; enumeration
        completes before its firmware finishes starting, and AT may be silent
        for a while after the node appears.
        """
        lines = await self._send_and_wait("AT", timeout=self.probe_timeout)
        if b"OK" not in lines:
            raise RuntimeError(f"Modem did not answer AT within {self.probe_timeout}s")
        logger.info("Modem handshake succeeded")

    async def connect(self) -> None:
        """
        连接到串口设备并初始化。
        """
        retries = 0
        while retries < self.max_retries:
            try:
                self.reader, self.writer = await serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baudrate)
                await self.setup_sms()
                logger.warning(f"已连接到 {self.port}")
                break
                
            except Exception as e:
                retries += 1
                logger.warning(f"连接 {self.port} 失败（第 {retries} 次）: {e}")
                await asyncio.sleep(self.retry_delay)          
        else:
            logger.error(f"重试 {retries} 次失败，无法连接到设备 {self.port}")
            raise ValueError("无法连接到设备")
        
        self.is_running = True
        # 仅在任务不存在或已结束时创建，避免重复任务
        if self.read_task is None or self.read_task.done():
            self.read_task = asyncio.create_task(self.read_loop())
        if self.process_task is None or self.process_task.done():
            self.process_task = asyncio.create_task(self.process_loop())
    
    async def reconnect(self) -> None:
        """设备断开或出错时重新连接"""
        logger.info(f"尝试重新连接设备 {self.port}")
        await self.close()

        await asyncio.sleep(self.retry_delay)
        await self.connect()
        logger.info(f"设备 {self.port} 重新连接成功")
    
    async def setup_sms(self) -> None:
        """Run the initialisation sequence, waiting for each response.

        Read ownership matters here: this method owns the reader, and
        read_loop must only be created after it returns. Two readers on the
        same stream would race for the modem's replies.
        """
        for command in self.SETUP_COMMANDS:
            if command == self.DRAIN_MARKER:
                await self._drain_stored_sms()
                continue

            timeout = (
                config.AT_SLOW_COMMAND_TIMEOUT if command in self.SLOW_COMMANDS
                else config.AT_COMMAND_TIMEOUT
            )
            lines = await self._send_and_wait(command, timeout=timeout)
            if b"OK" in lines:
                continue

            # Silence and refusal are both failures here; see REQUIRED_COMMANDS
            # for which ones are fatal and why. Raising hands the decision to
            # the caller, which reconnects and retries.
            if command in self.REQUIRED_COMMANDS:
                raise RuntimeError(f"Modem did not acknowledge {command}")

            if command == self.ERASE_COMMAND:
                # Deliberately not fatal: the messages have already been read
                # out at this point, so the cost is duplication rather than
                # loss. It still needs saying plainly, because a store that
                # was not erased is drained again on the next reconnect and
                # every message in it is forwarded a second time.
                logger.warning(
                    "Modem did not acknowledge the erase; stored messages may "
                    "be forwarded again after the next reconnect"
                )
                continue

            logger.warning(f"{command} was not acknowledged; continuing setup")

    async def start(self) -> None:
        """
        启动设备管理器，连接到设备并开始读取数据。
        """
        try:
            await self.connect()
            self.is_running = True  # 确保设置正确的运行状态
            self.priming_event.set()
            await self.exit_event.wait()
        except Exception as e:
            logger.error(f"设备管理器启动失败: {e}")
            self.is_running = False
            self.priming_event.set()  # 设置事件避免主线程永久等待
            raise  # 向上级传递异常

    async def close(self) -> None:
        """
        关闭服务，停止所有正在运行的子任务。
        """
        logger.info("正在关闭 Device Manager 服务...")
        
        self.is_running = False
        
        # 取消读取和处理任务
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()
            try:
                await self.read_task
            except (asyncio.CancelledError, Exception) as e:
                logger.warning(f"read_task取消: {e}")
            self.read_task = None
            
        if self.process_task and not self.process_task.done():
            self.process_task.cancel()
            try:
                await self.process_task
            except (asyncio.CancelledError, Exception) as e:
                logger.warning(f"process_task取消: {e}")
            self.process_task = None
            
        # 关闭串口连接
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception as e:
                logger.warning(f"关闭写入器出错: {e}")
            self.writer = None
                
        # 设置退出事件
        self.exit_event.set()
                
        logger.info("Device Manager 服务已关闭")
    
    async def read_loop(self) -> None:
        """
        持续读取串口数据的循环
        """
        number_of_errors = 0
        while self.is_running:
            try:
                assert self.reader is not None
                line = await self.reader.readline()

                if line:
                    await self.message_queue.put(line)
                    number_of_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                number_of_errors += 1
                logger.warning(f"读取循环出错: {e}")
                await asyncio.sleep(self.retry_delay)
                
                # 如果连续出错超过阈值，标记服务为停止状态并退出
                if number_of_errors >= self.max_retries:
                    logger.error(f"读取循环连续出错 {number_of_errors} 次，停止服务")
                    self.is_running = False
                    raise RuntimeError(f"设备读取失败: {e}")
                    
        logger.warning("读取循环已关闭")
    
    async def process_loop(self) -> None:
        """处理消息队列的循环"""
        number_of_errors = 0
        while self.is_running:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=5)
                await self.process_message(message)
                number_of_errors = 0
            except asyncio.TimeoutError:
                await self.handle_incoming_sms_pdu()
                continue  # 队列为空，继续下一次循环
            except asyncio.CancelledError:
                break
            except Exception as e:
                number_of_errors += 1
                logger.error(f"处理循环出错: {e}")
                await asyncio.sleep(self.retry_delay)

                if 1 < number_of_errors < self.max_retries:
                    await self.reconnect()  # 尝试重新连接
                if number_of_errors >= self.max_retries:
                    logger.error(f"处理循环出错次数已达到 {number_of_errors} 次")
                    self.is_running = False
                    raise RuntimeError("处理循环出错")
                    
        logger.warning("处理循环已关闭")
    
    async def process_message(self, message: bytes) -> None:
        """处理单个消息"""

        if message.endswith(b'\r\n'):
            message = message[:-2].strip()

        if message.startswith(b'"') and message.endswith(b'"'):
            message = message[1:-1]
        
        if message in [b'', b' ', b'OK', b'>']:
            # 忽略没必要的内容
            return
        else:
            logger.debug(f"收到待处理的信息(处理后): {message}")

        if message.startswith(b'+CMT:'):
            await self.handle_incoming_sms_header(message)
        elif self.pending_sms["pdu"] is not None:
            await self.handle_incoming_sms_pdu(message)
        elif message.startswith(b'+CMGS:'):
            logger.info(f"短信发送成功，响应: {message.decode('utf-8')}")
            self.sms_sent_event.set()
        elif message.startswith(b'+CREG:'):
            try:
                # 解析CREG消息
                creg_msg = message.decode('utf-8')
                parts = creg_msg.replace('+CREG:', '').strip().split(',')

                # 解析各个部分
                status = parts[0].strip()
                lac = parts[1].strip(' "') if len(parts) > 1 else "Unknown"
                ci = parts[2].strip(' "') if len(parts) > 2 else "Unknown"
                act = parts[3].strip() if len(parts) > 3 else "Unknown"

                # 获取状态描述
                status_desc = {
                    "0": "未注册",
                    "1": "已注册，归属地网络",
                    "2": "未注册，正在搜索",
                    "3": "注册被拒绝",
                    "4": "未知",
                    "5": "已注册，漫游"
                }.get(status, "未知状态")

                # 获取网络类型描述
                act_desc = {
                    "0": "GSM",
                    "2": "UTRAN",
                    "3": "GSM w/EGPRS",
                    "4": "UTRAN w/HSDPA",
                    "5": "UTRAN w/HSUPA",
                    "6": "UTRAN w/HSDPA and HSUPA",
                    "7": "E-UTRAN",
                }.get(act, "Unknown")

                logger.debug(
                    f"网络注册状态更新 - 状态: {status_desc}, "
                    f"位置区: {lac}, 小区ID: {ci}, "
                    f"网络类型: {act_desc}"
                )
            except Exception as e:
                logger.debug(f"解析CREG消息失败: {e}, 原始消息: {message}")
        else:
            logger.warning(f"未处理的消息: {message}")
    
    async def handle_incoming_sms_header(self, bytes_message: bytes) -> None:
        """
        处理接收到的短信头部信息。
        
        :param bytes_message: 接收到的字节形式的消息头
        """
        # 将字节消息解码为字符串
        message = bytes_message.decode('utf-8', errors='ignore')
        
        # 使用正则表达式匹配 PDU 长度
        # 格式可能是 "+CMT: <length>" 或 "+CMT: ,<length>"
        match = re.search(r'\+CMT:\s*(?:,\s*)?(\d+)', message)
        
        if match:
            # 提取 PDU 长度
            pdu_length = int(match.group(1))
            
            # 初始化 pending_sms 字典，准备接收 PDU 数据
            self.pending_sms = {
                "pdu": b"",
                "expected_length": pdu_length
            }
            
            logger.debug(f"准备接收 {pdu_length} 字节的 PDU 数据")
        else:
            logger.warning(f"无法从消息头中解析 PDU 长度: {message}")

    async def _forward_pdu(self, pdu_hex: str, force_process: bool = False) -> bool:
        """Decode one PDU and forward it, merging concatenated parts.

        Both the live push path and the startup drain go through here so the
        two cannot drift apart.
        """
        try:
            decoded = decodeSmsPdu(pdu_hex)

            sender = decoded.get('number', 'Unknown')
            # The library exposes the service centre timestamp as 'time'. Using
            # any other key silently falls back to the local clock, which is
            # wrong by seconds in normal operation and wrong by years on a
            # machine that boots without a valid RTC. It would also defeat the
            # point of draining stored messages, since every recovered message
            # would be stamped with the moment it was recovered.
            timestamp = decoded.get('time') or datetime.now()
            timestamp_str = (
                timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(timestamp, datetime) else str(timestamp)
            )
            content = decoded.get('text', '')

            concat_info = None
            for header in decoded.get('udh', []):
                if isinstance(header, Concatenation):
                    concat_info = {
                        'ref': header.reference,
                        'max': header.parts,
                        'seq': header.number,
                    }
                    break

            if concat_info:
                logger.debug(
                    f"Concatenated message part ref={concat_info['ref']} "
                    f"seq={concat_info['seq']}/{concat_info['max']}"
                )
                await self._handle_concat_sms_part(
                    sender, timestamp, content,
                    concat_info['ref'], concat_info['max'], concat_info['seq']
                )
            else:
                logger.info(
                    f"Decoded message from {sender} at {timestamp_str}"
                    + (" (forced, may be incomplete)" if force_process else "")
                )
                await self.receive_sms_callback(sender, timestamp_str, content)
            return True

        except Exception as exc:
            # The message body must never reach the log.
            logger.error(f"Could not decode PDU: {exc}")
            return False

    async def _drain_stored_sms(self) -> int:
        """Read messages already in the modem's store and forward them.

        Anything that arrives while the process is not running lands in the
        modem's storage. Erasing the store during startup without reading it
        first means the process silently destroys those messages, which is
        exactly the window a restart is supposed to recover from.
        """
        lines = await self._send_and_wait(
            'AT+CMGL=4', timeout=config.AT_SLOW_COMMAND_TIMEOUT
        )

        if b"OK" not in lines:
            # Only an acknowledged listing proves anything about the store.
            # Silence means the modem never answered; an error line means it
            # answered that it could not list the store, which a modem whose
            # storage is still busy after a reset does routinely. Neither says
            # the store is empty, and the next command in the sequence erases
            # it, so continuing would destroy messages that were never read.
            raise RuntimeError("Modem did not acknowledge AT+CMGL=4; store left unread")

        forwarded = 0
        entries = 0
        index = 0
        while index < len(lines):
            if lines[index].startswith(b'+CMGL:') and index + 1 < len(lines):
                entries += 1
                pdu_hex = lines[index + 1].decode('ascii', errors='ignore').strip()
                # One unreadable entry must not cost us the rest of the store.
                if await self._forward_pdu(pdu_hex):
                    forwarded += 1
                index += 2
            else:
                index += 1

        if forwarded < entries:
            # These are about to be erased and cannot be recovered afterwards.
            logger.error(
                f"{entries - forwarded} of {entries} stored message(s) could "
                f"not be decoded and will not be forwarded"
            )
        if forwarded:
            logger.warning(f"Recovered {forwarded} message(s) from modem storage")
        elif not entries:
            logger.info("Modem listed an empty storage area")
        return forwarded

    async def handle_incoming_sms_pdu(self, pdu_part: bytes = b'', force_process: bool = False) -> None:
        """Accumulate a pushed PDU and forward it once it is complete.

        :param pdu_part: newly received slice of PDU data
        :param force_process: decode what has arrived even if it looks short
        """
        if self.pending_sms["pdu"] is None:
            return

        self.pending_sms["pdu"] += pdu_part

        if len(self.pending_sms["pdu"]) >= self.pending_sms["expected_length"] * 2 or force_process:
            pdu_hex = self.pending_sms["pdu"].decode('ascii', errors='ignore').strip()
            try:
                await self._forward_pdu(pdu_hex, force_process=force_process)
            finally:
                # Always reset, so one bad message cannot wedge the next one.
                self.pending_sms = {"pdu": None, "expected_length": None}
        else:
            logger.debug(
                f"PDU incomplete: {len(self.pending_sms['pdu'])} of "
                f"{self.pending_sms['expected_length'] * 2} bytes received"
            )
    
    async def _handle_concat_sms_part(
        self, sender: str, timestamp: datetime, content: str,
        ref_num: int, max_parts: int, seq_num: int
    ) -> None:
        """
        处理长短信的单个分片。
        
        :param sender: 发送者号码
        :param timestamp: 时间戳
        :param content: 分片内容
        :param ref_num: 分片引用号（用于识别属于同一条长短信的分片）
        :param max_parts: 总分片数
        :param seq_num: 当前分片序号（从1开始）
        """
        cache_key = (sender, ref_num)
        
        logger.debug(
            f"收到长短信分片 - 发送者: {sender}, 引用号: {ref_num}, "
            f"分片: {seq_num}/{max_parts}, 内容: {content[:20]}..."
        )
        
        # 清理过期的缓存
        await self._cleanup_expired_concat_cache()
        
        # 如果缓存中没有此长短信，创建新的缓存
        if cache_key not in self.concat_sms_cache:
            self.concat_sms_cache[cache_key] = ConcatSmsBuffer(
                sender=sender,
                ref_num=ref_num,
                max_parts=max_parts,
                timestamp=timestamp
            )
        
        buffer = self.concat_sms_cache[cache_key]
        buffer.add_part(seq_num, content)
        
        logger.info(
            f"长短信分片已缓存 - 发送者: {sender}, 引用号: {ref_num}, "
            f"已收到: {len(buffer.parts)}/{max_parts}"
        )
        
        # 检查是否所有分片都已收到
        if buffer.is_complete():
            merged_content = buffer.get_merged_content()
            timestamp_str = buffer.timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(buffer.timestamp, datetime) else str(buffer.timestamp)
            
            logger.info(
                f"长短信已完整合并 - 发送者: {sender}, 时间: {timestamp_str}, "
                f"分片数: {max_parts}, 完整内容: {merged_content}"
            )
            
            # 转发完整的短信
            await self.receive_sms_callback(sender, timestamp_str, merged_content)
            
            # 清理缓存
            del self.concat_sms_cache[cache_key]
    
    async def _cleanup_expired_concat_cache(self) -> None:
        """清理过期的长短信分片缓存"""
        expired_keys = [
            key for key, buffer in self.concat_sms_cache.items()
            if buffer.is_expired(self.CONCAT_SMS_TIMEOUT)
        ]
        
        for key in expired_keys:
            buffer = self.concat_sms_cache[key]
            logger.warning(
                f"长短信分片超时 - 发送者: {buffer.sender}, 引用号: {buffer.ref_num}, "
                f"已收到: {len(buffer.parts)}/{buffer.max_parts}, "
                f"丢弃未完成的分片"
            )
            # 可选：转发已收到的不完整内容
            # 这里选择丢弃，但记录日志
            del self.concat_sms_cache[key]
        
    async def handle_send_sms(self, phone_number: str, message: str) -> bool:
        """
        发送短信。

        :param phone_number: 目标电话号码
        :param message: 要发送的短信内容
        :return: 发送是否成功
        """
        logger.debug(f"准备发送短信到 {phone_number}，内容长度: {len(message)}")

        try:
            self.sms_sent_event.clear()
            
            # 1. 对用户输入进行简单检查，比如空字符串检查、号码格式检查（根据需求可更严格）
            if not phone_number.strip():
                logger.warning("目标电话号码为空，发送取消")
                return False

            # 2. 构建 PDU
            pdus = encodeSmsSubmitPdu(phone_number, message, requestStatusReport=True)
            logger.debug(f"共有 {len(pdus)} 个 PDU 需要发送")

            # 3. 逐条 PDU 发送
            for i, pdu in enumerate(pdus, 1):
                pdu_hex = pdu.data.hex().upper()
                
                smsc_length = int(pdu_hex[:2], 16)
                pdu_length = (len(pdu_hex) - (smsc_length + 1) * 2) // 2

                logger.debug(f"发送第 {i} 个 PDU，长度: {pdu_length}")

                # 4. 发送 AT+CMGS 命令
                await self.send_at_command_async(f'AT+CMGS={pdu_length}')
                await asyncio.sleep(1)  # 等待模块准备就绪

                # 发送 PDU 数据，Ctrl+Z 结尾
                logger.debug(f"发送 PDU 数据（截断显示前 20 个字符）: {pdu_hex[:20]}...")
                await self.send_at_command_async(pdu_hex + chr(26))

            # 5. 等待短信发送完成事件
            logger.info(f"已发送短信到 {phone_number}，正在等待模块发送结果...")
            await asyncio.wait_for(self.sms_sent_event.wait(), timeout=10.0)  # 等待 10 秒

            # 6. 如果执行到这里说明短信模块返回了 +CMGS: OK
            logger.info(f"短信发送成功: {phone_number}")
            return True

        except asyncio.TimeoutError:
            logger.error(f"等待短信发送结果超时: {phone_number}")
            return False
        except Exception as e:
            logger.error(f"发送短信过程出现异常: {e}", exc_info=True)
            return False
        finally:
            # 确保事件状态清理
            self.sms_sent_event.clear()
            