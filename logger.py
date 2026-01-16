import logging
import sys
import config

# 全局标志，避免重复配置根logger
_logging_configured = False


def setup_logger(name: str) -> logging.Logger:
    """
    设置并返回一个日志记录器。
    
    :param name: logger名称
    :return: 配置好的logger实例
    """
    global _logging_configured
    
    logger = logging.getLogger(name)
    
    # 将字符串日志级别转换为 logging 模块可识别的数字级别
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)
    
    # 只在logger没有handler时添加（避免重复添加）
    if not logger.handlers:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s'
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)
    
    # 防止日志传播到父logger造成重复输出
    logger.propagate = False
    
    return logger
