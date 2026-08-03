# Vendor AT command manual

`Quectel_LTE_Standard(A)系列_AT命令手册_V1.1.pdf` is the vendor's AT command
reference for the Quectel LTE Standard (A) module family — the EC200T, EC200S,
EC200A, EC200N, EC600S, EC600N, EC800N, EG912Y and EG915N series listed in the
top-level [README](../README.md#hardware-requirements). It is kept here so that
anyone adapting this project to another module can look up what a command means
without hunting for the document first.

The manual is the vendor's copyrighted material, included for reference only.

The tables below map every AT command this project actually issues to what it is
for, so you can search the PDF for the command name and land in the right
section instead of reading it end to end. They are derived from
[`module/device_manager.py`](../module/device_manager.py) and
[`module/discovery.py`](../module/discovery.py); nothing here is issued anywhere
else.

## Startup sequence

`DeviceManager.SETUP_COMMANDS`, run in this order once per connection. The
ordering is load-bearing in one place: the store is read out after PDU mode and
the storage area have been selected, and before the store is erased.

| # | Command | What it does |
| --- | --- | --- |
| 1 | `AT&F` | Restore the factory default configuration |
| 2 | `ATE0` | Turn off command echo, so replies are not mixed with echoed commands |
| 3 | `AT+CFUN=1` | Full functionality: radio on |
| 4 | `AT+CMGF=0` | PDU message format rather than text mode |
| 5 | `AT+CSCS="UCS2"` | TE character set |
| 6 | `AT+CSMS=1` | SMS service phase 2+ |
| 7 | `AT+CREG=2` | Network registration URCs, with location information |
| 8 | `AT+CTZU=3` | Update the clock and time zone from the network |
| 9 | `AT+CTZR=0` | Do not report time zone changes |
| 10 | `AT+QCFG="urc/cache",0` | Vendor specific: do not cache URCs |
| 11 | `AT+QURCCFG="urcport","usbmodem"` | Vendor specific: emit URCs on the USB modem port |
| 12 | `AT+CPMS="ME","ME","ME"` | Preferred message storage: module memory for reading, writing and receiving |
| 13 | *(not a command)* | Placeholder that runs the stored-message drain; see below |
| 14 | `AT+CMGD=1,4` | Delete every message in the store |
| 15 | `AT+CNMI=2,2,0,0,0` | Deliver new messages straight to us rather than storing them |
| 16 | `AT+CSMP=17,167,0,8` | Text mode parameters, including long message support |
| 17 | `AT+CSDH=1` | Show the full message header fields |
| 18 | `AT+CMMS=2` | Keep the link to the network up between consecutive messages |
| 19 | `AT&W` | Store the settings to the module's profile |

Four of these are mandatory (`DeviceManager.REQUIRED_COMMANDS`): `AT+CFUN=1`,
`AT+CMGF=0`, `AT+CPMS` and `AT+CNMI`. A module that refuses one of them has no
usable degraded mode, so setup aborts and the connection is retried. Every other
command may be refused — a module that does not implement it logs a warning and
setup continues. That is what makes the two vendor specific entries safe on a
module from another vendor.

Three are slow enough to get a longer deadline
(`DeviceManager.SLOW_COMMANDS`): `AT&F`, `AT+CFUN=1` and `AT&W`. See
`AT_SLOW_COMMAND_TIMEOUT` in the top-level README.

## Commands issued outside the startup sequence

| Command | Issued by | What it does |
| --- | --- | --- |
| `AT` | Port discovery, and the handshake right after the port opens | Bare command. A port that answers `OK` is the module's AT port, and a module that answers has finished starting |
| `AT+CMGL=4` | The stored-message drain, at step 13 above | List every message already in the store, so messages that arrived while the process was down are forwarded before step 14 erases them |
| `AT+CSQ` | The liveness heartbeat, every `MODEM_PROBE_INTERVAL` | Signal quality. Asking a question the module must answer is the only way to tell a live module from an open port behind a wedged one |
| `AT+CMGS=<length>` | The send path | Announce an outgoing PDU of that length. The module answers with a prompt, the hex PDU is written, and Ctrl+Z (0x1A) submits it |

## Responses and URCs this project reads

| Line | Meaning |
| --- | --- |
| `OK` / `ERROR` | Terminating line of a command's response |
| `+CME ERROR:` / `+CMS ERROR:` | Terminating error lines, treated the same as `ERROR` |
| `+CMT:` | A new message is being delivered; the PDU follows on the next line |
| `+CMGL:` | One entry of a stored-message listing; its PDU follows on the next line |
| `+CMGS:` | The module accepted an outgoing message, and reports its reference number |
| `+CSQ:` | Signal quality reply, which is also the heartbeat's proof of life |
| `+CREG:` | Network registration state |

Any other line is logged as an unhandled line and otherwise ignored. Nothing in
this project writes a message body to the log, so an unrecognised line is
reported by length rather than quoted; look it up here by the URC name the
module's own documentation gives it.
