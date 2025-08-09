import serial
import serial_asyncio
import asyncio
import time
import re
from datetime import datetime, timedelta
from typing import Optional, Callable
from config import SMS_PORT, SMS_BAUDRATE
from logger import setup_logger
from gsmmodem.pdu import encodeSmsSubmitPdu, decodeSmsPdu

logger = setup_logger(__name__)

class DeviceManager:
    """
    设备管理类，用于检测和管理串口设备。
    """

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
        if self.read_task:
            try:
                self.read_task.cancel()
                logger.warning("轮调任务read_task被取消")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"取消read_task时出错: {e}")
            finally:
                self.read_task = None
            
        if self.process_task:
            try:
                self.process_task.cancel()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"取消process_task时出错: {e}")
            finally:
                self.process_task = None
            
        # 关闭串口连接
        if self.writer:
            try:
                # 安全关闭串口写入器
                self.writer.close()
                # 添加try-except防止wait_closed()抛出异常
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"等待写入器关闭超时或出错: {e}")
            except Exception as e:
                logger.error(f"关闭写入器时出错: {e}")
            finally:
                # 无论如何都确保写入器被标记为已关闭
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
            except Exception as e:
                number_of_errors += 1
                logger.warning(f"读取循环出错: {e}")
                await asyncio.sleep(self.retry_delay)
                
                # 如果连续出错超过阈值，标记服务为停止状态并退出
                if number_of_errors >= self.max_retries:
                    logger.error(f"读取循环连续出错 {number_of_errors} 次，停止服务")
                    self.is_running = False
                    # 确保抛出异常让上层知道服务已停止
                    raise RuntimeError(f"设备读取失败: {e}")
                    
        # 当is_running为False时，确保任务能够正常结束
        logger.warning("读取循环已关闭")
    
    async def process_loop(self) -> None:
        """处理消息队列的循环"""
        number_of_errors = 0
        while self.is_running:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=5)
                await self.process_message(message)
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
                    await self.close()
                    raise ValueError("处理循环出错")
            else:
                number_of_errors = 0
    
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
        处理接收到的短信PDU数据。

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
                timestamp = decoded_pdu.get('date', datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
                content = decoded_pdu.get('text', '')

                logger.info(
                    f"{'成功解码短信' if not force_process else '强制解码可能不完整的短信'} - 发送者: {sender}, 时间: {timestamp}, 内容: {content}")

                # 调用回调函数处理解码后的短信
                await self.receive_sms_callback(sender, timestamp, content)

            except Exception as e:
                logger.error(f"解析PDU时出错: {e}")
                # 在这里添加更详细的错误信息
                logger.error(f"PDU内容: {self.pending_sms['pdu']}")
                logger.error(f"decoded_pdu: {decoded_pdu}")
            finally:
                # 重置pending_sms，准备接收下一条短信
                self.pending_sms = {"pdu": None, "expected_length": None}
        else:
            logger.debug(f"PDU数据不完整,已接收 {len(self.pending_sms['pdu'])} 字节,"
                         f"预期 {self.pending_sms['expected_length'] * 2} 字节")
        
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
            