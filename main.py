import asyncio
import threading
import sys
from config import BOT_TOKEN, CHAT_ID, PROXY_URL
from logger import setup_logger
from module.device_manager import DeviceManager
from module.telegram_bot import TelegramBot
from typing import Optional

logger = setup_logger(__name__)

class Main:
    
    def __init__(self):
        """
        Main 类初始化方法，创建 DeviceManager 和 TelegramBot 实例，并初始化必要的线程和循环。
        """
        self.dm: Optional[DeviceManager] = DeviceManager(self.handle_forwarding_sms)
        self.tb: Optional[TelegramBot] = TelegramBot(self.handle_send_sms, BOT_TOKEN, CHAT_ID, PROXY_URL)
        
        # 线程
        self.dm_thread: Optional[threading.Thread] = threading.Thread(target=self.run_device_manager, name="DeviceManagerThread")
        self.tb_thread: Optional[threading.Thread] = threading.Thread(target=self.run_telegram_bot, name="TelegramBotThread")
        
        # 程序运行标志
        self.is_running: bool = True
        
        # TelegramBot 的事件循环，用于跨线程调用异步方法
        self.dm_loop: Optional[asyncio.AbstractEventLoop] = None
        self.tb_loop: Optional[asyncio.AbstractEventLoop] = None
    
    async def start(self):
        """
        启动主线程，启动设备管理器和 TelegramBot 的线程，并保持服务运行。
        """
        # 启动子线程
        self.dm_thread.start()
        try:
            await asyncio.wait_for(self.dm.priming_event.wait(), timeout=40)
        except asyncio.TimeoutError:
            logger.error("设备管理器启动超时")
            raise RuntimeError("设备管理器启动失败")
            
        self.tb_thread.start()

        # 保持服务运行状态
        while self.is_running:
            await asyncio.sleep(60)  # 每分钟检查一次
            
            # 检查服务状态
            if not self.tb.is_running or not self.dm.is_running:
                logger.warning("检测到某个服务未运行，进行等待...")
                await asyncio.sleep(10)  # 延迟10秒后重试
                if not self.tb.is_running or not self.dm.is_running:
                    if not self.tb.is_running:
                        raise RuntimeError("TelegramBot 服务停止运行")
                    elif not self.dm.is_running:
                        raise RuntimeError("DeviceManager 服务停止运行")
                else:
                    logger.info("所有服务已继续运行")
    
    async def close(self):
        """
        关闭服务，停止所有正在运行的子线程。
        """
        self.is_running = False
        logger.info("开始关闭应用程序...")

        try:
            # 在各自事件循环中执行 close
            if self.dm_loop and not self.dm_loop.is_closed():
                asyncio.run_coroutine_threadsafe(self.dm.close(), self.dm_loop)

            if self.tb_loop and not self.tb_loop.is_closed():
                asyncio.run_coroutine_threadsafe(self.tb.close(), self.tb_loop)

            # 等待线程结束
            if self.dm_thread and self.dm_thread.is_alive():
                self.dm_thread.join(timeout=5)

            if self.tb_thread and self.tb_thread.is_alive():
                self.tb_thread.join(timeout=5)

            logger.info("所有服务已关闭")
        except Exception as e:
            logger.error(f"关闭过程中出现错误: {e}")
    
    def run_device_manager(self):
        """
        DeviceManager 的线程运行函数，启动 DeviceManager 的事件循环
        """
        logger.info("DeviceManager线程已启动")
        self.dm_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.dm_loop)
        try:
            self.dm_loop.run_until_complete(self.dm.start())
        except Exception as e:
            logger.error(f"DeviceManager出现致命错误: {e}")
            self.is_running = False  # 通知主线程停止
        finally:
            logger.info("DeviceManager线程已结束")
        
    def run_telegram_bot(self):
        """
        TelegramBot 的线程运行函数，启动 TelegramBot 的事件循环
        """
        logger.info("TelegramBot 线程已启动")
        self.tb_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.tb_loop)
        try:
            self.tb_loop.run_until_complete(self.tb.start())
        except Exception as e:
            logger.error(f"TelegramBot出现致命错误: {e}")
            self.is_running = False  # 通知主线程停止
        finally:
            logger.info("TelegramBot 线程已结束")
        
    async def handle_forwarding_sms(self, phone_number: str, timestamp: str, content: str) -> bool: 
        """
        处理接收到的短信并转发到 TelegramBot，确保该函数在 TelegramBot 事件循环中执行。

        :param phone_number: 发送者的电话号码
        :param timestamp: 短信的接收时间戳
        :param content: 短信内容
        :return: 返回转发结果（True 为成功，False 为失败）
        """
        try:
            # 使用 TelegramBot 的事件循环来调用异步方法
            future = asyncio.run_coroutine_threadsafe(
                self.tb.handle_forwarding_sms(phone_number, timestamp, content),
                self.tb_loop
            )
            return future.result()
        except Exception as e:
            logger.error(f"转发短信时出现错误: {e}")
            return False

    async def handle_send_sms(self, phone_number: str, message: str) -> bool:
        """
        处理发送短信的请求，并调用 DeviceManager 进行实际的短信发送。

        :param phone_number: 目标电话号码
        :param message: 短信内容
        :return: 返回发送结果（True 为成功，False 为失败）
        """
        try:
            # 使用 DeviceManager 的事件循环来调用异步方法
            future = asyncio.run_coroutine_threadsafe(
                self.dm.handle_send_sms(phone_number, message),
                self.dm_loop
            )
            return future.result()
        except Exception as e:
            logger.error(f"发送短信时出现错误: {e}")
            return False
    
if __name__ == "__main__":
    main = Main()
    exit_code = 0
    try:
        logger.info("程序启动中...")
        asyncio.run(main.start())
    except KeyboardInterrupt:
        logger.warning("接收到键盘中断信号，正在关闭程序...")
        exit_code = 0
    except Exception as e:
        logger.error(f"程序运行时发生错误: {e}")
        exit_code = 1  # 统一使用退出码1触发容器重启
    finally:
        try:
            asyncio.run(main.close())
        except Exception as e:
            logger.error(f"程序关闭时出现错误: {e}")
        logger.info(f"程序已退出，退出码: {exit_code}")
        sys.exit(exit_code)
