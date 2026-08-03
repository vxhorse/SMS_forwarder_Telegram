# SMS_forwarder_Telegram (EC200)

このプロジェクトはGSM/LTE通信モジュールが受信したSMSをTelegramボットに転送し、Telegramを通じてSMSを送信する機能もサポートします。

## ドキュメント
[English](README.md) | [日本語](README_JP.md) | [简体中文](README_CN.md) | [فارسی](README_FA.md)

## 機能特徴

- 受信したSMSを自動的にTelegramに転送
- Telegramを通じてSMSに返信
- **長文SMS自動結合**：分割されたSMSを自動的に識別・結合し、完全なテキストを確実に受信
- 主流のLTEモジュール（EC200T/EC200S/EC200Aなどのシリーズ）をサポート
- Dockerデプロイメントで簡単にインストールと管理が可能
- **ポートの自動検出**：モジュールのATポートを自ら探索するため、デバイスパスの設定は不要
- **ハードウェアを待機**：モジュールの列挙が完了する前に起動でき、デバイスが現れ次第接続
- **サービスヘルスチェック**：監視・ヘルス報告・ウォッチドッグにより無人での安定稼働を保証

## システムアーキテクチャ

```mermaid
graph TD
    subgraph DC [Docker Container]
        Sup[Supervisor]
        WD[Watchdog]
        HS[HealthState]
        HC["healthcheck.py"]

        subgraph DeviceLayer [Device Layer]
            DM[DeviceManager]
            Disc[Port Discovery]
            Serial["Serial Port (ttyUSB/ttyACM)"]
            Buffer[ConcatSmsBuffer]
        end

        subgraph NetworkLayer [Network Layer]
            Bot[TelegramBot]
        end
    end

    Hardware[LTE Module] <--> Serial
    Sup -- "監視と再接続" --> DM
    Sup -- "監視と再接続" --> Bot
    Sup --> WD
    DM --> Disc
    DM <--> Serial
    DM -- "SMSフラグメント" --> Buffer
    Buffer -- "結合されたSMS" --> DM
    DM -- "SMS転送" --> Bot
    Bot -- "SMS送信" --> DM
    Bot <--> API[Telegram API]

    DM -. "状態報告" .-> HS
    Bot -. "状態報告" .-> HS
    WD -. "停止継続時間" .-> HS
    HS -. "スナップショットファイル" .-> HC
```

`Supervisor` は独立した2つのコンポーネントを駆動します。各コンポーネントはそれぞれ接続・実行し、
失敗すると指数バックオフで再接続します。互いの完了を待つことはありません。
セッションが `SERVICE_STABLE_SECONDS` の間持続して初めて復旧とみなされるため、
接続直後に失敗を繰り返すコンポーネントは故障として扱われ続けます。
`HealthState` がその状態を記録し、いずれかが `WATCHDOG_DOWN_SECONDS` を超えて停止したままなら
ウォッチドッグがプロセスを終了させ、`healthcheck.py` が同じ状態をコンテナランタイムへ報告します。

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

モジュール接続後、Linuxは複数のシリアルポートデバイスを作成します：

```bash
ls -l /dev/ttyUSB*
```

通常、複数のデバイス（例：ttyUSB0、ttyUSB1、ttyUSB2など）が表示されますが、
AT命令を受け付けるのはそのうち1つだけです。
**どれかを自分で特定する必要は通常ありません**：サービスは起動時に各ポートを試し、
応答したものを採用します。詳細は「シリアルポートの選択」の節を参照してください。

ただしこの一覧にはもう1つ読む価値があります。日付の前にある2つの数字はデバイスの
メジャー番号とマイナー番号です。メジャー番号によってコンテナに必要な
`device_cgroup_rules` の項目が決まり、よく使われる2つ（`ttyUSB*` は188、
`ttyACM*` は166）はサンプルの構成ファイルにすでに含まれています。

### 3. デバイス競合の回避

一部のシステムサービスがモジュールのシリアルポートを占有している可能性があるため、ポートが利用可能であることを確認してください：

