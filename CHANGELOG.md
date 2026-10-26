## 0.7.6 (2026-10-26)

* Detect a dead or stale Pulse session. pyadtpulse's `wait_for_update()` only returns on data changes or errors, so a session that was silently logged out (2026-10-14..25) left the connection-status sensor at "Connection OK" for days. The update loop now wakes every 60 s and checks the session:
  * not authenticated (`is_connected` false) for 3+ min -> status "Not Logged In"
  * keepalive or sync-check task no longer running, or no relogin for 2 x relogin interval + 1 keepalive interval (125 min with 60/5) -> new status "Connection Stale"
  * clears itself back to "Connection OK" when the session is healthy again; never overrides a more specific error reported by pyadtpulse

## 0.7.5 (2026-10-09)

* Confirm arm/disarm commands actually reach the panel: after the portal accepts a command, wait up to 120 s for Pulse to report the requested state, retry once, then fire an `adtpulse_command_failed` event (`action`, `attempts`, `last_status`) instead of silently reverting
* Confirmation/retry runs in the background so service calls return immediately and don't stall automations
* A command the portal rejects outright also fires `adtpulse_command_failed`

## 0.7.4 (2026-10-08)

* Fix arming, zone-listener and reload issues (see commit 2696c2a)

## 0.7.3 (2026-03-21)

* Fix missing `code` parameter in `async_alarm_arm_night` causing crash when arming to Night mode on HA 2025.8+

## 0.7.2 (2026-03-05)

* Bump integration version to `0.7.2`
* Bump minimum Python version to `>=3.14.3`
* Bump minimum Home Assistant version to `>=2026.3.0`
* Bump `pyadtpulse` requirement to `>=1.2.14`
* Update `.python-version` to `3.14.3`
* Update tooling targets for Python 3.14 (`ruff` target `py314`, `uv` required `>=0.10.0`)

## 0.7.1 (2025-11-16)

* Rename `alarm_disarm_func` to `arm_disarm_func`

## 0.7.0 (2025-10-24)

* Fix inverted `assumed_state()` behavior in alarm control panel
* Refactor alarm control panel state handling to use `AlarmControlPanelState`
* Improve general formatting/readability across the integration
* Fix `binary_sensor` `via_device` to reference the gateway unique ID
* Bump minimum Home Assistant version and package version to `0.7.0`

## 0.6.0 (2025-10-14)

* Bump `pyadtpulse` to `1.2.12`
* Update README ownership and project references
* Merge small README/documentation fixes

## 0.5.1 (2025-08-16)

* Apply repository pre-commit updates

## 0.5.0 (2025-08-14)

* Repo cleanup by @EVWorth in #60
* Resolve setup bugfix and added support for HA 2025.8.0 @EVWorth in #61

## 0.4.8 (2025-07-13)

* updated to the latest HACS validation and hassfest

## 0.4.7 (2025-07-12)

* bump pyadtpulse to 1.2.11 (requires HA 2025.7.1 currently)

## 0.4.6 (2024-04-21)

* add arm night
* bump pyadtpulse to 1.2.9
  This should fix some more instances where it is needed to recover from errors/disconnects
  
## 0.4.5 (2024-04-04)

* bump minimum HA version to 2024.04.0
* fix import of FlowResult

## 0.4.4 (2024-03-06)

* update connection status and next update sensors on successful data fetch
* bump pyadtpulse to 1.2.8 which should provide better error logging and more stability for failures from Pulse site

## 0.4.3 (2024-03-02)

* fix not updating when exception is raised
* change glass break type to sound binary sensor

## 0.4.2 (2024-02-25)

* use default Home Assistant icons where possible
* remove gateway serial number/site id from gateway name

## 0.4.1 (2024-02-24)

* bump pyadtpulse to 1.2.7.
  This will:
  * perform a full re-login approximately every 6 hours
  * speed up zone and alarm update times
  * allow Home Assistant to just refresh updated zones/alarm instead all all entities
  * fix various "you have not logged in yet" errors
* have connection status show authentication errors
* correctly handle coordinator update authentication errors to perform configuration reauthentication flow

## 0.4.0 (2024-02-02)

* bump pyadtpulse to 1.2.0.  This should provide more robust error handling and stability
* add connection status and next update sensors
* remove quick re-login service

## 0.3.5 (2023-12-22)

* bump pyadtpulse to 1.1.5 to fix more changes in Pulse v27

## 0.3.4 (2023-12-13)

* bump pyadtpulse to 1.1.4 to fix changes in Pulse V27

## 0.3.3 (2023-10-12)

* bump pyadtpulse to 1.1.3.  This should fix alarm not updating issue
* add force stay and force away services
* add relogin service
* refactor code to use base entity.  This should cause most entities to become unavailable if the gateway goes offline
* disallow invalid alarm state changes
* revert alarm card functionality.  All states will be available, but exceptions will be thrown if an invalid state is requested.

## 0.3.2 (2023-10-08)

Alarm control panel updates:
* update alarm capabilities based upon existing state of alarm
* disable setting alarm to existing state
* add arming/disarming icons
* add assumed state
* remove site id from attributes
* raise HomeAssistant exception on alarm set failure
* write ha state even if alarm action fails

## 0.3.1 (2023-10-07)

* fix typo in manifest preventing install

## 0.3.0 (2023-10-07)

* bump pyadtpulse to 1.1.2
* add options flow for poll interval, relogin interval, keepalive interval
* add reauth config flow
* change code owner to rlippmann
* add gateway and alarm devices
