# Changelog

## [0.1.0] - 2026-11-15

### Initial scope

- Local, native Home Assistant integration for TP-LINK TL-AC100 controllers.
- Controller/AP/client monitoring, with a default 60-second polling interval.
- Optional AP LED and reboot controls. Management is off by default; reboot button
  entities are disabled by default. SSID changes are not included.

### Compatibility and limitations

- Minimum Home Assistant: **2026.11.1**. Older HA releases are not supported.
- Read-only hardware checks: TL-AC100 hardware **5.0**, firmware **1.0.5 Build 250814 Rel.82234n**.
  Other combinations are unverified.
- LED/AP reboot/controller reboot operations are based on interface evidence and
  software tests, **not live-write validation**. Do not test reboots on production Wi-Fi.
- This is an early community release, not an official TP-LINK or HA integration.

### Installation and rollback

- Install with HACS as a custom repository, or copy the complete integration directory
  from this release's source archive. Restart Home Assistant Core after installation
  or any code update, then configure it through Devices & services.
- Back up HA before updating. A code rollback does not reverse a config-entry migration
  or controller-side action; restore a matching backup when needed. See README.md.