```bash
# シリアルポートを占有しているサービスを確認
lsof /dev/ttyUSB*

# 干渉する可能性のあるサービス（ModemManagerなど）を無効化
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

これは見た目以上に重要です。ポート検出は候補を排他モードで開き、
他のプロセスが保持しているポートは飛ばすため、ATポートを掴んだままの
モデムマネージャーがあると、モジュールは本サービスから完全に見えなくなります。

### 4. プライベートTelegramボットの作成

1. Telegramで[@BotFather](https://t.me/botfather)と対話して新しいボットを作成
2. 指示に従って作成プロセスを完了し、ボットのTOKENを取得
3. あなたのTelegramユーザーID (CHAT_ID)を取得：
   - [@userinfobot](https://t.me/userinfobot)と対話して取得
   - または他のCHAT_ID取得ボットを使用

詳細なチュートリアルは[Telegram Bot APIドキュメント](https://core.telegram.org/bots/api)を参照してください

### 5. プロジェクトの設定

1. Dockerイメージを取得します。`latest` イメージは `linux/amd64` と `linux/arm64` をサポートしています：

```bash
docker pull vxhorse/sms-forwarder
```

2. サニタイズ済みテンプレートからローカル設定ファイルを作成します：

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

3. 実際の環境に合わせて `.env` を編集します。

変更が必要なのは次の2つだけです：
- `BOT_TOKEN`: あなたのTelegramボットTokenに置き換え
- `CHAT_ID`: あなたのTelegramユーザーIDに置き換え

その他はすべて実用的な既定値を持ちます：
- `SMS_PORT`: 自動検出が誤ったデバイスを選ぶ場合を除き、空のままにします。詳細は「シリアルポートの選択」の節を参照
- `PROXY_URL`: 空ならTelegram APIへ直接接続します。必要な場合のみプロキシを設定（例：`http://127.0.0.1:7890`）
- 構成ファイルにデバイスパスを書く必要は一切ありません。詳細は「`devices:` を使わない理由」の節を参照

### 6. サービスの起動

```bash
docker compose up -d
```

起動を確認します：

```bash
docker compose ps          # ヘルス状態が starting から healthy に変わります
docker compose logs -f     # 起動シーケンスを追跡します
```

コンテナは最大3分間 `starting` を報告します。これは正常です：コンポーネントは
接続が `SERVICE_STABLE_SECONDS` の間持続して初めて稼働中と報告されるため、
最初のヘルス報告はそれより早くは届かず、その時点でモジュールがまだ列挙中である場合もあります。

## 設定

### 環境変数

すべての設定は環境変数から読み込まれ、すべてに既定値があります。実用的な `.env` に
必要なのは `BOT_TOKEN` と `CHAT_ID` だけです。時間の単位は秒です。

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | ログの詳細度 |
| `SMS_PORT` | *(空)* | モジュールのATポート。空なら自動検出 |
| `SMS_BAUDRATE` | `115200` | シリアル速度 |
| `SMS_DEV_ROOT` | `/dev` | 走査するデバイスツリー。構成ファイルでは `/hostdev` |
| `PORT_PROBE_TIMEOUT` | `3.0` | 候補ポートが `AT` に応答する制限時間 |
| `BOT_TOKEN` | *(プレースホルダ)* | TelegramボットToken |
| `CHAT_ID` | *(プレースホルダ)* | 転送先のTelegramチャット |
| `PROXY_URL` | *(空)* | Telegram API向けの送信プロキシ。空なら直接接続 |
| `NOTIFY_TIMEOUT` | `5.0` | 状態通知1回の制限時間。1〜10に制限されます |
| `RECONNECT_BACKOFF_MIN` | `1.0` | 再接続待機の最小値 |
| `RECONNECT_BACKOFF_MAX` | `30.0` | 再接続待機の最大値 |
| `SERVICE_STABLE_SECONDS` | `60.0` | 復旧とみなすまでのセッション継続時間。下限は5 |
| `MODEM_PROBE_INTERVAL` | `30.0` | 死活監視の間隔。`HEALTH_STALE_SECONDS` の半分が上限 |
| `MODEM_PROBE_TIMEOUT` | `5.0` | モジュールが監視に応答する制限時間 |
| `MODEM_PROBE_FAILURES` | `3` | 再接続を強制する連続失敗回数 |
| `AT_COMMAND_TIMEOUT` | `3.0` | AT命令1回の制限時間 |
| `AT_SLOW_COMMAND_TIMEOUT` | `10.0` | 低速な命令（`AT&F`、`AT+CFUN`、`AT&W`）の制限時間 |
| `HEALTH_FILE` | `/tmp/healthy` | ヘルスチェックが読むスナップショットファイル |
| `HEALTH_STALE_SECONDS` | `120` | そのスナップショットが古いとみなされるまでの時間。下限は2 |
| `WATCHDOG_DOWN_SECONDS` | `3600` | コンポーネント停止がこの時間を超えたら終了 |
| `WATCHDOG_CHECK_INTERVAL` | `30.0` | ウォッチドッグの確認間隔。下限は1 |

