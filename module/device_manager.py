import serial
import serial_asyncio
import asyncio
import time
import re
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any
from config import SMS_PORT, SMS_BAUDRATE
from logger import setup_logger
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
        """
        配置SMS相关设置，包括文本模式、字符集等。
        """
        
        # 计算时间
        current_time = datetime.now()
        modified_time = current_time - timedelta(hours=2)
        modified_time += timedelta(seconds=2 * 9)
        formatted_time = modified_time.strftime(r'AT+CCLK="%y/%m/%d,%H:%M:%S+08"')
        
        commands = [
            r'AT&F',                    # 恢复出厂设置
            r'ATE0',                    # 关闭回显
            r'AT+CFUN=1',               # 设置为全功能模式
            r'AT+CMGF=0',               # 设置短信格式为PDU模式
            r'AT+CSCS="UCS2"',          # 设置字符集
            r'AT+CSMS=1',               # 设置短信服务为Phase 2+
            r'AT+CREG=2',               # 启用网络注册和位置信息URC
            r'AT+CTZU=3',               # 启用通过NITZ自动更新时区和本地时间到RTC
            r'AT+CTZR=0',               # 禁用时区变更报告
            formatted_time,             # 设置模块时间
            r'AT+QCFG="urc/cache",0',   # 关闭 URC 缓存
            r'AT+QURCCFG="urcport","usbmodem"',
            r'AT+CPMS="ME","ME","ME"',  # 设置短信存储位置
            r'AT+CMGD=1,4',             # 删除所有短信
            r'AT+CNMI=2,2,0,0,0',       # 设置新消息指示
            r'AT+CSMP=17,167,0,8',      # 设置短信文本模式参数，支持长短信
            r'AT+CSDH=1',               # 显示详细的短信头信息
            r'AT+CMMS=2',               # 支持更多信息
            r'AT&W',                    # 保存设置
        ]
        for command in commands:
            await asyncio.sleep(2)
            await self.send_at_command_async(command)
    
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

    async def handle_incoming_sms_pdu(self, pdu_part: bytes = b'', force_process: bool = False) -> None:
        """
        处理接收到的短信PDU数据，支持长短信分片合并。

        :param pdu_part: 接收到的部分PDU数据
        :param force_process: 是否强制处理当前已接收的数据,即使数据不完整
        """
        if self.pending_sms["pdu"] is None:
            return

        # 将新接收的PDU部分添加到已有数据中
        self.pending_sms["pdu"] += pdu_part

        # 检查是否已接收到足够的PDU数据或者是否强制处理
        if len(self.pending_sms["pdu"]) >= self.pending_sms["expected_length"] * 2 or force_process:
            decoded_pdu = None
            try:
                # 将字节数据转换为十六进制字符串
                pdu_hex = self.pending_sms["pdu"].decode('ascii', errors='ignore').strip()

                # 解码PDU数据
                decoded_pdu = decodeSmsPdu(pdu_hex)

                # 提取短信信息
                sender = decoded_pdu.get('number', 'Unknown')
                timestamp = decoded_pdu.get('date', datetime.now())
                timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, datetime) else str(timestamp)
                content = decoded_pdu.get('text', '')
                
                # 检查是否是长短信分片（检查UDH中的concatenation信息）
                udh = decoded_pdu.get('udh', [])
                concat_info = None
                
                # 遍历UDH查找concatenation信息
                for header in udh:
                    if isinstance(header, Concatenation):
                        concat_info = {
                            'ref': header.reference,
                            'max': header.parts,
                            'seq': header.number
                        }
                        break
                
                if concat_info:
                    # 这是长短信的一个分片
                    logger.debug(
                        f"检测到长短信UDH - ref={concat_info['ref']}, "
                        f"seq={concat_info['seq']}/{concat_info['max']}"
                    )
                    await self._handle_concat_sms_part(
                        sender, timestamp, content,
                        concat_info['ref'], concat_info['max'], concat_info['seq']
                    )
                else:
                    # 这是普通短信，直接转发
                    logger.debug(f"普通短信（无UDH拼接信息），UDH元素: {udh}")
                    logger.info(
                        f"{'成功解码短信' if not force_process else '强制解码可能不完整的短信'} - "
                        f"发送者: {sender}, 时间: {timestamp_str}, 内容: {content}"
                    )
                    await self.receive_sms_callback(sender, timestamp_str, content)

            except Exception as e:
                logger.error(f"解析PDU时出错: {e}")
                logger.error(f"PDU内容: {self.pending_sms['pdu']}")
                logger.error(f"decoded_pdu: {decoded_pdu}")
            finally:
                # 重置pending_sms，准备接收下一条短信
                self.pending_sms = {"pdu": None, "expected_length": None}
        else:
            logger.debug(f"PDU数据不完整,已接收 {len(self.pending_sms['pdu'])} 字节,"
                         f"预期 {self.pending_sms['expected_length'] * 2} 字节")
    
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
            