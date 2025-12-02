import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN
from .helpers import to_json_safe

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN]["manager"]
    selectors = []
    selectors.append(ResSceneSelector(hass, manager, True))
    selectors.append(ResSceneSelector(hass, manager, False))
    async_add_entities(selectors)

    async def scene_update(_):
        for selector in selectors:
            await selector.async_update_options()

    async_dispatcher_connect(
        hass,
        f"{DOMAIN}_scene_added",
        scene_update,
    )
    async_dispatcher_connect(
        hass,
        f"{DOMAIN}_scene_removed",
        scene_update,
    )

    return True


class ResSceneSelector(SelectEntity):
    """Provides a dropdown list of scenes."""

    def __init__(self, hass, manager, do_apply: bool = True):
        self.hass = hass
        self.manager = manager
        self._attr_options = []
        self._attr_name = f"Res Scene {('Apply' if do_apply else 'JSON')} Selector"
        self._attr_unique_id = f"res_scene_{('apply' if do_apply else 'json')}_selector"
        self.entity_id = "select." + self._attr_unique_id
        self._do_apply = do_apply
        self._attr_extra_state_attributes = {}

    @property
    def extra_state_attributes(self):
        return self._attr_extra_state_attributes

    async def async_update_options(self):
        """Refresh the list of scenes."""
        scenes = (["Apply scene"] if self._do_apply else []) + list(
            self.manager.stored_data.keys()
        )
        self._attr_options = scenes

        # Set current selection if needed
        if scenes and (self._attr_current_option not in scenes):
            self._attr_current_option = scenes[0]
            await self.async_select_option(self._attr_current_option)
        elif not scenes:
            self._attr_current_option = None

        # Push state change to HA
        self.async_write_ha_state()

    async def async_select_option(self, option: str):
        """Called when user selects a scene."""
        self._attr_current_option = option
        if self._do_apply:
            if option != "Apply scene":
                await self.manager.apply_scene(option)
                self.async_write_ha_state()
        else:
            pretty = json.dumps(
                to_json_safe(self.manager.stored_data.get(option, {})),
                ensure_ascii=False,
                indent=2,
            )
            self._attr_extra_state_attributes["json"] = pretty
            self.async_write_ha_state()
