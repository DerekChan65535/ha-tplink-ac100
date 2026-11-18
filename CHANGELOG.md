# Changelog

## [0.2.3b2] - 2026-11-18

**Beta release.** This update changes Home Assistant polling, not controller configuration.
The default is aligned with the WebUI's common table timer; it is not a verified device rate limit.
Terminal hardware acceptance remains incomplete; this update adds no terminal hardware experiment.

### Added

- A native **Core refresh interval (seconds)** integration option, from **10 to 3600 seconds**, with
  **10 seconds** as the default. It applies on options reload. Existing entries without a saved
  interval use the new default, keeping their control/SSID opt-ins and device/entity identities.
- Simplified Chinese text for the integration options screen; other screens retain their existing
  translations and fallback behavior.

### Changed

- The shared core polling default changes from 60 to 10 seconds, matching the WebUI's common AP/client
  table timer, not its 3-second CPU/memory or 5-second top-bar timers. This shorter interval increases
  API load. The device's safe sustained whole-controller rate is unknown; the new default has not
  been load-tested on physical hardware.
- Status and enabled-settings reads keep their nominal 5/15-minute due periods, checked on core
  polls. Long primary intervals can delay them. Normal polls still run only one slower read and
  never start a catch-up burst. Reads after explicit management actions retain their refresh behavior.

### Fixed

- Slow primary polling intervals can no longer indefinitely favor status reads over enabled settings:
  when both are due, the older deadline is served first. No per-entity polling, parallel timer, cache,
  write retry, automatic Save, identity migration or user-settings reset is introduced.
- Current documentation distinguishes incomplete terminal hardware acceptance from no hardware
  operation ever having been attempted. Earlier release notes remain historical records.

## [0.2.3b1] - 2026-11-16

**Beta release.** New terminal controls are disabled by default and have not been exercised on
real hardware. Enable them only deliberately on a suitable test terminal. This release does not
claim firmware validation of disconnect, clear, binding or allowed-time enforcement.

### Added

- Each client's existing MAC-identified device now shows the controller's saved name, rate-limit
  state and caps, AP binding and forced-binding state, and allowed-time mode and schedule. These
  diagnostic readings are enabled by default and remain readable for a retained offline record;
  they never stand in for current presence, AP, VLAN or traffic. Unknown data stays unknown.
- Native terminal controls are listed on every client page, disabled by default and unavailable
  without management opt-in and the required safe source: controller-side name, rate limiting,
  upload/download caps, AP binding/target/forced binding, allowed-time mode with manual weekdays
  and windows or a weekly hourly calendar, disconnect, disconnect and clear, and offline clear.
  They follow the terminal pages, but **have not been tested on live hardware**. A disconnect is
  not a permanent block; clearing the saved record is not deleting an HA device.
- SSID encoding (`UTF-8`/`GBK`) and radio unit, for example `2 (5GHz)`, are enabled connection
  readings. Vendor and operating system category `others` is now shown as **Others**, matching
  the controller; a missing value or `---` remains unknown.
- Fake-controller protocol and native HA tests cover saved and offline records, unknown fields,
  stable identities, disabled defaults and opt-in, field validation, binding targets, allowed-time
  conversion, partial failures, no write replay, no optimistic state and no automatic save.

### Changed

- The shared core poll keeps allow-listed saved terminal configuration and reads credential-free
  radio bindings and linked time objects within fixed limits. Entities do not poll independently.
  A retained record can create an offline client page without marking it present or linking it
  to an AP. A saved controller name is preferred over an old registry name when no current name
  exists; user-assigned HA names, existing device/entity identities and user settings are kept.
- HA device removal is refused while the controller still retains a saved terminal record.
  Controller-side clear actions and HA device removal remain separate.
- Terminal actions recheck fresh state and identity, preserve unrelated form fields and refuse
  unsafe or ambiguous records/targets/schedules. There is no automatic write replay, optimistic
  update, save or rollback. A partial operation is reported explicitly; failures are reread on
  the next scheduled update rather than causing a login/read burst.

## [0.2.2] - 2026-11-16

### Added

- Each client's device page shows read-only readings of its own. They are **enabled by
  default**, so the page no longer depends on the optional client tracker: connectivity (off
  while the client is away), signal strength (dBm, the strongest radio), link rate (Mbit/s, the
  highest negotiated rate, not traffic), Wi-Fi upload and download speed (kB/s), IPv4 and IPv6
  address, SSID, access point name, Wi-Fi band, VLAN ID, vendor, operating system and
  connection time. They come from the client list that the 60-second poll already reads, with
  no extra request.
