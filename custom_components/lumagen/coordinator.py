"""Data update coordinator for the Lumagen integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from lumagen_control import (
    LumagenDevice,
    LumagenProtocol,
    SerialTransport,
    TcpTransport,
)
from lumagen_control.formatters import (
    OsdMessageOptions,
    format_3d_mode,
    format_auto_aspect_status,
    format_dynamic_range,
    format_input_status,
    format_nls_active,
    format_output_colorspace,
    format_output_enabled_mask,
    format_scan_mode,
    format_subtitle_shift_status,
    format_vertical_rate,
    strip_leading_zeroes,
)
from lumagen_control.models import (
    LumagenFullStatus,
    LumagenOutputInfo,
    LumagenPowerStatus,
)

from .const import (
    CONF_BAUDRATE,
    CONF_CONNECTION_TYPE,
    CONF_SERIAL_DEVICE,
    CONF_SW_VERSION,
    CONNECTION_TYPE_SERIAL,
    CONNECTION_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

POWER_OFF_IGNORE_AFTER_POWER_ON_SECONDS = 5.0
OUTPUT_REFRESH_DELAY_SECONDS = 3.0
OUTPUT_REFRESH_RETRY_DELAY_SECONDS = 30.0
RECOVERY_POLL_INTERVAL = timedelta(seconds=30)
LUMAGEN_TIMEOUT_ERRORS = (TimeoutError, asyncio.TimeoutError)

StatusValue = str | int | float | bool | None


class LumagenCommunicationError(HomeAssistantError):
    """Raised when communication with the Lumagen fails."""


@dataclass(frozen=True)
class LumagenCoordinatorData:  # pylint: disable=too-many-instance-attributes
    """Runtime data from the Lumagen."""

    power_on: bool
    available: bool = True
    input_number: int | None = None
    input_memory: str | None = None
    input_labels: dict[int, str] | None = None
    status: dict[str, StatusValue] | None = None
    sw_version: str | None = None


@dataclass(frozen=True)
class LumagenOsdMessage:  # pylint: disable=too-many-instance-attributes
    """Lumagen OSD message request."""

    message: str | None
    duration: int
    message_placement: str = "auto"
    block_char: str | None = None
    line1: str | None = None
    line2: str | None = None
    center_line1: bool = False
    center_line2: bool = False


# pylint: disable=too-many-instance-attributes
class LumagenDataUpdateCoordinator(DataUpdateCoordinator[LumagenCoordinatorData]):
    """Coordinator for Lumagen state."""

    config_entry: ConfigEntry
    device: LumagenDevice

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device: LumagenDevice,
    ) -> None:
        """Initialize the coordinator."""
        self.device = device
        self._lumagen_lock = asyncio.Lock()
        self._ignore_power_off_until = 0.0
        self._horizontal_refresh_task: asyncio.Task[None] | None = None
        self._horizontal_refresh_retry_after = 0.0
        self._input_labels_task: asyncio.Task[None] | None = None
        self._firmware_refresh_pending = True
        self._connected = False

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=RECOVERY_POLL_INTERVAL,
            always_update=False,
        )

    async def _async_setup(self) -> None:
        """Set up the Lumagen coordinator."""
        self.device.set_event_callback(self.async_handle_event)

    async def _async_update_data(self) -> LumagenCoordinatorData:
        """Fetch startup data from the Lumagen."""
        was_unavailable = self.data is not None and not self.data.available

        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()

                power_status = await self.device.query_power()
                power_on = _power_on(power_status, default=False)
                input_info = await self.device.query_input()
                full_status = await self.device.query_full_status()

            status = self._parse_full_status(full_status)

        except LUMAGEN_TIMEOUT_ERRORS as err:
            return await self._async_handle_update_failure(
                "Timed out querying Lumagen state",
                err,
            )
        except (OSError, RuntimeError) as err:
            return await self._async_handle_update_failure(
                f"Error querying Lumagen state; marking unavailable: {err}",
                err,
            )
        except ValueError as err:
            if self.data is not None:
                _LOGGER.debug(
                    "Invalid Lumagen response; keeping previous state: %s", err
                )
                return self.data

            raise UpdateFailed(f"Invalid Lumagen response: {err}") from err

        if not power_on and self._should_ignore_power_off() and self.data is not None:
            _LOGGER.debug("Ignoring stale Lumagen power-off query result")
            return self.data

        if power_on:
            self._ignore_power_off_until = 0.0

        if status is not None:
            await self._add_horizontal_status(status)

        input_labels = self._current_input_labels()

        if input_labels is None or was_unavailable:
            try:
                input_labels = await self._async_query_input_labels()
            except LUMAGEN_TIMEOUT_ERRORS as err:
                return await self._async_handle_update_failure(
                    "Timed out loading Lumagen input labels during recovery",
                    err,
                )
            except (OSError, RuntimeError) as err:
                return await self._async_handle_update_failure(
                    f"Failed to load Lumagen input labels during recovery: {err}",
                    err,
                )
            except ValueError as err:
                _LOGGER.debug(
                    "Invalid Lumagen input label response; keeping cached labels: %s",
                    err,
                )
                input_labels = self._current_input_labels()

                if input_labels is None:
                    return await self._async_handle_update_failure(
                        "Invalid Lumagen input label response during recovery",
                        err,
                    )

        if input_labels is None:
            self._schedule_input_labels_load()

        sw_version = await self._async_refresh_firmware_version()

        return LumagenCoordinatorData(
            power_on=power_on,
            available=True,
            input_number=_select_input_number(
                status,
                input_info.input_number if input_info else None,
            ),
            input_memory=_select_input_memory(
                status,
                input_info.memory if input_info else None,
            ),
            input_labels=input_labels,
            status=status,
            sw_version=sw_version,
        )

    async def async_handle_event(self, event: str) -> None:
        """Handle an unsolicited Lumagen event."""
        _LOGGER.debug("Lumagen unsolicited event: %s", event)

        if event.startswith("!S02,"):
            was_unavailable = self.data is not None and not self.data.available
            power_status = self.device.parse_power_event(event)
            power_on = _power_on(
                power_status,
                default=self.data.power_on if self.data else False,
            )

            if not power_on and self._should_ignore_power_off():
                _LOGGER.debug("Ignoring stale Lumagen power-off event: %s", event)
                return

            if power_on:
                self._ignore_power_off_until = 0.0

            if power_on and was_unavailable:
                self.hass.async_create_task(
                    self.async_request_refresh(),
                    "lumagen_refresh_after_power_event",
                )
                return

            self.async_set_updated_data(
                LumagenCoordinatorData(
                    power_on=power_on,
                    available=True,
                    input_number=self._current_input_number(),
                    input_memory=self._current_input_memory(),
                    input_labels=self._current_input_labels(),
                    status=self._current_status(),
                    sw_version=self._current_sw_version(),
                )
            )

            if self._current_input_labels() is None:
                self._schedule_input_labels_load()

            return

        if event.startswith("!I01,"):
            status = dict(self._current_status() or {})
            status.update(_parse_input_info(event))
            self._set_status(status)
            return

        if event.startswith("!O01,"):
            status = dict(self._current_status() or {})
            status.update(
                _parse_output_info(self.device.parse_output_info_event(event))
            )
            self._set_status(status)
            return

        if event.startswith(("!I21,", "!I22,", "!I23,", "!I24,", "!I25,")):
            full_status = self.device.parse_full_status_event(event)
            status = self._parse_full_status(full_status)

            if status is not None and self.data is not None:
                self.async_set_updated_data(
                    LumagenCoordinatorData(
                        power_on=self.data.power_on,
                        available=True,
                        input_number=_select_input_number(
                            status,
                            self._current_input_number(),
                        ),
                        input_memory=_select_input_memory(
                            status,
                            self._current_input_memory(),
                        ),
                        input_labels=self._current_input_labels(),
                        status=status,
                        sw_version=self._current_sw_version(),
                    )
                )

                if full_status is not None and full_status.input_status_code == "1":
                    self._schedule_horizontal_status_refresh()

                if self._current_input_labels() is None:
                    self._schedule_input_labels_load()

            return

    async def async_power_on(self) -> None:
        """Turn the Lumagen on and ignore stale standby events briefly."""
        self._ignore_power_off_until = (
            monotonic() + POWER_OFF_IGNORE_AFTER_POWER_ON_SECONDS
        )

        await self._async_run_device_command("turning Lumagen on", self.device.power_on)

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=True,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=self._current_input_labels(),
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_standby(self) -> None:
        """Put the Lumagen in standby."""
        self._ignore_power_off_until = 0.0

        await self._async_run_device_command(
            "putting Lumagen in standby",
            self.device.standby,
        )

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=False,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=self._current_input_labels(),
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_select_input(self, input_number: int) -> None:
        """Select a Lumagen input."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                await self.device.select_input(input_number)
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                f"Timed out selecting Lumagen input {input_number}; "
                "marking unavailable",
                err,
            )
            raise LumagenCommunicationError(
                f"Lumagen is unavailable while selecting input {input_number}"
            ) from err
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Error selecting Lumagen input {input_number}; "
                f"marking unavailable: {err}",
                err,
            )
            raise LumagenCommunicationError(
                f"Lumagen is unavailable while selecting input {input_number}"
            ) from err

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=True,
                available=True,
                input_number=input_number,
                input_memory=self._current_input_memory(),
                input_labels=self._current_input_labels(),
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_load_input_labels(self) -> None:
        """Load Lumagen input labels in the background."""
        try:
            input_labels = await self._async_query_input_labels()
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                "Timed out loading Lumagen input labels; marking unavailable",
                err,
            )
            return
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Failed to load Lumagen input labels; marking unavailable: {err}",
                err,
            )
            return
        except ValueError as err:
            _LOGGER.debug("Failed to load Lumagen input labels: %s", err)
            return
        finally:
            self._input_labels_task = None

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=self.data.power_on if self.data else False,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=input_labels,
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_shutdown(self) -> None:
        """Disconnect from the Lumagen."""
        for task in (self._horizontal_refresh_task, self._input_labels_task):
            if task is not None and not task.done():
                task.cancel()

        async with self._lumagen_lock:
            await self._async_disconnect_locked()

    async def _async_ensure_connected(self) -> None:
        """Connect to the Lumagen if the current transport is disconnected."""
        if self._connected:
            return

        await self.device.connect()
        self._connected = True

    async def _async_disconnect_locked(self) -> None:
        """Disconnect from the Lumagen while the Lumagen lock is held."""
        try:
            await self.device.disconnect()
        except (OSError, RuntimeError, ValueError) as err:
            _LOGGER.debug("Failed to disconnect Lumagen transport cleanly: %s", err)
        finally:
            self._connected = False

    async def _async_query_input_labels(self) -> dict[int, str]:
        """Query Lumagen input labels using the managed connection."""
        async with self._lumagen_lock:
            await self._async_ensure_connected()
            return await self.device.query_input_labels("A")

    async def _async_reset_connection(self) -> None:
        """Reset the transport so the next poll or command reconnects."""
        async with self._lumagen_lock:
            await self._async_disconnect_locked()

    async def _async_handle_update_failure(
        self,
        message: str,
        err: BaseException | None = None,
    ) -> LumagenCoordinatorData:
        """Disconnect stale transport and return unavailable coordinator data."""
        await self._async_reset_connection()
        return self._handle_update_failure(message, err)

    async def _async_handle_command_failure(
        self,
        message: str,
        err: BaseException,
    ) -> None:
        """Mark the coordinator unavailable after a command failure."""
        data = await self._async_handle_update_failure(message, err)
        self.async_set_updated_data(data)

    async def _async_run_device_command(
        self,
        action: str,
        command: Callable[[], Awaitable[None]],
    ) -> None:
        """Run a Lumagen command with reconnect and failure handling."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                await command()
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                f"Timed out {action}; marking unavailable",
                err,
            )
            raise LumagenCommunicationError(
                f"Lumagen is unavailable while {action}"
            ) from err
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Error {action}; marking unavailable: {err}",
                err,
            )
            raise LumagenCommunicationError(
                f"Lumagen is unavailable while {action}"
            ) from err

    def _handle_update_failure(
        self,
        message: str,
        err: BaseException | None = None,
    ) -> LumagenCoordinatorData:
        """Return unavailable data or raise UpdateFailed for initial failures."""
        self._connected = False
        self._firmware_refresh_pending = True

        if self.data is not None:
            _LOGGER.debug(message)
            return LumagenCoordinatorData(
                power_on=self.data.power_on,
                available=False,
                input_number=self.data.input_number,
                input_memory=self.data.input_memory,
                input_labels=self.data.input_labels,
                status=self.data.status,
                sw_version=self.data.sw_version,
            )

        if err is None:
            raise UpdateFailed(message)

        raise UpdateFailed(message) from err

    async def async_send_remote_command(self, method_name: str) -> None:
        """Send a simple remote command to the Lumagen."""
        method = getattr(self.device, method_name)
        await self._async_run_device_command(
            f"sending Lumagen remote command {method_name}",
            method,
        )

    async def async_show_aspect(self) -> None:
        """Show aspect information."""
        await self._async_run_device_command(
            "showing Lumagen aspect information",
            self.device.show_aspect,
        )

    async def async_input_restart(self) -> None:
        """Restart Lumagen HDMI input connection."""
        await self._async_run_device_command(
            "restarting Lumagen HDMI input",
            self.device.input_restart,
        )

    async def async_output_restart(self) -> None:
        """Restart Lumagen HDMI output connection."""
        await self._async_run_device_command(
            "restarting Lumagen HDMI output",
            self.device.output_restart,
        )

    def _set_status(self, status: dict[str, StatusValue]) -> None:
        """Update only the status payload while preserving the rest of the data."""
        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=self.data.power_on if self.data else False,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=self._current_input_labels(),
                status=status,
                sw_version=self._current_sw_version(),
            )
        )

    def _current_input_number(self) -> int | None:
        """Return current input number."""
        if self.data is None:
            return None

        return self.data.input_number

    def _current_input_memory(self) -> str | None:
        """Return current input memory."""
        if self.data is None:
            return None

        return self.data.input_memory

    def _current_input_labels(self) -> dict[int, str] | None:
        """Return current input labels."""
        if self.data is None:
            return None

        return self.data.input_labels

    def _current_status(self) -> dict[str, StatusValue] | None:
        """Return current full-status values."""
        if self.data is None:
            return None

        return self.data.status

    def _current_sw_version(self) -> str | None:
        """Return current software version."""
        if self.data is None:
            return self.config_entry.data.get(CONF_SW_VERSION)

        return self.data.sw_version

    @staticmethod
    def _parse_full_status(
        full_status: LumagenFullStatus | None,
    ) -> dict[str, StatusValue] | None:
        """Parse useful fields from a Lumagen full-status model."""
        if full_status is None or len(full_status.parts) <= 19:
            return None

        return {
            "input_status": format_input_status(full_status.input_status_code),
            "source_vertical_rate": _none_if_zero(
                format_vertical_rate(full_status.input_rate_code)
            ),
            "source_vertical_resolution": _none_if_no_input(
                strip_leading_zeroes(full_status.input_vertical_resolution)
            ),
            "source_3d_mode": format_3d_mode(full_status.source_3d_mode),
            "input_config_number": _format_int(full_status.active_input_config),
            "source_raster_aspect": full_status.input_raster_aspect,
            "current_source_content_aspect": full_status.current_source_content_aspect,
            "nls_active": format_nls_active(full_status.nls_active),
            "output_mode_3d": format_3d_mode(full_status.output_3d_mode),
            "output_on": format_output_enabled_mask(full_status.output_enabled_mask),
            "input_memory": _format_input_memory(full_status.input_memory),
            "active_output_cms": full_status.active_output_cms,
            "active_output_style": full_status.active_output_style,
            "output_vertical_rate": _none_if_zero(
                format_vertical_rate(full_status.output_rate_code)
            ),
            "output_vertical_resolution": _none_if_no_input(
                strip_leading_zeroes(full_status.output_vertical_resolution)
            ),
            "output_aspect": full_status.output_raster_aspect,
            "output_color_space": format_output_colorspace(
                full_status.output_colorspace_code
            ),
            "source_dynamic_range": format_dynamic_range(
                full_status.input_dynamic_range_code
            ),
            "source_mode": format_scan_mode(full_status.input_mode),
            "output_mode": format_scan_mode(full_status.output_mode),
            "virtual_input_selected": _format_int(full_status.virtual_input_selected),
            "physical_input_selected": _format_int(full_status.physical_input_selected),
            "detected_source_raster_aspect": full_status.detected_source_raster_aspect,
            "detected_source_aspect": full_status.detected_source_aspect,
            "subtitle_shift_status": format_subtitle_shift_status(
                full_status.subtitle_shift_status
            ),
            "auto_aspect_status": format_auto_aspect_status(
                full_status.auto_aspect_status
            ),
        }

    async def async_set_input_label(
        self,
        memory: str,
        input_number: int,
        label: str,
    ) -> None:
        """Set a Lumagen input label and update cached labels."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                await self.device.set_input_label(memory, input_number, label)
                await self.device.save_config()
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                "Timed out setting Lumagen input label; marking unavailable",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while setting input label"
            ) from err
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Error setting Lumagen input label; marking unavailable: {err}",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while setting input label"
            ) from err

        labels = dict(self._current_input_labels() or {})

        if label.strip():
            labels[input_number] = label.strip()
        else:
            labels.pop(input_number, None)

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=self.data.power_on if self.data else True,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=labels,
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_set_input_labels(
        self,
        memory: str,
        labels_to_set: list[dict[str, object]],
    ) -> None:
        """Set multiple Lumagen input labels and update cached labels."""
        labels = dict(self._current_input_labels() or {})

        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()

                for item in labels_to_set:
                    input_number = int(item["input_number"])
                    label = str(item["label"]).strip()

                    await self.device.set_input_label(memory, input_number, label)

                    if label:
                        labels[input_number] = label
                    else:
                        labels.pop(input_number, None)

                await self.device.save_config()
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                "Timed out setting Lumagen input labels; marking unavailable",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while setting input labels"
            ) from err
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Error setting Lumagen input labels; marking unavailable: {err}",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while setting input labels"
            ) from err

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=self.data.power_on if self.data else True,
                available=True,
                input_number=self._current_input_number(),
                input_memory=self._current_input_memory(),
                input_labels=labels,
                status=self._current_status(),
                sw_version=self._current_sw_version(),
            )
        )

    async def async_set_auto_aspect(self, enabled: bool) -> None:
        """Enable or disable Lumagen auto aspect."""
        if enabled:
            await self._async_run_device_command(
                "enabling Lumagen auto aspect",
                self.device.auto_aspect_enable,
            )
        else:
            await self._async_run_device_command(
                "disabling Lumagen auto aspect",
                self.device.auto_aspect_disable,
            )

    async def async_set_nls(self, enabled: bool) -> None:
        """Enable or disable Lumagen NLS."""
        current_status = self._current_status() or {}
        current_enabled = current_status.get("nls_active") == "On"

        if current_enabled == enabled:
            return

        await self._async_run_device_command(
            "toggling Lumagen NLS",
            self.device.toggle_nls,
        )

    def _should_ignore_power_off(self) -> bool:
        """Return true if a power-off report is probably stale."""
        return monotonic() < self._ignore_power_off_until

    def _schedule_horizontal_status_refresh(self) -> None:
        """Schedule a delayed background refresh of output horizontal status."""
        if monotonic() < self._horizontal_refresh_retry_after:
            return

        if (
            self._horizontal_refresh_task is not None
            and not self._horizontal_refresh_task.done()
        ):
            return

        self._horizontal_refresh_task = self.hass.async_create_task(
            self._delayed_refresh_horizontal_status(),
            "lumagen_refresh_horizontal_status",
        )

    def _schedule_input_labels_load(self) -> None:
        """Schedule a background load of input labels."""
        if self._input_labels_task is not None and not self._input_labels_task.done():
            return

        self._input_labels_task = self.hass.async_create_task(
            self.async_load_input_labels(),
            "lumagen_load_input_labels",
        )

    async def _delayed_refresh_horizontal_status(self) -> None:
        """Refresh output horizontal status after a short debounce delay."""
        await asyncio.sleep(OUTPUT_REFRESH_DELAY_SECONDS)
        await self.async_refresh_horizontal_status()

    async def _add_horizontal_status(self, status: dict[str, StatusValue]) -> None:
        """Add output horizontal field to a status payload."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                output_info = await self.device.query_output_info()
        except LUMAGEN_TIMEOUT_ERRORS:
            _LOGGER.debug("Timed out loading Lumagen output horizontal status")
            return
        except (OSError, RuntimeError, ValueError) as err:
            _LOGGER.debug("Failed to load Lumagen output horizontal status: %s", err)
            return

        status.update(_parse_output_info(output_info))

    async def async_refresh_horizontal_status(self) -> None:
        """Refresh output horizontal resolution field."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                output_info = await self.device.query_output_info()
        except LUMAGEN_TIMEOUT_ERRORS:
            self._horizontal_refresh_retry_after = (
                monotonic() + OUTPUT_REFRESH_RETRY_DELAY_SECONDS
            )
            _LOGGER.debug(
                "Timed out refreshing Lumagen output horizontal status; "
                "keeping cached values"
            )
            return
        except (OSError, RuntimeError, ValueError) as err:
            self._horizontal_refresh_retry_after = (
                monotonic() + OUTPUT_REFRESH_RETRY_DELAY_SECONDS
            )
            _LOGGER.debug(
                "Failed to refresh Lumagen output horizontal status; "
                "keeping cached values: %s",
                err,
            )
            return

        self._horizontal_refresh_retry_after = 0.0

        status = dict(self._current_status() or {})
        status.update(_parse_output_info(output_info))
        self._set_status(status)

    async def async_refresh_info(self) -> None:
        """Refresh Lumagen runtime information."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                power_status = await self.device.query_power()
                power_on = _power_on(
                    power_status,
                    default=self.data.power_on if self.data else False,
                )
                input_info = await self.device.query_input()
                full_status = await self.device.query_full_status()

            status = self._parse_full_status(full_status)

            if status is not None:
                await self._add_horizontal_status(status)

        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                "Timed out refreshing Lumagen information; marking unavailable",
                err,
            )
            return
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Failed to refresh Lumagen information; marking unavailable: {err}",
                err,
            )
            return
        except ValueError as err:
            _LOGGER.debug("Invalid Lumagen refresh response: %s", err)
            return

        self.async_set_updated_data(
            LumagenCoordinatorData(
                power_on=power_on,
                available=True,
                input_number=_select_input_number(
                    status,
                    input_info.input_number if input_info else None,
                ),
                input_memory=_select_input_memory(
                    status,
                    input_info.memory if input_info else None,
                ),
                input_labels=self._current_input_labels(),
                status=status,
                sw_version=self._current_sw_version(),
            )
        )

    async def _async_refresh_firmware_version(self) -> str | None:
        """Refresh Lumagen software version if needed."""
        if not self._firmware_refresh_pending:
            return self._current_sw_version()

        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                lumagen_id = await self.device.query_id()
        except LUMAGEN_TIMEOUT_ERRORS:
            _LOGGER.debug("Timed out refreshing Lumagen software version")
            return self._current_sw_version()
        except (OSError, RuntimeError, ValueError) as err:
            _LOGGER.debug("Failed to refresh Lumagen software version: %s", err)
            return self._current_sw_version()

        self._firmware_refresh_pending = False
        return lumagen_id.software

    async def async_display_message(self, osd_message: LumagenOsdMessage) -> None:
        """Display a message on the Lumagen OSD."""
        try:
            async with self._lumagen_lock:
                await self._async_ensure_connected()
                await self.device.display_message(
                    duration=osd_message.duration,
                    options=OsdMessageOptions(
                        message=osd_message.message,
                        message_placement=osd_message.message_placement,
                        block_char=osd_message.block_char,
                        line1=osd_message.line1,
                        line2=osd_message.line2,
                        center_line1=osd_message.center_line1,
                        center_line2=osd_message.center_line2,
                    ),
                )
        except LUMAGEN_TIMEOUT_ERRORS as err:
            await self._async_handle_command_failure(
                "Timed out displaying Lumagen OSD message; marking unavailable",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while displaying OSD message"
            ) from err
        except (OSError, RuntimeError) as err:
            await self._async_handle_command_failure(
                f"Error displaying Lumagen OSD message; marking unavailable: {err}",
                err,
            )
            raise LumagenCommunicationError(
                "Lumagen is unavailable while displaying OSD message"
            ) from err

    async def async_clear_message(self) -> None:
        """Clear Lumagen OSD message."""
        await self._async_run_device_command(
            "clearing Lumagen OSD message",
            self.device.clear_message,
        )


def create_lumagen_device(config_entry: ConfigEntry) -> LumagenDevice:
    """Create a Lumagen device from a config entry."""
    data = config_entry.data
    connection_type = data[CONF_CONNECTION_TYPE]

    if connection_type == CONNECTION_TYPE_TCP:
        transport = TcpTransport(
            host=data[CONF_HOST],
            port=data.get(CONF_PORT, DEFAULT_PORT),
            timeout=DEFAULT_TIMEOUT,
        )
    elif connection_type == CONNECTION_TYPE_SERIAL:
        transport = SerialTransport(
            device=data[CONF_SERIAL_DEVICE],
            baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
            timeout=DEFAULT_TIMEOUT,
        )
    else:
        raise ValueError(f"Unsupported Lumagen connection type: {connection_type}")

    return LumagenDevice(LumagenProtocol(transport))


def _power_on(power_status: LumagenPowerStatus | None, default: bool) -> bool:
    """Return a bool from the typed Lumagen power model."""
    if power_status is None:
        return default

    return power_status.power_on


def _select_input_number(
    status: dict[str, StatusValue] | None,
    fallback: int | None,
) -> int | None:
    """Return selected input from status, falling back to ZQI00 input info."""
    if status is None:
        return fallback

    selected = status.get("virtual_input_selected")

    if isinstance(selected, int):
        return selected

    return fallback


def _select_input_memory(
    status: dict[str, StatusValue] | None,
    fallback: str | None,
) -> str | None:
    """Return selected input memory from status, falling back to ZQI00 input info."""
    if status is not None:
        input_memory = status.get("input_memory")

        if isinstance(input_memory, str) and input_memory in {"A", "B", "C", "D"}:
            return input_memory

    return fallback


def _format_int(value: str) -> int | None:
    """Format an integer field."""
    try:
        return int(value)
    except ValueError:
        return None


def _format_input_memory(value: str) -> str | None:
    """Format Lumagen input memory."""
    if value in {"A", "B", "C", "D"}:
        return value

    return None


def _parse_input_info(response: str) -> dict[str, StatusValue]:
    """Parse unsolicited ZQI01 input info response."""
    parts = response.split(",")

    if len(parts) < 5 or parts[0] != "!I01":
        return {}

    return {
        "source_horizontal_resolution": _none_if_no_input(
            strip_leading_zeroes(parts[3])
        ),
    }


def _parse_output_info(
    output_info: LumagenOutputInfo | None,
) -> dict[str, StatusValue]:
    """Parse ZQO01 output info response."""
    if output_info is None:
        return {}

    return {
        "output_horizontal_resolution": _none_if_no_input(
            strip_leading_zeroes(output_info.horizontal_resolution)
        ),
    }


def _none_if_zero(value: str) -> str | None:
    """Return None for zero-like values."""
    if value in {"0", "0.00"}:
        return None

    return value


def _none_if_no_input(value: str) -> str | None:
    """Return None for no-input values."""
    if value in {"0", "000", "0000", "No Input"}:
        return None

    return value
