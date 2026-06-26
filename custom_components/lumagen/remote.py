"""Remote platform for the Lumagen integration."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import RemoteEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LumagenConfigEntry
from .const import DOMAIN, REMOTE_COMMANDS
from .coordinator import LumagenDataUpdateCoordinator
from .entity import LumagenEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Lumagen remote entity."""
    coordinator: LumagenDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([LumagenRemote(coordinator)])


class LumagenRemote(LumagenEntity, RemoteEntity):  # pylint: disable=abstract-method
    """Lumagen remote entity."""

    _attr_name = "Remote"

    def __init__(self, coordinator: LumagenDataUpdateCoordinator) -> None:
        """Initialize the Lumagen remote."""
        super().__init__(coordinator, "remote")

    @property
    def is_on(self) -> bool:
        """Return true if the Lumagen is on."""
        return self.coordinator.data is not None and self.coordinator.data.power_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the Lumagen on."""
        await self.coordinator.async_power_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put the Lumagen in standby."""
        await self.coordinator.async_standby()

    async def async_send_command(
        self,
        command: Iterable[str],
        **kwargs: Any,
    ) -> None:
        """Send commands to the Lumagen."""
        for item in command:
            method_name = REMOTE_COMMANDS.get(item)

            if method_name is None:
                _LOGGER.warning("Unsupported Lumagen remote command: %s", item)
                continue

            if method_name == "power_on":
                await self.coordinator.async_power_on()
                continue

            if method_name == "standby":
                await self.coordinator.async_standby()
                continue

            await self.coordinator.async_send_remote_command(method_name)
