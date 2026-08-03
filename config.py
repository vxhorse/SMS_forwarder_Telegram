import os

# 从环境变量获取日志级别，默认INFO
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# SMS模块配置
SMS_PORT = os.getenv("SMS_PORT", "/dev/ttyUSB2")
SMS_BAUDRATE = int(os.getenv("SMS_BAUDRATE", "115200"))

# Telegram 机器人配置
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_telegram_bot_token")
CHAT_ID = os.getenv("CHAT_ID", "your_telegram_chat_id")
PROXY_URL = os.getenv("PROXY_URL", "http://127.0.0.1:7890")

# Health snapshot file written by the process and read by the container healthcheck.
HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/healthy")
# How old the health file may be before the healthcheck considers it stale.
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "120"))
# Exit the process once any component has been down this long, letting the
# container runtime restart everything as a last resort.
WATCHDOG_DOWN_SECONDS = int(os.getenv("WATCHDOG_DOWN_SECONDS", "3600"))
