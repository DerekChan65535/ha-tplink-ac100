# Security policy / 安全策略

## Supported versions

Security fixes target the latest published stable release. Older releases are not
promised security backports. Before the first stable release, the code is experimental
and no release has a supported-production status.

Home Assistant **2026.11.1+** is required. Read-only hardware checks cover TL-AC100
hardware **5.0**, firmware **1.0.5 Build 250814 Rel.82234n**; other hardware/firmware is unverified.
An HA or firmware update may change this boundary. Consult the release notes.

## Report a vulnerability privately

Use the repository's **Security → Report a vulnerability** page when private reporting
is available:

https://github.com/DerekChan65535/ha-tplink-ac100/security/advisories/new

The maintainer must enable GitHub private vulnerability reporting for that route to
accept reports. If the option is not available, open a public issue containing **only**
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

## Management operations

Monitoring is the default. Management is **off by default**, and reboot button entities
are **disabled by default**. LED/AP reboot/controller reboot interface evidence and
mock tests do not establish that a write is safe on any firmware. Live-write validation
has not been performed for these operations. SSID changes are not included initially.

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
漏洞请优先使用上面的 GitHub 私密报告入口；维护者尚未启用入口时，公开 Issue **只能请求私密联络方式**，
不要写漏洞细节、凭据或利用步骤。本志愿项目不承诺固定响应时限。

集成在 HA 进程内运行；HACS 收录和 CI 通过都不是安全审计。控制器应在受信管理网内，
不要开放公网管理端口。HTTP 接口本身不加密，HA 配置项与备份可能保存密码，需控制读取权限。
日志、诊断、抓包和 Issue 都不得含密码、cookie、会话密钥或真实客户端记录。

管理默认关闭，重启按钮默认禁用。管理接口证据及模拟测试不等于真机写入验证；
不要用生产 Wi-Fi 试重启。SSID 修改首版不提供。写超时不要盲目重试；
恢复 HA 备份也不能撤销已下发到控制器的改变。发生泄漏先撤销/轮换凭据，
再处理历史与缓存，单纯删除当前文件并不能补救。
