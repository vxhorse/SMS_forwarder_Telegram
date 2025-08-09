# SMS_forwarder_Telegram (EC200)

このプロジェクトはGSM/LTE通信モジュールが受信したSMSをTelegramボットに転送し、Telegramを通じてSMSを送信する機能もサポートします。

## 機能特徴

- 受信したSMSを自動的にTelegramに転送
- Telegramを通じてSMSに返信
- 主流のLTEモジュール（EC200T/EC200S/EC200Aなどのシリーズ）をサポート
- Dockerデプロイメントで簡単にインストールと管理が可能
- モジュールのホットプラグサポート

## ハードウェア要件

- サポート可能なLTEモジュール（すべて検証済みではありません）：
  - EC200Tシリーズ
  - EC200Sシリーズ
  - EC200Aシリーズ
  - EC200N-CN
  - EC600Sシリーズ
  - EC600Nシリーズ
  - EC800Nシリーズ
  - EG912Y-EU
  - EG915N-EU
  - その他AT命令をサポートするGSM/LTEモジュール
- モジュール接続用USBデータケーブル
- Linuxが稼働するサーバー/コンピュータ

## インストール手順

### 1. ハードウェアの準備

1. SIMカードをLTEモジュールに挿入
2. USBデータケーブルでモジュールをLinuxホストに接続

### 2. デバイス認識の確認

モジュール接続後、Linuxは複数のシリアルポートデバイスを作成します。正しいSMS通信ポートを確認する必要があります：

```bash
ls -l /dev/ttyUSB*
```

通常、複数のデバイス（例：ttyUSB0、ttyUSB1、ttyUSB2など）が表示されます。異なるモジュールでは、SMS機能が異なるデバイスに割り当てられている場合があります：
- ほとんどの場合、ttyUSB2がSMS操作に使用されます
- 正しいポートを特定するには、異なるデバイスを試す必要があるかもしれません

### 3. デバイス競合の回避

一部のシステムサービスがモジュールのシリアルポートを占有している可能性があるため、ポートが利用可能であることを確認してください：

```bash
# シリアルポートを占有しているサービスを確認
lsof /dev/ttyUSB*

# 干渉する可能性のあるサービス（ModemManagerなど）を無効化
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

### 4. プライベートTelegramボットの作成

1. Telegramで[@BotFather](https://t.me/botfather)と対話して新しいボットを作成
2. 指示に従って作成プロセスを完了し、ボットのTOKENを取得
3. あなたのTelegramユーザーID (CHAT_ID)を取得：
   - [@userinfobot](https://t.me/userinfobot)と対話して取得
   - または他のCHAT_ID取得ボットを使用

詳細なチュートリアルは[Telegram Bot APIドキュメント](https://core.telegram.org/bots/api)を参照してください

### 5. プロジェクトの設定

1. Dockerイメージを取得：

```bash
docker pull vxhorse/sms-forwarder
```

2. `docker-compose.yml`ファイルを作成し、環境変数とデバイスマッピングを設定：

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

以下の内容を必ず変更してください：
- `SMS_PORT`: 実際の状況に応じて正しいSMS通信ポートに修正
- `BOT_TOKEN`: あなたのTelegramボットTokenに置き換え
- `CHAT_ID`: あなたのTelegramユーザーIDに置き換え
- デバイスマッピング `/dev/ttyUSB2:/dev/ttyUSB2`: 正しいSMSポートに修正

### 6. サービスの起動

```bash
docker compose up -d
```

## 使用方法

サービス起動後、自動的にSMSを監視し、設定されたTelegramチャットに転送します。

### Telegramを通じてSMSを送信

Telegramボットとの対話で：

1. `/sendsms`コマンドで送信プロセスを開始
2. 指示に従って宛先の電話番号を入力
3. 指示に従ってSMS内容を入力
4. SMS送信後に確認通知が届きます

### ヘルプの確認

Telegramボットとの対話で`/help`を送信して、利用可能なすべてのコマンドを確認できます。

## 注意事項

- **互換性**：異なるモデルのモジュールの互換性は異なり、一部のモジュールは長文SMSの送受信をサポートしていない場合があります
- **安定性**：一部のモジュールは長時間稼働後、安定性を維持するために再起動が必要な場合があります
- **シリアルポートの選択**：通信問題が発生した場合、`SMS_PORT`環境変数を他のttyUSBデバイスに変更してみてください
- **SIMカードの検出**：SIMカードが正しく挿入され、十分な残高があることを確認してください
- **ネットワーク依存**：Telegram通信には安定したネットワーク接続が必要です
- **ファイアウォール設定**：サーバーがTelegram APIのネットワーク接続を許可していることを確認してください

## トラブルシューティング

1. **SMSの送受信ができない**：
   - モジュールのシリアルポートが正しく設定されているか確認
   - SIMカードの状態（信号、残高があるか）を確認
   - ログを確認：`docker logs sms-forwarder`

2. **Telegram通信の問題**：
   - TOKENとCHAT_IDの設定を検証
   - ネットワーク接続とプロキシ設定を確認
   - ボットの権限設定が正しいことを確認

3. **モジュールが認識されない**：
   - モジュールを抜き差し
   - USB接続を確認
   - システムがデバイスを認識しているか確認：`dmesg | grep tty`