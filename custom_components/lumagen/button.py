"""Button platform for the Lumagen integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LumagenConfigEntry
from .const import DOMAIN
from .coordinator import LumagenDataUpdateCoordinator
from .entity import LumagenEntity


@dataclass(frozen=True)
class LumagenButtonDescription:
    """Lumagen button description."""

    key: str
    name: str
    icon: str
    press_fn: Callable[[LumagenButton], Awaitable[None]]
    entity_category: EntityCategory | None = None


async def _press_show_aspect(entity: LumagenButton) -> None:
    """Show aspect information."""
    await entity.coordinator.async_show_aspect()


async def _press_input_restart(entity: LumagenButton) -> None:
    """Restart Lumagen HDMI input connection."""
    await entity.coordinator.async_input_restart()


async def _press_output_restart(entity: LumagenButton) -> None:
    """Restart Lumagen HDMI output connection."""
    await entity.coordinator.async_output_restart()


async def _press_refresh_info(entity: LumagenButton) -> None:
    """Refresh Lumagen information."""
    await entity.coordinator.async_refresh_info()


BUTTONS = [
    LumagenButtonDescription(
        key="show_aspect",
        name="Show Aspect",
        icon="mdi:aspect-ratio",
        press_fn=_press_show_aspect,
    ),
    LumagenButtonDescription(
        key="input_restart",
        name="HDMI Input Restart",
        icon="mdi:video-input-hdmi",
        press_fn=_press_input_restart,
    ),
    LumagenButtonDescription(
        key="output_restart",
        name="HDMI Output Restart",
        icon="mdi:video-input-hdmi",
        press_fn=_press_output_restart,
    ),
    LumagenButtonDescription(
        key="refresh_info",
        name="Refresh Info",
        icon="mdi:refresh",
        press_fn=_press_refresh_info,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumagenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lumagen button entities."""
    coordinator: LumagenDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LumagenButton(coordinator, description) for description in BUTTONS]
    )


class LumagenButton(LumagenEntity, ButtonEntity):
    """Lumagen button entity."""

    def __init__(
        self,
        coordinator: LumagenDataUpdateCoordinator,
        description: LumagenButtonDescription,
    ) -> None:
        """Initialize the Lumagen button."""
        super().__init__(coordinator, description.key)
        self._description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category

    def press(self) -> None:
        """Press the button."""
        raise NotImplementedError

    async def async_press(self) -> None:
        """Handle the button press."""
        await self._description.press_fn(self)