- A value that the controller does not report stays unknown. Placeholder addresses (`---`,
  `0.0.0.0` and `::`) and a VLAN of 0 (which the web UI shows as `---`) are not shown as values.
  The connection time is the controller's local date and time as text, with no time zone. The signal strength and link
  rate sensors carry per-radio records as an attribute that the recorder does not store.

### Changed

- Devices have three levels: TL-AC100, its APs, and the clients of each AP. A client is linked
  to the AP that the controller names for it, but only when exactly one AP, an offline one
  included, has that exact name. A client whose AP name is shared, unknown or not listed, and a
  client that is away, has no parent device. No client is attached to the controller any more.
  The links are corrected when the integration loads after the upgrade, and they follow a
  client that roams. Device IDs, entity IDs, and names, areas and labels that you set are kept.
- A name reported by the controller updates the client's default device name. An unnamed or
  absent client keeps its earlier known name; the MAC address is used only when no name is known.

### Fixed

- A client's device page showed only its MAC address, because the details were in the optional
  client tracker, which is disabled by default. The tracker stays optional and unchanged.
- The controller's **Connected devices** listed every client. Each client is listed under its
  AP now, and the controller lists its APs.

## [0.2.1] - 2026-11-15

### Fixed

- Every client has a device again, as before 0.2.0: connected by the client's MAC address,
  named by the client (or by its MAC address) and linked to the controller. Version 0.2.0
  removed the client devices that were not in use, which made the device information of a
  connected client hard to find. They are created again when the integration loads, normally
  with their earlier device IDs. A client that is away at that moment gets its MAC address as
  the device name until the integration loads while it is connected. Client trackers keep
  their entity IDs and unique IDs and stay disabled by default.

### Added

- Wi-Fi upload speed and Wi-Fi download speed sensors (KB/s) for the controller, which add up
  all clients, and for each AP, which add up the clients that report the AP's name. An AP's
  sensors are unavailable while any other AP, even an offline one, has the same name, because
  the controller tells only the name of a client's AP; rename one of them in the controller.
  The speeds are the ones that the controller reports for each client in its Wireless Terminal
  Management table, read with the 60-second poll. Rejected, malformed or oversized tables make
  only the speeds unknown; connection and session errors still fail the poll.
- Client trackers show `upload_kilobytes_per_second` and `download_kilobytes_per_second`
  attributes while the controller reports them.

## [0.2.0] - 2026-11-15

### Added

- Controller CPU usage averaged across cores (the web UI value); per-core sensors start
  disabled on new installations.
- Unreleased read-only expansion: AP IP and per-radio readings, native last-restart
  values, controller configuration/alarm flags, physical-port and interface telemetry,
  firmware availability without installation, and selected safe scalar settings.
- Separate 5-minute monitoring and 15-minute settings reads, staggered behind the
  existing 60-second core poll and sharing the same controller session. Settings are
  read only while a settings entity is enabled.
- A source the account may not read makes only its entities unavailable; it does not
  start a password reauthentication.
- Source-specific availability: failed optional reads do not expose stale values as
  healthy data. Diagnostics report aggregate coverage, not raw setting values.
- Read-only per-radio AP settings as diagnostic entities on each AP, disabled by default:
  radio enabled, radio mode, channel width, configured channel (Auto or a number), dynamic
  channel switching, transmit power level (1-10, not dBm), client limit, beacon interval,
  DTIM period, RTS and fragmentation thresholds, short guard interval, WMM, airtime
  scheduling, weak-signal restriction and disconnect rules with their dBm thresholds, client
  forward access and broadcast probe response. The controller's radio table is read with the
  15-minute settings read, so these entities also start that read when enabled. A setting
  that does not apply, such as a weak-signal threshold while its rule is off, is unknown.

- Optional wireless network (SSID) devices, off by default: connected clients, enabled and
  hidden state, client isolation, security type and default VLAN. The SSID table also holds
  Wi-Fi/RADIUS keys; they are dropped in memory and never stored, logged or shown.
- Management controls (management option, entities disabled by default): AP radio on/off,
  SSID enabled/broadcast/client isolation/IGMP snooping/IGMP querier, band steering and load
  balancing with their thresholds, broadcast storm suppression and rate, multicast settings,
  controller name and Save configuration. Each write is sent once and never replayed;
  settings use the web page's complete form from a fresh read, SSID/radio/name changes send
  only the changed field. Live-tested with restore on TL-AC100 hardware 5.0 (see README).
- Management actions rejected by the controller no longer start a password reauthentication.
  After an action with an unknown outcome, the next update reads all optional data again.
