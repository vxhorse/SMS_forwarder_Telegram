import logging
import sys
import config

# Guards against configuring the root logger more than once.
_logging_configured = False


def setup_logger(name: str) -> logging.Logger:
    """
    Build and return a logger.

    :param name: logger name
    :return: the configured logger
    """
    global _logging_configured

    logger = logging.getLogger(name)

    # Translate the configured level name into the numeric level.
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    # Only add a handler when there is none, so repeat calls do not duplicate output.
    if not logger.handlers:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s'
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)
    
    # Do not propagate to the parent logger, which would duplicate every line.
    logger.propagate = False
    
    return logger
