#!/usr/bin/env bash
#
# Docker环境下的启动脚本
# 1. 设置日志级别
# 2. 启动main.py

# 在出现错误时终止脚本
set -e

# 信号处理函数
handle_signal() {
    echo "接收到信号 $1，正在退出..."
    if [ -n "$PID" ]; then
        kill -TERM "$PID" 2>/dev/null || true
    fi
    exit 0
}

# 设置信号处理
trap 'handle_signal SIGTERM' SIGTERM
trap 'handle_signal SIGINT' SIGINT

# 日志级别已通过环境变量设置
echo "日志级别: ${LOG_LEVEL:-INFO}"
echo "串口设备: ${SMS_PORT:-/dev/ttyUSB2}"
echo "波特率: ${SMS_BAUDRATE:-115200}"

# 启动项目
python main.py &
PID=$!

# 等待程序结束
wait $PID
EXIT_CODE=$?

echo "程序退出，退出码: $EXIT_CODE"
exit $EXIT_CODE