import logging
import config

def setup_logger(name: str) -> logging.Logger:
    """
    设置并返回一个日志记录器
    """
    logger = logging.getLogger(name)

    # 将字符串日志级别转换为 logging 模块可识别的数字级别
    # 若无法识别，则默认使用 INFO 级别
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger
