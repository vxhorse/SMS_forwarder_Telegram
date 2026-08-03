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
# Exponential backoff bounds for component reconnection, in seconds.
RECONNECT_BACKOFF_MIN = float(os.getenv("RECONNECT_BACKOFF_MIN", "1.0"))
RECONNECT_BACKOFF_MAX = float(os.getenv("RECONNECT_BACKOFF_MAX", "30.0"))
# How long a connected session must last before it counts as a recovery.
# A component that connects and then fails immediately, over and over, is still
# broken; treating the connection itself as success would reset the backoff and
# the health timestamp every cycle, hiding the fault from the watchdog.
SERVICE_STABLE_SECONDS = float(os.getenv("SERVICE_STABLE_SECONDS", "60.0"))
# How often the watchdog inspects component health, in seconds. Floored because
# a value of zero would turn the watchdog into a busy loop.
WATCHDOG_CHECK_INTERVAL = max(1.0, float(os.getenv("WATCHDOG_CHECK_INTERVAL", "30.0")))
