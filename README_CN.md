# SMS_forwarder_Telegram (EC200)

该项目将GSM/LTE通信模块接收到的短信转发至Telegram机器人，同时支持通过Telegram发送短信。

## 说明文档
[English](README.md) | [日本語](README_JP.md) | [简体中文](README_CN.md) | [فارسی](README_FA.md)

## 功能特点

- 自动转发接收到的短信到Telegram
- 通过Telegram回复短信
- **长短信自动合并**：自动识别并合并分片短信，确保完整接收长文本
- 支持主流LTE模块（如EC200T/EC200S/EC200A等系列）
- Docker部署，易于安装和管理
- **串口自动发现**：自行探测模块的AT端口，无需配置设备路径
- **等待硬件就绪**：可在模块尚未枚举完成时启动，设备出现后立即接管
- **服务健康检查**：监督、健康上报与看门狗共同保证服务无人值守运行

## 系统架构

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
    Sup -- "监督与重连" --> DM
    Sup -- "监督与重连" --> Bot
    Sup --> WD
    DM --> Disc
    DM <--> Serial
    DM -- "SMS分片" --> Buffer
    Buffer -- "合并后短信" --> DM
    DM -- "转发短信" --> Bot
    Bot -- "发送短信" --> DM
    Bot <--> API[Telegram API]

    DM -. "上报状态" .-> HS
    Bot -. "上报状态" .-> HS
    WD -. "掉线时长" .-> HS
    HS -. "状态快照文件" .-> HC
```

`Supervisor` 驱动两个相互独立的组件。每个组件各自连接、运行，失败后按指数退避重连，
彼此不互相等待。只有当一次会话持续超过 `SERVICE_STABLE_SECONDS` 后，组件才算真正恢复，
因此「连上就立刻断开」的组件仍会被判定为故障。`HealthState` 记录这一状态，
看门狗会在组件失联的三种情形下退出进程——掉线时间超过 `WATCHDOG_DOWN_SECONDS`；
虽仍报告就绪、但其循环已停止推进达 `WATCHDOG_STALL_SECONDS`；
或在 `WATCHDOG_CHURN_WINDOW` 之内结束了 `WATCHDOG_CHURN_SESSIONS` 次已连接的会话；
`healthcheck.py` 则把同一状态报告给容器运行时。

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

连接模块后，Linux会创建多个串口设备：

```bash
ls -l /dev/ttyUSB*
```

通常会看到多个设备（例如ttyUSB0、ttyUSB1、ttyUSB2等），其中只有一个接受AT命令。
**一般无需自行判断是哪一个**：服务启动时会逐个探测，并保留能够应答的那个端口，
详见「串口选择」一节。

不过这条列表仍有一处值得留意：日期前的两个数字是设备的主设备号与次设备号。
主设备号决定容器需要哪一条 `device_cgroup_rules` 规则，
而两种常见取值（`ttyUSB*` 为 188，`ttyACM*` 为 166）已经写在示例编排文件中。

### 3. 避免设备冲突

某些系统服务可能会占用模块串口，需确保端口可用：

```bash
# 检查是否有服务占用串口
lsof /dev/ttyUSB*

# 禁用可能干扰的服务（如ModemManager）
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```

这一步比看上去更重要：端口探测以独占方式打开候选设备，并跳过已被其他进程占用的端口，
因此一个占着AT端口的调制解调器管理服务会让模块对本服务完全不可见。

### 4. 创建私有Telegram机器人

1. 在Telegram中，与[@BotFather](https://t.me/botfather)对话创建新机器人
2. 按照指引完成创建流程，获取机器人TOKEN
3. 获取您的Telegram用户ID (CHAT_ID)：
   - 与[@userinfobot](https://t.me/userinfobot)对话获取
   - 或通过其他CHAT_ID获取机器人发送消息

详细教程可参考[Telegram Bot API文档](https://core.telegram.org/bots/api)

### 5. 配置项目

1. 拉取Docker镜像。`latest` 镜像已支持 `linux/amd64` 和 `linux/arm64`：

```bash
docker pull vxhorse/sms-forwarder
```

也可以直接用本仓库构建镜像，刚克隆下来时通常这样做：

```bash
docker build -t sms-forwarder .
```

若自行构建，请在 `docker-compose.yml` 中把 `image:` 改为 `sms-forwarder`。

2. 从脱敏模板创建本地配置文件：

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

3. 根据实际环境编辑 `.env`。

必须修改的只有两项：
- `BOT_TOKEN`: 替换为您的Telegram机器人Token
- `CHAT_ID`: 替换为您的Telegram用户ID

其余各项都有可用的默认值：
- `SMS_PORT`: 保持为空即可，只有当自动发现选错设备时才需要设置，详见「串口选择」一节
- `PROXY_URL`: 留空表示直连Telegram API；如需代理再填写（例如 `http://127.0.0.1:7890`）
- 编排文件中完全不需要写设备路径，详见「为什么不使用 `devices:`」一节

