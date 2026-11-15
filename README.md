# TP-Link AC100 for Home Assistant

A community custom integration for **TP-LINK TL-AC100** controllers. It talks to the
controller's local management interface and exposes native Home Assistant devices,
entities, and actions. No embedded web panel, cloud account, MQTT bridge, or separate
service is required.

[Releases](https://github.com/DerekChan65535/ha-tplink-ac100/releases) · [Report an issue](https://github.com/DerekChan65535/ha-tplink-ac100/issues) · [Security](SECURITY.md)

> **Early release.** This is not an official TP-LINK or Home Assistant integration.
> Polling and the optional controls have been checked on one controller with the
> hardware/firmware listed below; every control except reboot was changed, read back and
> restored there. Reboot actions have **not** been tested on live hardware.
> Do not test reboot actions on a production Wi-Fi network.

## Scope and compatibility

| Item | Status |
| --- | --- |
| Home Assistant | Requires **2026.11.1 or newer**; development/test target: 2026.11.1. Older versions are not supported. |
| Controller | Live-checked: **TL-AC100 hardware 5.0**, firmware **1.0.5 Build 250814 Rel.82234n** (read-only polling in native HA, one AP LED cycle). Other versions are unverified. |
| Monitoring | Controller, AP, radio, port, interface, firmware and client state, with the clients' Wi-Fi speeds; controller and radio settings as disabled-by-default readings. |
| Wireless networks (SSIDs) | Optional device per SSID; off by default because the controller returns Wi-Fi keys with the SSID list (they are dropped in memory). |
| Controls | Optional, off by default: AP LED, AP radio, SSID enable/broadcast/isolation/IGMP, band steering, load balancing, broadcast storm suppression, multicast, controller name, Save configuration. Live-tested with restore on the hardware above; see [Controls](#controls). |
| AP/controller reboot | Optional, disruptive control; **not tested on live hardware**. |
| Not included | Firmware installation, factory reset, configuration/certificate files, accounts, portals and authentication lists, network interfaces, DHCP and routing changes, wireless network mode. See [Not included](#not-included). |
| Debug level logging | Native diagnostic switch; off by default, independent of device management. |
| Default polling | Core telemetry **60 seconds**; status **5 minutes**; settings **15 minutes**, only while a settings entity is enabled. |

The minimum HA version is intentional: this integration uses current HA APIs, without
compatibility shims for older releases. A newer HA release or controller firmware may
still introduce changes; consult the release notes before upgrading.

**Management is off by default.** Enable the integration's management option only if
you accept the risk. Reboot button entities are also disabled by default; turning on
the option does not make it safe to invoke them. Do not use write operations as a
health check. Software tests and interface evidence do not establish hardware safety.

Client names, addresses, and presence can be sensitive. Review which entities you
enable and expose to dashboards, history, automations, or external services.

## What it shows

Native readings, not an embedded controller web panel:

- Controller: CPU usage (the average across cores, as the web UI shows it; per-core sensors
  are disabled by default on new installations), memory, last restart, AP and client counts,
  unsaved-configuration and AP alarm flags, physical-port links and speeds, interface traffic
  rates and byte counters, and firmware availability.
- APs: connectivity, IP address, last restart, clients, firmware availability, and per radio
  the channel, channel load and clients.
- Wi-Fi upload and download speed in KB/s, for the controller and for each AP. The controller
  reports a speed for each client, not for each AP, so these are sums of the client speeds (the
  values on its Wireless Terminal Management page). The controller sensors add up all clients.
  An AP sensor adds up the clients that report the AP's name, because the controller names the
  AP of a client and nothing else. It is unavailable while another AP has the same name, so give
  each AP a unique name in the controller. A client without a reported speed adds nothing, and a
  sensor is unknown when none of its clients reports one. Client trackers show their own speeds
  as attributes.
- Settings, disabled by default: 70 controller settings (roaming, band steering, load
  balancing, radio optimization, multicast, time, session limits and AP management) and 21
  configured values per AP radio (mode, channel width, channel, power level 1-10, client
  limit, beacon/DTIM/RTS/fragmentation values, WMM, weak-signal rules and others).
- Firmware uses the native Update platform. **Installation is not supported**: firmware
  installation can interrupt service and recovery has not been verified.
- The original **Uptime (seconds)** entities keep their unique IDs, numeric seconds and
  statistics. They are disabled by default only on new installations.

Polling shares the existing single management session. The core poll remains at
60 seconds; it also reads the controller's host table for the client speeds (one more
request for every 10 hosts it lists), and if that table cannot be read only the speeds
become unknown. Status, port, interface and firmware reads run every 5 minutes. Settings
are read every 15 minutes, and **only while at least one settings entity is enabled**.
The slower reads are staggered, with no burst to catch up after downtime. A failed
slower read makes its readings unavailable; old values are not presented as current.
Downloadable diagnostics include source names and counts, not values or network identities.

The controller cannot return single fields, so some requested sections contain more
than is shown: the firmware announcement includes a download URL and release notes,
the cloud status includes binding details, the AP-management and reporting settings
include proxy and server addresses, and the radio table includes AP names and capability
limits. Those extra fields are dropped in memory and never stored, logged or included in
diagnostics. Tables that hold secrets are **not requested**: the AP table with per-AP
credentials, mesh backhaul settings, account and portal records, certificate/configuration
files, and the wireless service table (Wi-Fi/RADIUS keys) unless the wireless network
option below is on.

### Wireless networks (optional)

Turn on **Monitor wireless networks** in the integration options to get one device per SSID
with connected clients, enabled and hidden state, client isolation, security type and default
VLAN. The controller returns the Wi-Fi and RADIUS keys together with the SSID list and cannot
leave them out. With this option on, that table is read every 5 minutes; the keys are dropped
in memory and are never stored, logged, shown or included in diagnostics. With the option off,
the table is not requested.

## Controls

**Management is off by default.** With **Enable device control** on, these controls are added.
Except for the AP LED switches they are created disabled; enable only the ones you need.

| Control | Scope | Effect |
| --- | --- | --- |
| AP LED | One AP | Indicator only, with the LED/Wi-Fi synchronization check below. |
| AP radio | One AP radio | Off disconnects the clients of that radio. |
| SSID enabled, broadcast, client isolation, IGMP snooping/querier | One SSID (needs the wireless network option) | Off disconnects that SSID's clients; hiding stops new clients from seeing it. |
| Band steering, load balancing, broadcast storm suppression, multicast | All APs | Changes how clients are steered, admitted or served multicast. |
| Controller name | Label | No effect on clients. |
| Save configuration | Controller | Stores the running configuration so it survives a controller restart. |
| Reboot | One AP or the controller | Interrupts service; **not tested on live hardware**. |

Each change is sent once and is never repeated automatically (a timeout does not prove that
a write failed). Controller settings send their web page's complete form, built from a fresh
read. SSID, radio and name changes send only the changed field. For SSID broadcast and client
isolation this differs from the web UI, which sends the whole SSID row including its key; the
one-field update was verified on the firmware listed at the top. Setting the broadcast storm
rate also switches the rate type to a custom rate, as in the web UI. The multicast settings do
not repeat the web UI's warning about APs without multicast optimization.

Changes stay in the controller's running configuration, as in the web UI, until **Save
configuration** is pressed. Saving also stores changes made outside Home Assistant, so check
the **Configuration not saved** sensor first. Nothing is saved automatically.

Live tests on the hardware listed at the top (each applied, read back, restored and compared
with the original configuration, with no other field changed): AP radio on/off; SSID enabled,
broadcast, client isolation, IGMP snooping and querier on a designated test SSID; band
steering on/off, 5 GHz threshold and maximum failures; load balancing on/off and RSSI
threshold; broadcast storm suppression on/off; multicast snooping, unknown multicast and
2.4 GHz rate; controller name; Save configuration. The other thresholds, the storm rate and
the 5 GHz multicast rate use the same tested forms but were not changed individually.
Reboot is not tested.

## Device pages

Every client has a device, connected by the client's MAC address and linked to the
controller. It is named by the client's name, or by its MAC address while the controller
reports no name. A client that is away keeps its device and name, and a new name that the
controller reports is applied the next time the integration loads. The client trackers
remain disabled by default.

The controller device is named by its model (`TL-AC100`), APs without a name use their
model plus the last six MAC digits, and APs with duplicate names get those digits as a
suffix. Existing entity IDs and user-assigned device names are not changed. AP firmware
and hardware versions are filled in when the controller reports them. The device page
links to the AC website for troubleshooting: disable the HA entry before signing in there.
An AP or client device that the controller no longer reports can be deleted from its
device page; the controller and currently reported devices cannot.

## Not included

These web UI functions are not offered, because they interrupt service, cannot be safely
undone, or would expose credentials:

- Firmware installation (controller and APs), factory reset, reboot schedules.
- Backup/restore of configuration and certificate files, log export, packet capture.
- Accounts, administrator roles, password changes, portal, 802.1X, MAC authentication,
  RADIUS/LDAP and user lists.
- Adding, deleting or editing SSIDs beyond the switches above, including names, passwords
  and security, VLAN pools and radio binding.
- Network interfaces, DHCP servers, static routes, DNS and port settings, which can cut off
  management access.
- Wireless network mode and immediate radio optimization ("tune now"): they reconfigure every
  AP radio at once.
- Mesh, cloud management and time settings, and radio settings other than on/off.

Read-only values of many of these remain available as disabled-by-default settings readings.

## AP status and LED safety

Only `link_status=1` counts as an online AP. `link_status=0` means **connecting**,
not online or standby-online, and is excluded from the online count.

LED readback and write values differ on the tested protocol:

| Operation | On | Off | Unknown |
| --- | --- | --- | --- |
| Read LED state | `1` | `2` | `0` |
| Write LED state | `1` | `0` | Not a write value |

Initial live checks exposed this difference, which is now handled. After the fix, one
AP (TL-XAP1506GC-PoE/DC, `led_wifi_sync` off) was switched off and on through the native
HA switch. HA showed off, then on; the AP stayed online and its client count did not
change. A direct controller readback confirmed the original on state (`1`). Other AP
models, firmware, and APs with LED/Wi-Fi sync on are not live-tested; the sync-on case
is covered only by simulated tests, where the write is refused.

Some APs can link their LED and Wi-Fi state. Before **every** LED write, the integration
must read `led_wifi_sync` and confirm it is explicitly **off**; on, missing, or unknown
blocks the write. The integration never changes this linkage option or wireless
settings itself. There is no unconditional promise that an LED operation cannot affect
Wi-Fi. Leave optional management off unless you understand this protection and risk.

**Data handling for the LED safety check:** only when an LED action is requested, the
integration requests one AP configuration row using an exact `entry_id` filter. The
controller does not support field projection, so that row may also contain AP management
fields. The response is processed transiently in HA memory, only `led_wifi_sync` is
extracted, and the raw row is discarded. It is never logged, included in diagnostics,
written to disk, or retained in a cache. Normal read-only polling does **not** read the AP credential table
`apmng_set.ap_list`. Credential-free global settings are separate from that table.
This is a minimal single-row read, not a claim that the controller returns no management fields.

## Controller login sessions

On the tested firmware, the controller allows **one management session at a time**.
Monitoring uses that session even when optional management controls are disabled.

- **Before adding the integration, sign out of all other controller management clients**,
  including the AC web interface.
- The HA entry keeps a session while connected. To use the **AC web interface**, first
  **disable the entry in HA**; do not delete it. When finished, sign out of the web
  interface, then re-enable the HA entry.
- To change the connection address in HA, use the entry's **Reconfigure** flow; manual
  disabling is not required. Reconfigure and reauthentication temporarily unload only
  that entry before validation. If a reconfigure fails, reaches a different controller,
  or is cancelled, the original entry is restored automatically. A failed reauthentication
  does not restore the entry, because its stored credentials were already rejected.
- A **busy/session-in-use** error does not mean the password is wrong. Wait for the
  other session to sign out or expire, then retry. Do not force another administrator out.
- A **locked account** message means the controller temporarily blocked sign-ins after too
  many failed attempts. Wait until the lock ends. The integration does not retry in a loop.

### Session timeout

The controller ends sessions after its **Web session timeout** (5-60 minutes, 6 minutes on the
tested unit). In testing, polling every 60 seconds kept one session open for longer than that
timeout, but the controller can invalidate a session at other times too (for example after a
long pause). The integration notices the rejected token, signs in again once, and repeats the
same read. This is normal and needs no action. Turn on the **Debug logging** switch to see the
short `session expired; reauthenticating once` log lines.

A management action that meets an expired session is **not retried
automatically**: the integration cannot prove that the rejected command had no effect, and an
LED command must pass its safety check again. Check the current state and run the action again.
If a renewed session is rejected as well, that update fails and the next poll tries again; it is
not reported as a wrong password.

Configuration validation releases its temporary session after checking the connection.
Entry unload/reload releases only the session token owned by that entry; the integration
never uses `force=1` or deliberately signs out another administrator.

## Install with HACS

This repository can be added as a **HACS custom repository**. That is separate from
being included in HACS's default directory; do not assume default-directory inclusion.

1. Back up Home Assistant and read the [release notes](https://github.com/DerekChan65535/ha-tplink-ac100/releases).
2. Open **HACS → ⋮ → Custom repositories**.
3. Add `https://github.com/DerekChan65535/ha-tplink-ac100`, type **Integration**.
4. Open **TP-Link AC100**, select **Download**, and choose a compatible release.
5. **Restart Home Assistant Core**. Refreshing the browser or reloading an integration
   does not replace this restart after installing or changing its Python code.
6. Open **Settings → Devices & services → Add integration → TP-Link AC100**.
7. Sign out of other AC management clients, then enter the controller's management
   address and credentials. Start with monitoring; leave optional management disabled.

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=DerekChan65535&repository=ha-tplink-ac100&category=integration)

HACS downloads code; adding the controller in Devices & services is a separate step.
No GitHub PAT or Home Assistant access token needs to be shared with this project's
maintainer. Installing/configuring HACS itself is outside this repository's scope.

## Manual installation

1. Download `tplink_ac100-<version>.zip` and `SHA256SUMS` from a specific
   [Release](https://github.com/DerekChan65535/ha-tplink-ac100/releases). Verify the
   downloaded archive with `sha256sum -c SHA256SUMS` before installing.
2. Extract it locally. Copy **only** the `tplink_ac100/` folder into your HA
   configuration directory's `custom_components/` directory. If you download GitHub's
   source archive instead, that folder is under `custom_components/` in the source.
3. Check the final path: `<HA_CONFIG>/custom_components/tplink_ac100/manifest.json`.
   Do not nest the whole repository inside `custom_components`.
4. Restart Home Assistant Core, then add **TP-Link AC100** in Devices & services.

For HA OS the configuration directory is normally `/config`. For HA Container, use
the host directory mounted at `/config`; do not write into Python `site-packages`.
Use one code-management method at a time: HACS **or** manual installation.

## Update, roll back, and remove

### Update

1. Read the target release's compatibility notes. Record your current HA/integration
   versions and create a complete HA backup; keep credential-bearing backups private.
2. In HACS, download the update or use **Redownload** to choose a specific version.
   **Update information** only refreshes metadata; it does not install code.
3. Restart Home Assistant Core. Check monitoring, entity IDs, and existing automations
   before considering any management action.

For a manual update, stop HA and replace the **whole** `tplink_ac100` directory with
the target release's directory, so removed files do not linger. Keep the old copy
outside `custom_components`, then start HA. Do not overwrite the rest of the HA config.

### Roll back

If there was no incompatible configuration migration, use **HACS → repository ⋮ →
Redownload → Need a different version?** to choose the previous working release,
then restart HA. If it is not offered, restore that release's complete directory
manually while HA is stopped.

A code downgrade does **not** undo config-entry migrations, an HA upgrade, or changes
made on the controller. In those cases restore a matching pre-upgrade HA backup and
compatible HA/integration versions. Do not hand-edit `.storage` to force a downgrade.
Recheck the release notes before retrying an update.

### Remove

Remove or adjust dependent automations first. Delete the integration's config entry
in **Devices & services**, remove the code through HACS (or manually while HA is
stopped), then restart HA. Removing a HACS repository alone does not guarantee removal
of its stored configuration/data. Never delete the whole `custom_components` directory.

## Debug level logging

On the controller's HA device page, use the native **Debug level logging** diagnostic
switch to turn this integration's debug logging on or off. No YAML logger changes are
needed. The switch is independent of the optional device-management setting and
remains usable when the controller is offline. Toggling it makes **no device API call**.

- Default: **off**. Its state is not persisted; after an HA restart it starts off.
- On: save the current levels, then set `custom_components.tplink_ac100` **and its
  child loggers** to `DEBUG`. HA's root logger and other integrations are unchanged.
- Off: restore the earlier non-`DEBUG` levels. Any logger in this namespace that would
  still be at `DEBUG` is set to `INFO`, so an explicit child-logger level cannot bypass
  the off state. Turn it off when the troubleshooting session is finished.

The log allowlist contains only fixed operation/stage names, success/failure and fixed
error categories, timing, and aggregate counts. Debug logging does **not** dump raw
requests/responses, authentication exchanges, tokens/cookies/passwords, URLs containing
credentials, or device/client names, MAC/IP addresses, and SSIDs. Review any log export
before sharing it: this switch is not a system-wide log scrubber. See [SECURITY.md](SECURITY.md).

## Network and credential safety

- Keep the controller and HA on a trusted management network. Do not expose the
  controller's management port to the Internet or add port-forwarding for this integration.
- Use a dedicated least-privilege account if the controller supports one. If only an
  administrator account works, understand that the integration holds those privileges.
- An HTTP management interface is not encrypted. Device limitations are not fixed by
  running this integration. Do not disable TLS verification as a general workaround.
- HA config entries and backups can contain credentials. Protect both. Do not upload
  raw config entries, cookies, authentication responses, packet captures, or device dumps.
- Runtime communication is local to the controller. HACS/GitHub and package installation
  may require Internet access when installing or updating software.

For vulnerability reports, use [SECURITY.md](SECURITY.md), not a public issue with
sensitive details. For ordinary bugs, include HA/integration/hardware/firmware versions,
steps to reproduce, and **redacted** logs. Never include passwords or real client data.

## Development

Requires Python **3.14.2+** and [uv](https://docs.astral.sh/uv/). From the repository root:

```console
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

Tests must use mocks/fixtures, not a real controller or a user's Home Assistant.
The development environment pins `homeassistant==2026.11.1` and the official
`pytest-homeassistant==2026.11.1` in `uv.lock`.

CI runs project checks, hassfest, and HACS validation. These checks do not prove
controller firmware compatibility or safe write behavior. Pushing a version tag runs
the checks and creates a **draft** GitHub Release; a maintainer must review and publish it.
A tag alone is not a published release. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License and attribution

[MIT](LICENSE), copyright 2026 Derek Chan and contributors. The controller protocol
client is independently implemented; third-party interface references do not grant
permission to copy unlicensed source code. See [THIRD_PARTY.md](THIRD_PARTY.md).

TP-LINK and Home Assistant names identify compatible products. This community project
is not affiliated with or endorsed by their owners. Its license does not license
TP-LINK firmware, proprietary web assets, or third-party trademarks.
