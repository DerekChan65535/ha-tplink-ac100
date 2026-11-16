# Security policy

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

## Client data

Normal polling reads the client association list and `host_management.host_info`, even
with management off and all client trackers disabled. These tables contain client names,
MAC/IP addresses, SSIDs and traffic rates; the host table can also include departed hosts
and per-client limits. Only current clients receive speed values; raw host rows and their
other fields are discarded in memory. Device names and MACs remain in HA's device registry,
and enabled trackers expose client details as state attributes, which HA may record or
export. Controller/AP speed sensors expose sums of the clients' reported rates. Logs and
downloadable diagnostics do not include these client records. Review HA's recorder and
external-service settings before enabling or sharing client entities.

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
on state is now handled. After the fix, one AP was switched off and on through the native
HA switch, with readback, and its original state was restored. Other AP models and
firmware are untested. AP/controller reboot has not been tested on live hardware. The
other controls (AP radio, SSID switches, controller settings and name, Save configuration)
were live-tested once each on one controller with restore; see README.md for the scope.

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
row. It must never enter logs, diagnostics, disk storage, or retained caches. Read-only
polling never queries the AP table `apmng_set.ap_list`. (The global AP-management
settings section `apmng_set.settings` is read only while a settings entity is enabled,
and only allow-listed fields are kept.) A missing/ambiguous safety result must block the
LED write, not trigger a change to the linkage setting.

The optional **Monitor wireless networks** setting reads the wireless service table
`apmng_wserv.wlan_serv`, which always includes Wi-Fi and RADIUS keys. Only an allow-list of
non-secret fields leaves the API layer; the keys are never stored, logged, shown or included
in diagnostics. SSID writes send only the changed field and never echo a key. When the
option is off, the table is not requested.

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