### シリアルポートの選択

`SMS_PORT` は空のままで構いません。起動時にサービスは候補となるシリアルポートを走査し、
`AT` に `OK` で応答した最初のポートを採用します：

1. `$SMS_DEV_ROOT/serial/by-id/*` —— 識別子が安定しており最優先
2. `$SMS_DEV_ROOT/ttyUSB*`
3. `$SMS_DEV_ROOT/ttyACM*`

オンボードのシリアルポート（`ttyS*`）は決して探索しません。多くのボードで
`ttyS0` はカーネルコンソールだからです。

これが重要なのは、この種のモジュールが複数のシリアルポートを公開し、
AT命令を受け付けるのはそのうち1つだけだからです。`SMS_PORT` を明示するのは、
モジュールを複数接続している場合か、デバイスが通常と異なる場所にある場合だけにしてください。

明示する場合は、サービスから見えるパスを書いてください。本リポジトリの構成ファイルでは
ホストの `/dev` が `/hostdev` にマウントされるため、ポートは `/dev/ttyUSB2` ではなく
`/hostdev/ttyUSB2` になります。

### `devices:` を使わない理由

構成ファイルは `devices:` マッピングを使わず、`/dev` をバインドマウントして
`device_cgroup_rules` でアクセスを許可します。

`devices:` の項目はコンテナの**作成時**に解決されます。その瞬間にデバイスが存在しなければ
作成そのものが失敗し、コンテナは実行状態に入らず、再起動ポリシーも適用されません
—— 再起動ポリシーは、一度実行されてから終了したコンテナだけを対象とするからです。
起動の速いマシンでは、USBモジュールの列挙が終わる前にコンテナランタイムが起動することは
容易に起こり、その場合コンテナは誰かが手動で起動するまで停止したままになります。

バインドマウントであれば、コンテナの作成はデバイスに依存しません。後から現れたデバイスは
自動的にコンテナ内にも現れ、サービスは指数バックオフでそれを待ちます。

モジュールがUSBシリアルデバイス（メジャー188）ではなくCDC-ACM（メジャー166）であっても、
どちらもすでに許可されています。`ls -l /dev/ttyUSB*` または `ls -l /dev/ttyACM*` で確認してください。

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

- **長文SMSサポート**：本サービスは長文SMSの自動結合に対応しており、分割されたSMSは60秒以内にすべての断片が到着するのを待って結合・転送されます
- **互換性**：異なるモデルのモジュールの互換性は異なり、一部のモジュールは長文SMSの送受信をサポートしていない場合があります
- **安定性**：各コンポーネントは指数バックオフで自律的に再接続し、`WATCHDOG_DOWN_SECONDS` を超えて停止したままならウォッチドッグがプロセスを再起動します
- **シリアルポートの選択**：まずは `SMS_PORT` を空にして自動検出に任せてください。誤ったデバイスが選ばれる場合のみ設定し、`SMS_DEV_ROOT` 配下のパスを指定します
- **ハードウェアがなくてもエラーではありません**：モジュール未接続なら、サービスは無期限に待機して再試行し、その間コンテナは不健全と報告されます
- **SIMカードの検出**：SIMカードが正しく挿入され、十分な残高があることを確認してください
- **ネットワーク依存**：Telegram通信には安定したネットワーク接続が必要です
- **ファイアウォール設定**：サーバーがTelegram APIのネットワーク接続を許可していることを確認してください

## トラブルシューティング

1. **SMSの送受信ができない**：
   - どのポートが検出されたかをログで確認：`docker compose logs | grep -i port`
   - SIMカードの状態（信号、残高があるか）を確認
   - ログを確認：`docker logs sms-forwarder`

2. **Telegram通信の問題**：
   - TOKENとCHAT_IDの設定を検証
   - ネットワーク接続とプロキシ設定を確認
   - ボットの権限設定が正しいことを確認

3. **モジュールが認識されない**：
   - ホストがデバイスを認識しているか確認：`ls -l /dev/ttyUSB*` と `dmesg | grep tty`
   - ホストからは見えてコンテナから見えない場合、一覧のメジャー番号が `device_cgroup_rules` に含まれているか確認
   - モジュールを挿した後に何かを再起動する必要はありません：サービスは待機しており、自動的に接続します