### 6. 启动服务

```bash
docker compose up -d
```

确认启动情况：

```bash
docker compose ps          # 健康状态由 starting 变为 healthy
docker compose logs -f     # 跟踪启动过程
```

容器最多会显示三分钟的 `starting`，这是正常现象。
各个健康状态的含义以及查看方法，详见「读懂健康检查」一节。

## 配置说明

### 环境变量

全部配置均从环境变量读取，且都有默认值。可用的 `.env` 只需填写
`BOT_TOKEN` 与 `CHAT_ID`。时间单位均为秒。

**边界**一列是约定的一部分，而不是附注。凡是取值会让某项守卫失效、
或让该守卫所防的故障重新出现的设置，都会被钳制；启动日志会点名每一个被边界从
**你实际设过的值**上挪开的设置。若被边界挪动的是推导出的默认值，则不会播报
——那会变成每次启动都念一遍没人问过的算术——所以启动时没有动静，只说明你设过的值没有被覆盖，
并不说明没有任何东西被钳制。没有上限的设置会明说没有、以及为什么没有，
这样「未设边界」就绝不会被误读成「漏写了」。

| 变量 | 默认值 | 用途 | 边界 |
| --- | --- | --- | --- |
| `LOG_LEVEL` | `INFO` | 日志级别 | 无——这是级别名而非数值，无法识别的名称回落为 `INFO` |
| `SMS_PORT` | *(空)* | 模块AT端口，留空表示自动发现 | 无——这是一个路径 |
| `SMS_BAUDRATE` | `115200` | 串口波特率 | 无。与模块不匹配的速率表现为「该端口从不应答 `AT`」，自动发现会在日志中点名 |
| `SMS_DEV_ROOT` | `/dev` | 扫描设备的根目录，编排文件中设为 `/hostdev` | 无——这是一个路径 |
| `PORT_PROBE_TIMEOUT` | `3.0` | 候选端口应答 `AT` 的时限 | 故意无界。端口探测发生在任何会话存在之前，而「等待硬件」是本服务从不设时限的事 |
| `BOT_TOKEN` | *(占位符)* | Telegram机器人Token | 无——这是凭据。凭据没有边界，也绝不会出现在启动通知里 |
| `CHAT_ID` | *(占位符)* | 接收转发短信的Telegram会话 | 无——同上，这是凭据 |
| `PROXY_URL` | *(空)* | 访问Telegram API的出站代理，留空表示直连 | 无——这是一个URL |
| `NOTIFY_TIMEOUT` | `5.0` | 单次状态通知的时限 | 下限 1，上限为十秒的关停预算——它与 `SERIAL_CLOSE_TIMEOUT` 共用这一份预算：关停路径上先后等待这两者，两者之和不得超过常见运行时中最短的那个停机宽限期 |
| `RECONNECT_BACKOFF_MIN` | `1.0` | 重连退避的最小间隔 | 无界。这一对的次序由另一侧保证——被钳制的是 `RECONNECT_BACKOFF_MAX` |
| `RECONNECT_BACKOFF_MAX` | `30.0` | 重连退避的最大间隔 | 下限为 `RECONNECT_BACKOFF_MIN`：最大值低于最小值会让每次重试的等待越缩越短，而不是越拉越长。故意不设上限——等得更久只是推迟重连，而 `WATCHDOG_CHURN_WINDOW` 的下限会随之增大，重连频次判据因此不会掉队 |
| `SERVICE_STABLE_SECONDS` | `60.0` | 会话持续多久才算恢复 | 下限 5：取零时，连上的那一瞬间就算恢复，而这正是本设置要防的事。故意不设上限——窗口更长只会让「算作恢复」更难达成 |
| `MODEM_PROBE_INTERVAL` | `30.0` | 心跳探测间隔 | 下限 1，上限为 `HEALTH_STALE_SECONDS` 的一半（当前默认 60）。刷新快照的正是这个探测，所以仅凭这一条循环也必须能让文件保持新鲜 |
| `MODEM_PROBE_TIMEOUT` | `5.0` | 模块应答一次探测的时限 | 下限 1，上限为 `(HEALTH_STALE_SECONDS − MODEM_PROBE_FAILURES × MODEM_PROBE_INTERVAL) / (MODEM_PROBE_FAILURES + 1)`，**当前默认为 7.5**。模块应答慢时正是要调大它，而一旦越过该值，两次快照刷新之间最坏的间隔就会超出 `HEALTH_STALE_SECONDS`，健康检查会把正常工作的进程判为失败 |
| `MODEM_PROBE_FAILURES` | `3` | 连续丢失多少次探测后触发重连 | 下限 1：计数达到该值时循环即抛错，所以取零与取一行为完全相同，却看起来像个开关。故意不设上限——更有耐心只是推迟重连，且 `MODEM_PROBE_TIMEOUT` 的上限会随它增大而收紧 |
| `MODEM_REGISTRATION_CHECK` | `0` | 是否额外询问模块是否已驻网。默认关闭——见[驻网检查](#驻网检查) | 只有开与关，不是数值。它与下面的次数分成两个设置，正是为了让「调整耐心」不会顺手把这项守卫关掉 |
| `MODEM_REGISTRATION_FAILURES` | `3` | 开启该检查后，连续多少次读到「未驻网」触发重连 | 下限 2：小区切换时驻网状态会短暂中断，单次读数说明不了任何事；取零则会在看起来像设置的同时把检查关掉。故意不设上限——更有耐心只是推迟会话结束 |
| `AT_COMMAND_TIMEOUT` | `3.0` | 单条AT命令的时限 | 故意无界。它约束的是一条命令而非整个循环；某条命令若拖过心跳探测自身的时限，会由该探测及其触发的重连接手 |
| `AT_SLOW_COMMAND_TIMEOUT` | `10.0` | 慢速命令（`AT&F`、`AT+CFUN`、`AT&W`）的时限 | 故意无界，理由同上。这些命令在初始化阶段执行，而这一段等待本服务从不设时限 |
| `SERIAL_CLOSE_TIMEOUT` | `5.0` | 释放串口时等待缓冲区排空的时限，超时后强制关闭 | 下限 1，上限为同一份十秒关停预算中 `NOTIFY_TIMEOUT` 用剩的部分——默认值恰好用满：5 + 5 = 10。当下限与该上限相撞时以下限为准，并由启动通知说明该预算已不再成立 |
| `HEALTH_FILE` | `/tmp/healthy` | 健康检查读取的状态快照文件 | 无——这是一个路径。写不进去的路径只会被报出、而不会触发重启，见[读懂健康检查](#读懂健康检查) |
| `HEALTH_STALE_SECONDS` | `120` | 该快照的最大允许陈旧时间 | 下限 2：`MODEM_PROBE_INTERVAL` 被钳到它的一半但绝不低于 1，窗口更短就会短于可能的最快刷新间隔。故意不设上限——它表达的是运维者对「快照可以多陈旧」的容忍度，而两个探测上限都由它推导得出，放宽它会让二者同步放宽 |
| `WATCHDOG_DOWN_SECONDS` | `3600` | 组件掉线超过该时长后退出进程 | 故意无界。它自身就是 `WATCHDOG_STALL_SECONDS` 的上限；若被设到该设置推导下限之下，以下限为准，并由启动通知说明该不变量对这份配置已不成立 |
| `WATCHDOG_STALL_SECONDS` | *(推导得出)* | 组件循环虽仍报告就绪却停止推进达该时长后退出进程 | 默认取 `HEALTH_STALE_SECONDS` 的两倍，再由推导出的下限抬高（**当前默认约 310**），并以 `WATCHDOG_DOWN_SECONDS` 为上限，下限最后生效。低于该下限，看门狗会重启一个只是在耐心等待慢模块的进程 |
| `WATCHDOG_CHECK_INTERVAL` | `30.0` | 看门狗检查间隔 | 下限 1：取零会把看门狗变成忙等循环。故意不设上限——它只是采样频率，每条阈值都以各自的时钟度量、而非以本循环采样的疏密度量，所以看门狗变慢只会推迟重启，不会漏掉 |
| `WATCHDOG_CHURN_SESSIONS` | `10` | 单个组件在下面这个窗口内结束多少次已连接会话后退出进程 | 下限 2：一次重连算不上模式，取零或一时单次瞬时故障就会结束进程。故意不设上限——阈值更高只会让判据更迟钝，且 `WATCHDOG_CHURN_WINDOW` 的下限按它成倍放大，窗口不会跟不上要容纳的次数 |
| `WATCHDOG_CHURN_WINDOW` | `1800.0` | 统计这些会话的时间窗 | 下限为 `WATCHDOG_CHURN_SESSIONS × (RECONNECT_BACKOFF_MAX + 组件抛错最慢所需的时间)`，**当前默认为 1400**。低于该值，计数永远到不了阈值，判据会在看起来仍启用的同时形同关闭——与本表中其他下限不同，这是一条防漏报的下限。**故意不设上限**：放宽它只会让判据按运维者要求的比例变得更积极，任何取值都不会关掉某项守卫或让某种故障重新出现，而这正是本表中每一条上限存在的理由 |

上表中凡是写成公式、或写成另一个设置名字的边界，都是推导得出而非固定的：
它所依赖的任何东西一动，它就跟着动。这一列大部分都是推导得出的，
所以请把边界读作一种关系，而不是一个数字。

最需要留意的是 `WATCHDOG_CHURN_WINDOW` 的下限：它越过 1800 这个默认值所需的余量
比两个数字看上去要小，而且运维者动过的那个设置本身完全看不出会引起这件事。
`RECONNECT_BACKOFF_MAX=90` 会把下限推到 2000，且文件里没有别的东西会对它有反应。
放慢探测计划也是一样——`MODEM_PROBE_INTERVAL=50` 把下限推到 1840，`=60` 推到 2140
——不过这两种配置整体上并不安静：它们同时会把 `MODEM_PROBE_TIMEOUT` 的上限压垮，
将其钳到 1，并报出「最坏刷新间隔已装不进 `HEALTH_STALE_SECONDS`」。
无论哪种情况，实际生效的值都是安全的，因为以下限为准。真正静默的只有重连窗口本身，
而且只在它完全不设时才静默：任何自己设过它的人——包括所有照抄 `.env.example` 的人
——都会收到点名该覆盖的启动通知，因为那时被抬高的是他们的值，而不是默认值。

### 串口选择

`SMS_PORT` 可以留空。服务启动时会扫描候选串口，
并保留第一个用 `OK` 应答 `AT` 的端口：

1. `$SMS_DEV_ROOT/serial/by-id/*` —— 标识稳定，优先尝试
2. `$SMS_DEV_ROOT/ttyUSB*`
3. `$SMS_DEV_ROOT/ttyACM*`

板载串口（`ttyS*`）永远不会被探测，因为在许多板子上 `ttyS0` 是内核控制台。

这一点很重要：这类模块会暴露多个串口，其中只有一个接受AT命令。
只有当您接入了多个模块，或设备位于不寻常的位置时，才需要显式设置 `SMS_PORT`。

如果确实要设置，请按服务自身看到的路径填写。使用本仓库的编排文件时，
主机的 `/dev` 被挂载到 `/hostdev`，因此端口是 `/hostdev/ttyUSB2`，而不是 `/dev/ttyUSB2`。

### 驻网检查

心跳能证明模块还在应答，却不能证明短信能送达它：被运营商摘网的SIM卡，
对每一条命令的应答与之前一模一样，而短信一条也收不到。
`MODEM_REGISTRATION_CHECK=1` 会加问第二个问题 `AT+CREG?`，
并在连续 `MODEM_REGISTRATION_FAILURES` 次读到「未驻网」后结束当前会话。

它**默认关闭**，因为这个问题并非在所有网络上都有真实答案。
`+CREG` 描述的是电路域，因此当网络只为模块建立分组域附着时
——短信经由该域无法描述的路径送达——它会在一切正常的同时报告「未驻网」。
据此动作会每隔几分钟就中断一次本来正常的会话，每次都重新初始化射频
（这只会拖长真正的断网恢复，而不是缩短），并且每个周期都改写模块的存储配置。
这个循环从外部也很难看见：它触发失败的时间晚于一次会话被判定为恢复的时间，
因此每个周期都会把掉线计时与停滞计时清零；而快照仅在一次拆链的时间窗内变陈旧，
所以容器健康检查在两个周期之间保持绿色。重连频次判据确实会把这些周期计入，
并在累计到 `WATCHDOG_CHURN_SESSIONS` 次时结束进程——也就是说，在这个问题没有真实答案的网络上，
打开该检查换来的是一个重启循环，而不是一个正常工作的服务。

在弄清情况之前保持关闭并不损失任何诊断能力。启动流程已要求模块主动上报驻网变化，
因此无论开关与否，状态都会被解析、写入快照并记入日志；被推迟的只是
「是否因此结束会话」这一动作。请在包含正常短信往来的一段时间内观察 `registration` 字段：

```bash
docker compose exec sms-forwarder cat /tmp/healthy
```

- 稳定为 `1`、`5`、`6` 或 `7`（本地驻网、漫游驻网，或二者仅限短信的形式）
  ——说明这个问题在该网络上有真实答案，此时 `MODEM_REGISTRATION_CHECK=1`
  才能带来它设计中的检测能力。
- 在短信持续到达的同时停留在 `0` 或 `2` ——说明这正是该检查无法描述的网络，请保持关闭。

[`doc/README.md`](doc/README.md) 记录了要给出完整答案还需要做什么。

### 为什么不使用 `devices:`

编排文件采用绑定挂载 `/dev` 并通过 `device_cgroup_rules` 授权的方式，
而不是使用 `devices:` 映射。

`devices:` 条目是在容器**创建**时解析的。如果那一刻设备尚不存在，创建就会失败，
容器根本不会进入运行状态，重启策略也就永远不会生效
—— 重启策略只覆盖那些已经运行过、随后退出的容器。
在启动较快的机器上，容器运行时很容易早于USB模块完成枚举，
此时容器会一直处于停止状态，直到有人手动启动它。

改用绑定挂载后，容器创建不再依赖设备是否存在。之后才出现的设备会自动出现在容器内，
服务则以指数退避的方式等待它。

如果您的模块不是USB串口设备（主设备号 188）而是CDC-ACM设备（主设备号 166），
两者都已在允许之列。可用 `ls -l /dev/ttyUSB*` 或 `ls -l /dev/ttyACM*` 确认。

### 读懂健康检查

`healthcheck.py` 只回答一个问题——此刻这个进程能否转发短信——
而答案为「能」需要同时满足两点：

1. `HEALTH_FILE` 指向的状态快照文件，写入时间距今不足 `HEALTH_STALE_SECONDS`，
   这说明进程的各个循环仍在运行。
2. 该文件记录的每个组件都处于就绪状态，这说明进程既能访问模块，也能访问 Telegram API。

只有全部组件就绪时才会写入该文件，且每个组件都会从自己的循环中刷新它，
因此进程一旦不再运行就不会再刷新，遗留下来的文件自然变陈旧。
仅仅文件新鲜、或者文件存在，都不会被当作健康。

快照中还有一个 `reconnects` 对象：逐个组件给出它在 `WATCHDOG_CHURN_WINDOW`
之内结束了多少次已连接的会话。**只有计数**——没有时间戳、没有原因，
也没有任何来自短信本身的内容——因为建立在它之上的判据看的就只是这个数字，
而这里是看门狗据此动作之前唯一能看到它的地方。条目一旦超出窗口就会自行淘汰，
所以不再出故障的组件会自己回到 `0`。某个数字正在逼近 `WATCHDOG_CHURN_SESSIONS`，
就是进程因此退出之前你能得到的预警。

查看容器运行时当前的判断：

```bash
docker inspect --format '{{.State.Health.Status}}' sms-forwarder
docker inspect --format '{{json .State.Health.Log}}' sms-forwarder
```

也可以手动执行同一项检查并读取退出码——`0` 表示健康，`1` 表示不健康：

```bash
docker compose exec sms-forwarder python /app/healthcheck.py; echo $?
```

有三种状态值得辨认：

- **`starting`**——仍处于 `start_period` 之内，示例编排文件将其设为 180 秒。
  组件必须在会话持续 `SERVICE_STABLE_SECONDS`（默认 60 秒）之后才会被标记为就绪，
  而快照要等到全部组件都就绪才写入，因此最早也要在两者都连上之后再过一分钟。
  `start_period` 必须覆盖这段时间，再加上模块枚举所需的时间；模块较慢时请调大它。
- **`unhealthy`**——此时没有任何短信被转发。请注意它本身不会重启任何东西，
  容器运行时只是把它记录下来。恢复要么靠服务自行重连，要么靠看门狗退出进程、
  再由重启策略接手。看门狗有三条退出路径，各自问的问题不同——这个组件此刻处于什么状态、
  它的循环还在不在动、以及它最近多久处于那个状态一次——正因如此，
  某一条看不见的故障才会被另一条看见。前两条永远不会同时成立，
  因为停滞读数只在没有任何组件掉线时才取。第三条故意不这样设门槛：
  反复重连的组件有相当一部分时间处于掉线状态，
  非要等到「没有任何组件掉线」的那一刻再看，正好会把它要找的东西采样掉。
  哪条阈值先到，就由哪条结束进程。
  - **明确失败**的组件会被标记为掉线，掉线满 `WATCHDOG_DOWN_SECONDS`
    （默认一小时）后进程退出。日志为
    `Watchdog tripped: a component has been down for ...`。
  - **阻塞但未失败**的组件仍被标记为就绪，上面那个计时因此根本不会开始。
    真正抓住它的是 `WATCHDOG_STALL_SECONDS`：该组件自己的循环已有这么久没有上报推进。
    按当前默认约为 **310 秒**而非一小时，因此「安静下来大约五分钟后」的意外重启属于这一条，
    而不是第一条。日志为
    `Watchdog tripped: a component loop has not advanced for ...`。
  - **反复失败又反复恢复**的组件，从构造上就对上面两条不可见。
    一次持续 `SERVICE_STABLE_SECONDS` 的会话即算作恢复，而恢复会把上面两个计时都清零
    ——所以抛错慢于这一分钟的故障，总是先到达「看起来已恢复」那一刻、再到达「失败」那一刻，
    于是可以永远循环下去，其间掉线计时为零、停滞计时为零，容器还报告健康。
    抓住它的不是计时而是计数：单个组件在 `WATCHDOG_CHURN_WINDOW` 之内结束了
    `WATCHDOG_CHURN_SESSIONS` 次已连接的会话，按当前默认是半小时内十次。日志为
    `Watchdog tripped: component <name> has ended N connected sessions within ...`。
    只有真正连上过的会话才计数，所以尚未插入的模块依旧会被无限期等待。
- **`healthy`**——快照是新鲜的，且全部组件均已就绪。

有一种故障**故意不**触发任何重启：快照文件写不出去。容器会变为 `unhealthy`，
因为 `healthcheck.py` 读的正是这个文件自身的 mtime，而它已经不再变化。
日志里会有一条 ERROR——`HEALTH_FILE has not been written for ...`——
重复频率不高于停滞阈值。进程会继续运行，也继续转发短信。

这是预期结果，不是缺口。所有组件循环都仍在上报推进，这正是让这条读数可归因的原因：
失败的是写入这一步本身，而重启没法把一个写不进去的路径变成可写。重启只会每隔几分钟
就把整个启动流程重来一遍，每次都重新初始化模块并重读它存储的短信，这比它所应对的故障
本身更糟。最常见的原因是给容器加了只读根文件系统却没有给 `/tmp` 挂上可写卷，
而 `HEALTH_FILE` 默认就是 `/tmp/healthy`：给 `/tmp` 挂一个 tmpfs，
或把 `HEALTH_FILE` 指到进程能写的位置。

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

- **长短信支持**：本服务已支持长短信自动合并，分片短信会在60秒内等待所有分片到达后合并转发
- **兼容性**：不同型号的模块兼容性不同，某些模块可能不支持长文本短信的收发
- **稳定性**：各组件独立按指数退避重连；看门狗在三种信号下重启进程——某组件掉线超过 `WATCHDOG_DOWN_SECONDS`；某组件循环虽仍报告就绪却停止推进达 `WATCHDOG_STALL_SECONDS`；或单个组件在 `WATCHDOG_CHURN_WINDOW` 之内结束了 `WATCHDOG_CHURN_SESSIONS` 次已连接的会话。健康快照写不出去是唯一一种只报出、不重启的故障
- **串口选择**：优先让 `SMS_PORT` 保持为空，由自动发现决定；只有当发现选错设备时才设置，并填写 `SMS_DEV_ROOT` 之下的路径
- **没有硬件不算错误**：未接入模块时，服务会无限期等待并重试，其间容器报告为不健康
- **SIM卡检测**：确保SIM卡正确插入并有足够余额
- **网络依赖**：Telegram通信需要稳定的网络连接
- **防火墙设置**：确保服务器允许Telegram API的网络连接

## 故障排除

1. **短信无法收发**：
   - 在日志中确认实际发现的端口：`docker compose logs | grep -i port`
   - 确认SIM卡状态（是否有信号、余额）
   - 查看日志：`docker logs sms-forwarder`

2. **Telegram通信问题**：
   - 验证TOKEN和CHAT_ID配置
   - 检查网络连接和代理设置
   - 确认机器人权限设置正确

3. **模块无法识别**：
   - 先看自动发现报告了什么：`docker compose logs | grep -iE 'candidate|discovered|answered'`。
     `Discovered modem AT port: ...` 表示成功；`No candidate port answered AT`
     表示所有候选端口都探测过但无人应答；`No candidate serial ports under ...`
     表示根本没有可供探测的端口
   - 确认主机能看到设备：`ls -l /dev/ttyUSB*` 与 `dmesg | grep tty`
   - 若主机能看到而容器看不到，检查列表中的主设备号是否已被 `device_cgroup_rules` 覆盖
   - 若确实探测过候选端口却无人应答，最常见的原因是别的进程占用了AT端口，详见「避免设备冲突」一节
   - 若模块的AT端口既不是 `ttyUSB*` 也不是 `ttyACM*`，它永远不会被探测；
     请显式设置 `SMS_PORT`，并填写 `SMS_DEV_ROOT` 之下的路径
   - 插入模块后无需重启任何东西：服务本就在等待，会自行接管

4. **本项目尚未验证过的模块**：
   - 初始化序列中有两条厂商专有命令（`AT+QCFG`、`AT+QURCCFG`）。不支持它们的模块会记录
     `... was not acknowledged; continuing setup` 并继续执行，这是无害的
   - 只有四条命令是必需的：`AT+CFUN=1`、`AT+CMGF=0`、`AT+CPMS` 与 `AT+CNMI`。
     日志出现 `Modem did not acknowledge <command>` 并随即重连，说明其中一条被拒绝，
     该模块按现状无法驱动
   - [`doc/README.md`](doc/README.md) 列出了本服务发出的每一条命令及其用途，
     可据此在您自己模块的AT命令手册中逐条查阅

5. **容器始终不进入健康状态**：
   - 最多三分钟的 `starting` 属于正常，详见「读懂健康检查」一节
   - 一直是 `unhealthy` 说明有组件掉线，日志会指出是哪一个：`Component <name> failed ...`
   - 容器运行时不会因为不健康而自行重启容器。看门狗会退出进程——组件已失败的
     走 `WATCHDOG_DOWN_SECONDS`，未失败却停止推进的走短得多的
     `WATCHDOG_STALL_SECONDS`，某个组件在 `WATCHDOG_CHURN_WINDOW` 之内结束了
     `WATCHDOG_CHURN_SESSIONS` 次已连接会话的则立即退出——随后由重启策略接手。
     `docker compose logs | grep 'Watchdog tripped'` 可看出是哪一条
   - 若容器不健康、而日志里出现的是 `HEALTH_FILE has not been written for ...`，
     这是唯一一种故意不重启的情形：循环都在跑，只是快照文件写不进去。
     详见「读懂健康检查」一节
