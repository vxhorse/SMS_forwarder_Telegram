#!/usr/bin/env bash
#
# Docker环境下的启动脚本
# 1. 设置日志级别
# 2. 启动main.py

# 在出现错误时终止脚本
set -e

# 日志级别已通过环境变量设置
echo "日志级别: ${LOG_LEVEL:-INFO}"
echo "串口设备: ${SMS_PORT:-/dev/ttyUSB2}"
echo "波特率: ${SMS_BAUDRATE:-115200}"

# 启动项目
exec python main.py