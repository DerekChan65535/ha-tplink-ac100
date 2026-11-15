# Changelog

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
- Count only AP `link_status=1` as online; `0` means connecting (接入中), not standby-online.
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
