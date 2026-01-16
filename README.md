# SMS_forwarder_Telegram (EC200)

This project forwards SMS messages received by GSM/LTE communication modules to a Telegram bot, while also supporting sending SMS through Telegram.

## README
[English](README.md) [日本語](README_JP.md) [简体中文](README_CN.md)

## Features

- Automatically forwards received SMS to Telegram
- Reply to SMS through Telegram
- **Automatic long SMS merging**: Automatically identifies and merges segmented SMS to ensure complete text reception
- Supports mainstream LTE modules (such as EC200T/EC200S/EC200A series)
- Docker deployment for easy installation and management
- Hot-plug support for modules
- **Service health check**: Built-in health check mechanism to ensure stable service operation

## Hardware Requirements

- Potentially supported LTE modules (not all verified):
  - EC200T series
  - EC200S series
  - EC200A series
  - EC200N-CN
  - EC600S series
  - EC600N series
  - EC800N series
  - EG912Y-EU
  - EG915N-EU
  - Other GSM/LTE modules supporting AT commands
- USB data cable for connecting the module
- Linux server/computer

## Installation Steps

### 1. Prepare Hardware

1. Insert the SIM card into the LTE module
2. Connect the module to the Linux host via USB data cable

### 2. Confirm Device Recognition

After connecting the module, Linux will create multiple serial port devices. You need to confirm the correct SMS communication port:

```bash
ls -l /dev/ttyUSB*
```

You'll typically see multiple devices (e.g., ttyUSB0, ttyUSB1, ttyUSB2, etc.). On different modules, SMS functionality may be on different devices:
- In most cases, ttyUSB2 is used for SMS operations
- Determining the correct port may require trying different devices

### 3. Avoid Device Conflicts

Some system services may occupy the module's serial port. Ensure the port is available:

```bash
# Check if any services are using the serial port
lsof /dev/ttyUSB*

# Disable services that might interfere (such as ModemManager)
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

### 4. Create a Private Telegram Bot

1. In Telegram, chat with [@BotFather](https://t.me/botfather) to create a new bot
2. Follow the guide to complete the creation process and obtain the bot TOKEN
3. Get your Telegram user ID (CHAT_ID):
   - Chat with [@userinfobot](https://t.me/userinfobot) to obtain it
   - Or get it through other methods for the bot to send messages

For detailed tutorial, refer to the [Telegram Bot API documentation](https://core.telegram.org/bots/api)

### 5. Configure the Project

1. Pull the Docker image:

```bash
docker pull vxhorse/sms-forwarder
```

2. Create a `docker-compose.yml` file and configure environment variables and device mapping:

```yaml
services:
  sms-forwarder:
    image: vxhorse/sms-forwarder:latest
    container_name: sms-forwarder
    restart: unless-stopped
    network_mode: "host"
    init: true  # Use tini as init process to ensure proper signal handling
    stop_grace_period: 30s  # Graceful shutdown timeout
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
    healthcheck:  # Health check configuration
      test: ["CMD", "python", "-c", "import os; exit(0 if os.path.exists('/tmp/healthy') else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

Please make sure to modify the following:
- `SMS_PORT`: Change to the correct SMS communication port as needed
- `BOT_TOKEN`: Replace with your Telegram bot Token
- `CHAT_ID`: Replace with your Telegram user ID
- Device mapping `/dev/ttyUSB2:/dev/ttyUSB2`: Modify to the correct SMS port

### 6. Start the Service

```bash
docker compose up -d
```

## Usage Instructions

Once the service is started, it will automatically monitor incoming SMS and forward them to the configured Telegram conversation.

### Sending SMS via Telegram

In the Telegram bot conversation:

1. Use the `/sendsms` command to start the sending process
2. Enter the target phone number as prompted
3. Enter the SMS content as prompted
4. You'll receive confirmation after the SMS is sent

### View Help

Send `/help` in the Telegram bot conversation to view all available commands.

## Notes

- **Long SMS Support**: This service supports automatic merging of long SMS. Segmented messages will wait up to 60 seconds for all parts to arrive before merging and forwarding
- **Compatibility**: Different module models have varying compatibility; some modules may not support sending and receiving long text messages
- **Stability**: The service has built-in health checks and automatic restart mechanisms; network disconnections will automatically recover
- **Serial Port Selection**: If communication issues occur, try modifying the `SMS_PORT` environment variable to other ttyUSB devices
- **SIM Card Detection**: Ensure the SIM card is properly inserted and has sufficient balance
- **Network Dependency**: Telegram communication requires a stable network connection
- **Firewall Settings**: Ensure the server allows network connections to the Telegram API

## Troubleshooting

1. **Unable to Send/Receive SMS**:
   - Check if the module's serial port is correctly configured
   - Confirm SIM card status (signal, balance)
   - Check logs: `docker logs sms-forwarder`

2. **Telegram Communication Issues**:
   - Verify TOKEN and CHAT_ID configuration
   - Check network connection and proxy settings
   - Confirm bot permission settings are correct

3. **Module Not Recognized**:
   - Reconnect the module
   - Check USB connection
   - Confirm system recognizes the device: `dmesg | grep tty`
