import asyncio
import threading
import os
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
        self.dm: Optional[DeviceManager] = DeviceManager(self.handle_forwarding_sms)  # 设备管理器
        self.tb: Optional[TelegramBot] = TelegramBot(self.handle_send_sms, BOT_TOKEN, CHAT_ID, PROXY_URL)  # TelegramBot 管理器
        
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
        try:
            # 启动子线程
            self.dm_thread.start()
            await asyncio.wait_for(self.dm.priming_event.wait(), timeout=40)
            self.tb_thread.start()

            # 保持服务运行状态
            while self.is_running:
                await asyncio.sleep(60)  # 每分钟检查一次
                
                # 检查服务状态
                if not self.tb.is_running or not self.dm.is_running:
                    logger.warning("检测到某个服务未运行，进行等待...")
                    await asyncio.sleep(10)  # 延迟10秒后重试
                    if not self.tb.is_running:
                        raise RuntimeError("TelegramBot 服务停止运行")
                    elif not self.dm.is_running:
                        raise RuntimeError("DeviceManager 服务停止运行")
                    else:
                        logger.info("所有服务已继续运行")
                    
        except Exception as e:
            logger.error(f"主线程运行出错: {e}")
        finally:
            await self.close()
            await asyncio.sleep(10)
    
    async def close(self):
        """
        关闭服务，停止所有正在运行的子线程。
        """
        self.is_running = False
        await asyncio.gather(
            self.dm.close(),
            self.tb.close()
        )
            
        # 等待线程结束
        self.dm.exit_event.set()
        self.tb.exit_event.set()
        self.dm_thread.join()
        self.tb_thread.join()
    
    def run_device_manager(self):
        """
        DeviceManager 的线程运行函数，启动 DeviceManager 的事件循环
        """
        logger.info("设备管理器线程已启动")
        self.dm_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.dm_loop)
        self.dm_loop.run_until_complete(self.dm.start())
        logger.info("设备管理器线程已结束")
        
    def run_telegram_bot(self):
        """
        TelegramBot 的线程运行函数，启动 TelegramBot 的事件循环
        """
        logger.info("TelegramBot 线程已启动")
        self.tb_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.tb_loop)
        self.tb_loop.run_until_complete(self.tb.start())
        logger.info("TelegramBot 线程已结束")
        
    async def handle_forwarding_sms(self, phone_number: str, timestamp: str, content: str) -> bool: 
        """
        处理接收到的短信并转发到 TelegramBot，确保该函数在 TelegramBot 事件循环中执行。

        :param phone_number: 发送者的电话号码
        :param timestamp: 短信的接收时间戳
        :param content: 短信内容
        :return: 返回转发结果（True 为成功，False 为失败）
        """
        # 使用 TelegramBot 的事件循环来调用异步方法
        future = asyncio.run_coroutine_threadsafe(
            self.tb.handle_forwarding_sms(phone_number, timestamp, content),
            self.tb_loop
        )
        return future.result()

    async def handle_send_sms(self, phone_number: str, message: str) -> bool:
        """
        处理发送短信的请求，并调用 DeviceManager 进行实际的短信发送。

        :param phone_number: 目标电话号码
        :param message: 短信内容
        :return: 返回发送结果（True 为成功，False 为失败）
        """
        # 使用 DeviceManager 的事件循环来调用异步方法
        future = asyncio.run_coroutine_threadsafe(
            self.dm.handle_send_sms(phone_number, message),
            self.dm_loop
        )
        return future.result()
    
if __name__ == "__main__":
    main = Main()
    try:
        logger.info("程序启动中...")
        asyncio.run(main.start())  # 启动主程序
    except KeyboardInterrupt:
        logger.warning("接收到键盘中断信号，正在关闭程序...")
        asyncio.run(main.close())
    except Exception as e:
        logger.error(f"程序运行时出现错误: {e}")
        asyncio.run(main.close())
    finally:
        # 强制退出整个程序
        logger.info("程序清理已完成，准备强制关闭...")
        os._exit(0)
