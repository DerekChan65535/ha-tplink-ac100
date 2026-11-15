# Security policy / 安全策略

## Supported versions

Security fixes target the latest published stable release. Older releases are not
promised security backports. Before the first stable release, the code is experimental
and no release has a supported-production status.

Home Assistant **2026.11.1+** is required. Read-only hardware checks cover TL-AC100
hardware **5.0**, firmware **1.0.5 Build 250814 Rel.82234n**; other hardware/firmware is unverified.
An HA or firmware update may change this boundary. Consult the release notes.

## Report a vulnerability privately

GitHub private vulnerability reporting is **enabled** for this repository. Use
**Security → Report a vulnerability**:

https://github.com/DerekChan65535/ha-tplink-ac100/security/advisories/new

If the private-reporting route is unavailable, open a public issue containing **only**
a request for a private contact channel. Do not describe an exploit or include secrets
in that issue. Do not publish a credential-bearing proof of concept. No response-time
SLA is promised for this volunteer project.

In a private report, include affected integration/HA/hardware/firmware versions,
expected versus actual behavior, impact, and minimal **redacted** reproduction steps.
Do not send a user's credentials or real client records, even privately, unless a
maintainer explicitly requests a secure, necessary, limited exchange.

Ordinary non-security bugs belong in the public issue tracker with redacted logs.

## Trust boundary and credentials

- The integration runs inside HA with HA's privileges. Install only code you trust.
  HACS inclusion and passing CI are not a security audit.
- Runtime requests go to the configured local controller. Keep HA and controller
  management on a trusted network; never expose the management interface to the Internet.
- A device HTTP interface is not encrypted. The integration cannot turn a legacy
  management protocol into a secure transport. Do not disable TLS verification as a
  default workaround for certificate problems.
- Use a dedicated least-privilege account if supported. If the device requires an
  administrator account, the integration holds that account's privileges.
- HA config entries and backups may contain credentials. They are not a separate secret
  vault. Restrict access, protect backups, and rotate credentials after a suspected leak.
- No user credentials, cookies, session keys, raw authentication exchanges, packet
  captures, controller dumps, or identifiable client/MAC/IP/SSID records belong in
  the public repository, issues, CI logs, or release assets.
- Debug logs and diagnostics must redact secrets and identifying data. Do not upload
  raw controller responses. The project does not need telemetry or cloud uploads to
  operate; GitHub/HACS and Python dependency downloads may use Internet access on install.

## Debug logging allowlist

The native **Debug level logging** diagnostic switch defaults to off and is independent
of device management. Its state is not persisted: HA restart starts it off. It works
while the controller is offline and does not call a device API. Enabling it saves
current levels and changes `custom_components.tplink_ac100` **and its child loggers**
to `DEBUG`. Disabling it restores prior non-`DEBUG` levels; any logger in this namespace
that would remain at `DEBUG` is set to `INFO`, preventing an explicit child-logger
level from bypassing the off state. HA's root logger and other integrations' loggers
are not changed.

Integration log fields are restricted to this allowlist:

- Fixed operation/stage names and success/failure indicators.
- Fixed error categories or exception class names, not arbitrary exception text.
- Elapsed time and aggregate counts, without per-device/client identifiers.

Do not log raw request/response bodies, authentication payloads, passwords, hashes,
cookies, session tokens/keys, credential-bearing URLs, or device/client names, MAC/IP
addresses, SSIDs, or arbitrary strings returned by the controller. More verbose logging
must not bypass this rule; do not add a raw-response debug dump. This policy covers
this integration, not every HA logger. Review all exported logs before sharing them,
and turn debug logging off when troubleshooting is complete.

## Controller session ownership

The tested firmware permits only **one management session at a time**. Monitoring
occupies that session even if optional management controls are disabled. Sign out of
other controller management clients before configuring HA. To use the **AC web interface**,
**disable, rather than delete, the HA entry first**. Sign out of the web interface when
finished, then re-enable the entry.

Changing the connection address through HA's Reconfigure flow does not require manual
disabling. Reconfigure and reauthentication temporarily unload only the affected entry
before validation; failed validation, a different controller, or cancellation automatically
restores the original entry. Configuration validation releases its temporary login
session. Entry unload/reload releases only that entry's own token. The integration does not use `force=1` or
intentionally evict other administrators. A busy/session-in-use response is not an
invalid-password response: wait for the other session to exit or expire before retrying.

## Management operations

Monitoring is the default. Management is **off by default**, and reboot button entities
are **disabled by default**. Interface evidence and mock tests do not establish that a
write is safe on any firmware. Initial LED checks exposed asymmetric values: readback
`1` means on, `2` off, and `0` unknown; writes use `1` for on and `0` for off. The original
on state has been restored and checked against HA, but the corrected native HA off/on
cycle **still awaits final validation**. AP/controller reboot has not been tested on
live hardware. SSID changes are not included initially.

Some APs link LED state with Wi-Fi. Before **each** LED write, the integration must
confirm `led_wifi_sync` is explicitly **off**; on, missing, or unknown blocks the write.
It never changes that linkage option or wireless settings itself. Do not assume an LED
operation is unconditionally non-disruptive. AP `link_status=0` means connecting, not
online; only `link_status=1` contributes to the online AP count.

