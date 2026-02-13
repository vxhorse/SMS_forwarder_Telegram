import asyncio
import signal
import sys
import os
from config import BOT_TOKEN, CHAT_ID, PROXY_URL
from logger import setup_logger
from module.device_manager import DeviceManager
from module.telegram_bot import TelegramBot

logger = setup_logger(__name__)

# 健康检查文件路径
HEALTH_FILE = '/tmp/healthy'

# 强制退出超时（秒）
FORCE_EXIT_TIMEOUT = 10


class SMSForwarder:
    """SMS转发服务主类，统一管理DeviceManager和TelegramBot"""
    
    def __init__(self):
        self.dm: DeviceManager = None
        self.tb: TelegramBot = None
        self.is_running: bool = False
        self._shutdown_event = asyncio.Event()
    
    async def start(self) -> None:
        """启动SMS转发服务"""
        logger.info("正在启动SMS转发服务...")
        
        # 创建组件实例
        self.dm = DeviceManager(self._forward_sms_to_telegram)
        self.tb = TelegramBot(self._send_sms_via_device, BOT_TOKEN, CHAT_ID, PROXY_URL)
        
        try:
            # 先启动设备管理器
            logger.info("正在初始化设备管理器...")
            dm_connect_task = asyncio.create_task(self.dm.start())
            
            # 等待设备管理器就绪
            try:
                await asyncio.wait_for(self.dm.priming_event.wait(), timeout=40)
            except asyncio.TimeoutError:
                logger.error("设备管理器启动超时")
                raise RuntimeError("设备管理器启动失败")
            
            if not self.dm.is_running:
                raise RuntimeError("设备管理器未能正常启动")
            
            # 启动Telegram Bot
            logger.info("正在初始化Telegram Bot...")
            tb_connect_task = asyncio.create_task(self.tb.start())
            
            # 等待 Telegram Bot 就绪事件（带超时）
            try:
                await asyncio.wait_for(self.tb.priming_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("Telegram Bot启动超时")
                raise RuntimeError("Telegram Bot启动超时")
            
            if not self.tb.is_running:
                raise RuntimeError("Telegram Bot未能正常启动")
            
            self.is_running = True
            self._mark_healthy()
            logger.info("SMS转发服务已成功启动")
            
            # 主监控循环
            await self._monitor_loop(dm_connect_task, tb_connect_task)
            
        except Exception as e:
            logger.error(f"服务启动失败: {e}")
            raise
        finally:
            self._mark_unhealthy()
    
    async def _monitor_loop(self, dm_task: asyncio.Task, tb_task: asyncio.Task) -> None:
        """监控服务状态的主循环"""
        check_interval = 10  # 检查间隔（秒）
        
        while self.is_running and not self._shutdown_event.is_set():
            await asyncio.sleep(check_interval)
            
            # 检查各服务状态
            if not self.dm.is_running:
                logger.error("设备管理器已停止运行")
                raise RuntimeError("DeviceManager服务异常停止")
            
            if not self.tb.is_running:
                logger.error("Telegram Bot已停止运行")
                raise RuntimeError("TelegramBot服务异常停止")
            
            # 检查任务是否异常终止
            if dm_task.done() and not self._shutdown_event.is_set():
                exc = dm_task.exception()
                if exc:
                    raise RuntimeError(f"DeviceManager异常: {exc}")
            
            if tb_task.done() and not self._shutdown_event.is_set():
                exc = tb_task.exception()
                if exc:
                    raise RuntimeError(f"TelegramBot异常: {exc}")
            
            # 更新健康状态
            self._mark_healthy()
    
    async def shutdown(self) -> None:
        """优雅关闭服务"""
        logger.info("开始关闭SMS转发服务...")
        self.is_running = False
        self._shutdown_event.set()
        self._mark_unhealthy()
        
        try:
            # 关闭各组件，设置超时
            close_tasks = []
            
            if self.dm:
                close_tasks.append(asyncio.create_task(self.dm.close()))
            if self.tb:
                close_tasks.append(asyncio.create_task(self.tb.close()))
            
            if close_tasks:
                # 等待所有关闭任务完成，最多等待5秒
                done, pending = await asyncio.wait(close_tasks, timeout=5)
                
                # 取消未完成的任务
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            logger.info("SMS转发服务已关闭")
            
        except Exception as e:
            logger.error(f"关闭服务时出错: {e}")
    
    async def _forward_sms_to_telegram(self, phone_number: str, timestamp: str, content: str) -> bool:
        """将收到的短信转发到Telegram"""
        try:
            return await self.tb.handle_forwarding_sms(phone_number, timestamp, content)
        except Exception as e:
            logger.error(f"转发短信到Telegram失败: {e}")
            return False
    
    async def _send_sms_via_device(self, phone_number: str, message: str) -> bool:
        """通过设备发送短信"""
        try:
            return await self.dm.handle_send_sms(phone_number, message)
        except Exception as e:
            logger.error(f"发送短信失败: {e}")
            return False
    
    def _mark_healthy(self) -> None:
        """标记服务为健康状态"""
        try:
            with open(HEALTH_FILE, 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    
    def _mark_unhealthy(self) -> None:
        """移除健康标记"""
        try:
            if os.path.exists(HEALTH_FILE):
                os.remove(HEALTH_FILE)
        except Exception:
            pass


def force_exit(signum=None, frame=None):
    """强制退出程序"""
    logger.warning("强制退出程序")
    os._exit(1)


async def main():
    """主入口函数"""
    forwarder = SMSForwarder()
    loop = asyncio.get_running_loop()
    
    # 设置信号处理
    def signal_handler(sig):
        logger.info(f"收到信号 {sig}，开始关闭...")
        asyncio.create_task(shutdown_with_timeout(forwarder))
    
    # 注册信号处理器（仅在非Windows系统上）
    if sys.platform != 'win32':
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
    
    try:
        await forwarder.start()
    except KeyboardInterrupt:
        logger.warning("收到键盘中断信号")
    except Exception as e:
        logger.error(f"服务运行时发生错误: {e}")
        raise
    finally:
        await forwarder.shutdown()


async def shutdown_with_timeout(forwarder: SMSForwarder):
    """带超时的关闭流程"""
    try:
        await asyncio.wait_for(forwarder.shutdown(), timeout=FORCE_EXIT_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"关闭超时（{FORCE_EXIT_TIMEOUT}秒），强制退出")
        force_exit()


if __name__ == "__main__":
    exit_code = 0
    
    try:
        logger.info("程序启动中...")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("程序被用户中断")
        exit_code = 0
    except Exception as e:
        logger.error(f"程序运行时发生错误: {e}")
        exit_code = 1
    finally:
        logger.info(f"程序退出，退出码: {exit_code}")
        sys.exit(exit_code)