- SSID names with emoji or special spaces, and controller names with ideographic spaces,
  are shown instead of being treated as invalid.

### Changed

- Keep legacy numeric uptime entities and statistics intact; add separate native uptime
  timestamps. Legacy duration entities are disabled by default only for new installations.
- The controller device is named `TL-AC100` instead of `TP-Link TL-AC100`; existing entity
  IDs are unchanged. AP devices get fallback and duplicate-safe names, a link to the
  controller website, and firmware/hardware versions when reported.
- Client trackers no longer create MAC-only devices. Unused tracker-only client devices
  (tracker never enabled, not customised, not shared) are removed on upgrade; the tracker
  entities themselves are kept.
- Stale AP and client devices can be deleted from the device page.

### Fixed

- A management command (LED, reboot) that is rejected because its session expired is no longer
  replayed automatically. Reads still renew the session once and repeat.
- A session that is rejected again right after a successful sign-in no longer starts a password
  reauthentication; the next update retries.
- A failed reauthentication no longer retries the stored credentials that were already rejected.
- A temporarily locked account now has its own message instead of "invalid authentication".
- Calling the sign-in method while a session token is held no longer discards that token.

### Removed

- The zh-Hans translation and the Chinese README. The Home Assistant UI and the codebase are
  English only.

## [0.1.0] - 2026-11-15

### Initial scope

- Local, native Home Assistant integration for TP-LINK TL-AC100 controllers.
- Controller/AP/client monitoring, with a default 60-second polling interval.
- Native **Debug level logging** diagnostic switch, off by default and independent of
  device management. It saves levels and sets this integration's namespace and child
  loggers to `DEBUG`, without changing HA's root logger or other integrations. Off
  restores prior non-`DEBUG` levels and sets any remaining in-namespace `DEBUG` logger
  to `INFO`, so explicit child levels cannot bypass it. It starts off after an HA restart.
  It remains usable offline and makes no device API call. Logs use a metadata-only
  allowlist; raw request/response or authentication dumps are not supported.
- Optional AP LED and reboot controls. Management is off by default; reboot button
  entities are disabled by default. SSID changes are not included.
- Correct LED protocol mapping: read `1` on / `2` off / `0` unknown; write `1` on / `0` off.
  Each LED write requires a fresh confirmation that `led_wifi_sync` is explicitly off;
  on or unknown is rejected. The integration does not change linkage or wireless settings.
- Count only AP `link_status=1` as online; `0` means connecting, not standby-online.
- Session-safe setup and cleanup: configuration validation releases its temporary
  login, and entry unload/reload releases only its own token. No `force=1` login or
  intentional eviction of other administrators.
- GitHub private vulnerability reporting is enabled for this repository.

### Compatibility and limitations

- Minimum Home Assistant: **2026.11.1**. Older HA releases are not supported.
- Read-only hardware checks: TL-AC100 hardware **5.0**, firmware **1.0.5 Build 250814 Rel.82234n**.
  Other combinations are unverified.
- Tested firmware permits one management session at a time, including read-only HA
  polling. Sign out of other AC management clients before adding HA. To use the AC
  web interface, disable (do not delete) the HA entry first; sign out of the web
  interface when finished, then re-enable HA. Changing the address via HA Reconfigure
  does not require manual disabling: reconfigure/reauth unloads only its own entry
  before validation, then restores it on failure, wrong-device detection, or cancellation.
  A busy/session-in-use response is not a wrong-password response; wait for the other
  session to exit or expire.
- Initial live LED checks exposed the asymmetric protocol values. After the fix, one AP
  (TL-XAP1506GC-PoE/DC, `led_wifi_sync` off) was switched off and on through the native HA
  switch on the checked controller. HA showed off, then on; the AP stayed online with an
  unchanged client count, and a direct readback confirmed the original on state. Other AP
  models and firmware are untested; the sync-on refusal path is covered by simulated tests.
- Some APs link LED state with Wi-Fi; LED control has no unconditional non-disruption
  guarantee. The explicit-sync-off precondition is required before every write.
- AP/controller reboot has **not** been tested on live hardware. Do not test reboots
  on production Wi-Fi.
- This is an early community release, not an official TP-LINK or HA integration.

### Installation and rollback

- Install with HACS as a custom repository, or copy the complete integration directory
  from this release's source archive. Restart Home Assistant Core after installation
  or any code update, then configure it through Devices & services.
- Back up HA before updating. A code rollback does not reverse a config-entry migration
  or controller-side action; restore a matching backup when needed. See README.md.
