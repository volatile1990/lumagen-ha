## v0.1.3 - Serial recovery improvements

> **Important:** This version requires Lumagen firmware **030326 or later**.
>
> This version does **not** use Lumagen command delimiters. Configure the Lumagen Radiance Pro with:
>
> ```text
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Echo -> On
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Delimiters -> Off
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Report mode changes -> Fullv5
> ```

### Added

- Added periodic recovery polling so entities can recover automatically after the Lumagen loses and regains power.

### Changed

- Refresh input labels during recovery before marking the Lumagen entities available again.
- Route button and remote commands through the coordinator so command paths share reconnect handling.

### Fixed

- Reset stale serial transports after communication failures so later polls and commands reconnect cleanly.
- Raise Home Assistant communication errors for failed commands instead of surfacing raw serial exceptions.

## v0.1.2 - Update pylumagen dependency

> **Important:** This version requires Lumagen firmware **030326 or later**.
>
> This version does **not** use Lumagen command delimiters. Configure the Lumagen Radiance Pro with:
>
> ```text
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Echo -> On
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Delimiters -> Off
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Report mode changes -> Fullv5
> ```

### Changed

- Updated the required `pylumagen` package to `0.1.7`.

## v0.1.1 - OSD message service improvements

> **Important:** This version requires Lumagen firmware **030326 or later**.
>
> This version does **not** use Lumagen command delimiters. Configure the Lumagen Radiance Pro with:
>
> ```text
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Echo -> On
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Delimiters -> Off
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Report mode changes -> Fullv5
> ```

### Added

- Added explicit two-line support to the OSD message action with separate Line 1 and Line 2 fields.
- Added message placement control for normal messages, allowing auto-wrap, line 1, or line 2 placement.
- Added optional line centering support for line 1, line 2, or both lines.
- Added block-character substitution support for volume-bar style OSD messages.
- Added multi-device routing for Lumagen actions using an optional `entity_id`.


## v0.1.0 - Initial release

> **Important:** This version requires Lumagen firmware **030326 or later**.
>
> This version does **not** use Lumagen command delimiters. Configure the Lumagen Radiance Pro with:
>
> ```text
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Echo -> On
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Delimiters -> Off
> MENU -> Other -> I/O Setup -> RS-232 Setup -> Report mode changes -> Fullv5
> ```

### Added

- Initial Lumagen Radiance Pro Home Assistant integration.
- UI-based configuration flow.
- TCP serial and USB serial connection support.
- Media player entity with power and source selection.
- Remote entity with Lumagen command support.
- Button entities for common controls.
- Switch entities for supported toggle controls.
- Sensor entities for Lumagen status and diagnostics.
- Home Assistant actions for input labels and OSD messages.
- HACS custom repository support.
