# Vendor AT command manual

The vendor's AT command reference for the Quectel LTE Standard (A) module
family — the EC200T, EC200S, EC200A, EC200N, EC600S, EC600N, EC800N, EG912Y and
EG915N series listed in the top-level
[README](../README.md#hardware-requirements) — is published as
*Quectel_LTE_Standard(A)系列_AT命令手册*. It is Quectel's copyrighted material,
so this repository does not redistribute it; download it from Quectel's own
document centre.

The tables below map every AT command this project actually issues to what it is
for, so you can search that manual for the command name and land in the right
section instead of reading it end to end. They are derived from
[`module/device_manager.py`](../module/device_manager.py) and
[`module/discovery.py`](../module/discovery.py); nothing here is issued anywhere
else.

## Startup sequence

`DeviceManager.SETUP_COMMANDS`, run in this order once per connection. The
ordering is load-bearing in one place: the store is read out after PDU mode and
the storage area have been selected, and before the store is erased, which only
happens if the read accounted for everything it could decode.

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
| 14 | `AT+CMGD=1,4` | Delete every message in the store. Skipped, with a warning, unless the drain at step 13 accounted for every entry it could decode — an undelivered message must outlive the erase |
| 15 | `AT+CNMI=2,2,0,0,0` | Deliver new messages straight to us rather than storing them |
| 16 | `AT+CSMP=17,167,0,8` | Text mode parameters, including long message support |
| 17 | `AT+CSDH=1` | Show the full message header fields |
| 18 | `AT+CMMS=2` | Keep the link to the network up between consecutive messages |
| 19 | `AT&W` | Store the settings to the module's profile |

Five of these abort setup when the module refuses them. Four are
`DeviceManager.REQUIRED_COMMANDS`: `AT+CFUN=1`, `AT+CMGF=0`, `AT+CPMS` and
`AT+CNMI`; a module that refuses one of those has no usable degraded mode. The
fifth is the stored-message listing `AT+CMGL=4` at step 13, which
`_drain_stored_sms` aborts on for a sharper reason — step 14 erases the store,
so a listing that was not acknowledged would destroy unread messages. Setup
aborts either way and the connection is retried. Every other command may be
refused — a module that does not implement it logs a warning and setup
continues. That is what makes the two vendor specific entries safe on a module
from another vendor.

Four commands get a longer deadline. Three of them are
`DeviceManager.SLOW_COMMANDS`: `AT&F`, `AT+CFUN=1` and `AT&W`. The fourth is the
stored-message listing `AT+CMGL=4` at step 13, which `_drain_stored_sms` gives
the same deadline without being an entry in that set. See
`AT_SLOW_COMMAND_TIMEOUT` in the top-level README.

## Commands issued outside the startup sequence

| Command | Issued by | What it does |
| --- | --- | --- |
| `AT` | Port discovery, and the handshake right after the port opens | Bare command. A port that answers `OK` is the module's AT port, and a module that answers has finished starting |
| `AT+CMGL=4` | The stored-message drain, at step 13 above | List every message already in the store, so messages that arrived while the process was down are forwarded before step 14 erases them — and step 14 is skipped outright if any decodable entry went undelivered |
| `AT+CSQ` | The liveness heartbeat, every `MODEM_PROBE_INTERVAL` | Signal quality. Asking a question the module must answer is the only way to tell a live module from an open port behind a wedged one |
| `AT+CREG?` | The same heartbeat, after each answered `AT+CSQ`, and only with `MODEM_REGISTRATION_CHECK=1` (off by default) | Network registration state. Answering proves the module is alive, not that it is attached to a network: a SIM the carrier has detached answers every command exactly as before while no message can arrive |
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
| `+CREG:` | Network registration state, in either of its two shapes: `+CREG: <n>,<stat>[,<lac>,<ci>[,<AcT>]]` in answer to `AT+CREG?`, and `+CREG: <stat>[,<lac>,<ci>[,<AcT>]]` when the module reports a change by itself. `<stat>` 1 and 5 are registered at home and roaming, 6 and 7 the same two limited to messages alone; under any other value a message cannot be delivered |

Any other line is logged as an unhandled line and otherwise ignored, with four
exceptions that are discarded in silence: an empty line, a bare space, the `OK`
above, and the `>` prompt `AT+CMGS` produces. None of them carries anything to
route, and the sending path watches for its own `+CMGS:` rather than for the
prompt.

Nothing in this project writes a message body to the log, so an unrecognised
line is reported by length, prefixed by its URC name only when that name is one
`_KNOWN_URCS` in [`module/device_manager.py`](../module/device_manager.py)
lists — a body can itself have the shape of a URC keyword, so a fixed set is
the only test text cannot satisfy. Look that name up in the vendor manual named
above. A line that arrives as a byte count with no name is a URC nobody has seen
yet: identify it in the manual and add its prefix to that set, which is a
one-line change. The bare responses of the AT command set (`RING`, `RDY`,
`NO CARRIER` and the rest of `_SAFE_BARE_RESPONSES`) are the exception and are
quoted whole, because they have no payload to leak.

## Known limitation of the registration check

`+CREG` describes the circuit-switched domain. A module that a network attaches
for packet service alone, with messages delivered over a path that domain does
not describe, reports one of the unregistered states while every message
arrives — and on firmware predating `<stat>` 6 and 7 there is no value that
expresses that case either. The heartbeat would then drop the session every few
minutes and reinitialise the radio each time, which is worse than not checking.

Because that failure cannot be distinguished from a real one without knowing
what the SIM and the network actually report, `MODEM_REGISTRATION_CHECK`
defaults to `0`. The state is still parsed, published in the health snapshot
and, at `LOG_LEVEL=DEBUG`, logged with the check off — setup asks the module to
report registration changes unasked — so the reading needed to make the
decision is available without the check acting on it. Watch the snapshot's
`registration` field over a period that includes ordinary message traffic: if
it settles on 1, 5, 6 or 7, set `MODEM_REGISTRATION_CHECK=1` and the heartbeat
will act on a run of unregistered readings. If it sits at 0 or 2 while messages
keep arriving, this is the network described above and the check must stay off.
If it stays `null`, nothing has read it: with the check off the field is
written only by the reports the module sends when its registration changes, and
a module that was already registered when setup ran may send none.
`LOG_LEVEL=DEBUG` shows every registration report as it is parsed; do not
decide the setting either way before you have a real value.

Asking `AT+CGREG?` and `AT+CEREG?` as well — on the failing path only, counting
a miss only when every domain agrees — is the complete answer, and it is not a
copy of the `+CREG` code; they need a shape rule of their own. See
`_REGISTERED_STATES` in
[`module/device_manager.py`](../module/device_manager.py) for why.