The safety query runs **only on an LED action**, not during normal read-only polling.
It requests exactly one AP configuration row using an exact `entry_id` filter. The
controller cannot project individual fields, so the row may include AP management
fields beyond `led_wifi_sync`; do not claim those fields are never read. Process the
row only transiently in HA memory, extract only `led_wifi_sync`, then discard the raw
row. It must never enter logs, diagnostics, disk storage, or retained caches. Default
read-only polling does not query `apmng_set`. A missing/ambiguous safety result must
block the LED write, not trigger a change to the linkage setting.

Do not exercise reboots on production Wi-Fi to validate this project. Do not attach
management actions to unattended automations before independently checking their
scope and recovery plan. A timeout does not prove a command failed; blind retries of
write operations can repeat a disruptive action. Restoring an HA backup does not undo
a change that was already applied to the controller.

## Handling a leak or security fix

1. Revoke or rotate exposed controller credentials and sessions immediately.
2. Remove affected public attachments and coordinate Git history/cache cleanup as
   needed; deleting a file in the latest commit does not erase previous copies.
3. Use a private advisory for coordinated fixes. Publish affected and fixed version
   ranges when it is safe to disclose; do not silently replace a published release/tag.
4. Recheck logs, diagnostics, fixtures, and backups for the same exposure.

## 中文摘要

安全修复以最新稳定版为目标，旧版不承诺回移。首个稳定版前均属实验状态。
本仓库已启用 GitHub 私密漏洞报告，请优先使用上面的私密入口。若入口不可用，
公开 Issue **只能请求私密联络方式**，不要写漏洞细节、凭据或利用步骤。本志愿项目不承诺固定响应时限。

集成在 HA 进程内运行；HACS 收录和 CI 通过都不是安全审计。控制器应在受信管理网内，
不要开放公网管理端口。HTTP 接口本身不加密，HA 配置项与备份可能保存密码，需控制读取权限。
日志、诊断、抓包和 Issue 都不得含密码、cookie、会话密钥或真实客户端记录。

原生 **Debug level logging** 诊断开关独立于设备管理，默认关闭且不持久化，HA 重启后关闭。
它只调整 `custom_components.tplink_ac100` 及其子 logger：开启时保存原级别并统一设为 `DEBUG`；
关闭时恢复原非 `DEBUG` 级别，本命名空间内仍为 `DEBUG` 的 logger 压至 `INFO`，
避免显式子 logger 级别绕过关闭状态。控制器离线也能切换，不调用设备 API，不改变 root 或其他集成 logger。
日志白名单仅允许固定操作/阶段、成功/失败、固定错误类别或异常类名、耗时与无标识的汇总数量；
不得输出任意异常文本、设备返回字符串、原始请求/响应、认证数据、密码/哈希、cookie/token、
含凭据的 URL 或设备/客户端名称、MAC/IP、SSID。禁止增加 raw debug 响应转储。
这不是全系统日志脱敏，分享导出日志前仍需检查，排查后关闭调试开关。

实测固件只允许单一管理会话，只读监控也会占用登录。添加前退出其他 AC 管理端；
需要使用 AC 网页时，必须先在 HA **禁用条目，不是删除**，网页用完退出后再启用。
在 HA 中通过重新配置修改连接地址不必手动先禁用；重新配置/重新认证验证前仅临时卸载本条目，
验证失败、连接到错误设备或取消时自动恢复原条目。
配置验证结束释放临时会话，卸载/reload 只释放自己 token，不使用 `force=1` 或踢其他管理员。
忙碌/会话占用不等于密码错误，等待其他会话退出或超时即可重试。

管理默认关闭，重启按钮默认禁用。LED 读取 `1` 开/`2` 关/`0` 未知，写入则为 `1` 开/`0` 关；
初测发现并修正此差异。原开启状态已由读取 `1` 和 HA 开启状态核对恢复，但修正后的原生 HA off/on 周期
仍待最终验收。重启未在真机执行，不要用生产 Wi-Fi 试重启。每次 LED 写前必须确认
`led_wifi_sync` 明确关闭，开启、缺失或未知时拒绝；不修改联动选项或无线设置，不能无条件承诺 LED 不影响 Wi-Fi。
仅请求 LED 操作时，按 `entry_id` 精确过滤读取一条 AP 配置；默认只读轮询不读 `apmng_set`。
设备没有字段投影，返回行可能含 AP 管理字段，不能声称完全不读取；仅在 HA 内存中临时处理，
只提取 `led_wifi_sync` 后丢弃原始行，禁止日志、诊断、磁盘或缓存保留。
`link_status=0` 是接入中而非在线，仅 `1` 计为在线 AP。SSID 修改首版不提供。
写超时不要盲目重试；恢复 HA 备份也不能撤销已下发到控制器的改变。发生泄漏先撤销/轮换凭据，
再处理历史与缓存，单纯删除当前文件并不能补救。
