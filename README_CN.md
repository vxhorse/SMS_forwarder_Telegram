# SMS_forwarder_Telegram (EC200)

该项目将GSM/LTE通信模块接收到的短信转发至Telegram机器人，同时支持通过Telegram发送短信。

## 功能特点

- 自动转发接收到的短信到Telegram
- 通过Telegram回复短信
- 支持主流LTE模块（如EC200T/EC200S/EC200A等系列）
- Docker部署，易于安装和管理
- 模块热插拔支持

## 硬件要求

- 可能支持的LTE模块（尚未全部实际验证）：
  - EC200T系列
  - EC200S系列
  - EC200A系列
  - EC200N-CN
  - EC600S系列
  - EC600N系列
  - EC800N系列
  - EG912Y-EU
  - EG915N-EU
  - 其他支持AT命令的GSM/LTE模块
- 用于连接模块的USB数据线
- 运行Linux的服务器/计算机

## 安装步骤

### 1. 准备硬件

1. 将SIM卡插入LTE模块
2. 通过USB数据线将模块连接到Linux主机

### 2. 确认设备识别

连接模块后，Linux会创建多个串口设备，需确认正确的短信通信端口：

```bash
ls -l /dev/ttyUSB*
```

通常会看到多个设备（例如ttyUSB0、ttyUSB1、ttyUSB2等）。在不同模块上，短信功能可能在不同的设备上：
- 大多数情况下，ttyUSB2用于短信操作
- 确定正确的端口可能需要尝试不同设备

### 3. 避免设备冲突

某些系统服务可能会占用模块串口，需确保端口可用：

```bash
# 检查是否有服务占用串口
lsof /dev/ttyUSB*

# 禁用可能干扰的服务（如ModemManager）
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

### 4. 创建私有Telegram机器人

1. 在Telegram中，与[@BotFather](https://t.me/botfather)对话创建新机器人
2. 按照指引完成创建流程，获取机器人TOKEN
3. 获取您的Telegram用户ID (CHAT_ID)：
   - 与[@userinfobot](https://t.me/userinfobot)对话获取
   - 或通过其他CHAT_ID获取机器人发送消息

详细教程可参考[Telegram Bot API文档](https://core.telegram.org/bots/api)

### 5. 配置项目

1. 拉取Docker镜像：

```bash
docker pull vxhorse/sms-forwarder
```

2. 创建`docker-compose.yml`文件，并配置环境变量和设备映射：

```yaml
services:
  sms-forwarder:
    image: vxhorse/sms-forwarder:latest
    container_name: sms-forwarder
    restart: unless-stopped
    network_mode: "host"
    devices:
      - /dev/ttyUSB2:/dev/ttyUSB2
    volumes:
      - /etc/localtime:/etc/localtime:ro
    environment:
      - LOG_LEVEL=INFO
      - SMS_PORT=/dev/ttyUSB2
      - SMS_BAUDRATE=115200
      - BOT_TOKEN=your_telegram_bot_token
      - CHAT_ID=your_telegram_chat_id
      - PROXY_URL=http://127.0.0.1:7890
```

请确保修改以下内容：
- `SMS_PORT`: 按实际情况修改为正确的短信通信端口
- `BOT_TOKEN`: 替换为您的Telegram机器人Token
- `CHAT_ID`: 替换为您的Telegram用户ID
- 设备映射 `/dev/ttyUSB2:/dev/ttyUSB2`: 修改为正确的短信端口

### 6. 启动服务

```bash
docker compose up -d
```

## 使用说明

服务启动后，将自动监听接收短信并转发至配置的Telegram会话。

### 通过Telegram发送短信

在Telegram机器人对话中：

1. 使用`/sendsms`命令开始发送流程
2. 按提示输入目标手机号码
3. 按提示输入短信内容
4. 短信发送后会收到确认

### 查看帮助

在Telegram机器人对话中发送`/help`查看所有可用命令。

## 注意事项

- **兼容性**：不同型号的模块兼容性不同，某些模块可能不支持长文本短信的收发
- **稳定性**：部分模块在长时间运行后可能需要重启以保持稳定
- **串口选择**：如遇通信问题，尝试修改`SMS_PORT`环境变量为其他ttyUSB设备
- **SIM卡检测**：确保SIM卡正确插入并有足够余额
- **网络依赖**：Telegram通信需要稳定的网络连接
- **防火墙设置**：确保服务器允许Telegram API的网络连接

## 故障排除

1. **短信无法收发**：
   - 检查模块串口是否正确配置
   - 确认SIM卡状态（是否有信号、余额）
   - 查看日志：`docker logs sms-forwarder`

2. **Telegram通信问题**：
   - 验证TOKEN和CHAT_ID配置
   - 检查网络连接和代理设置
   - 确认机器人权限设置正确

3. **模块无法识别**：
   - 重新插拔模块
   - 检查USB连接
   - 确认系统识别设备：`dmesg | grep tty`