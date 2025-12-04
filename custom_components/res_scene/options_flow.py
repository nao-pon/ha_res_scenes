import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ResSceneOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        hass = self.hass
        manager = hass.data[DOMAIN]["manager"]

        restore_light_attributes = self.config_entry.options.get(
            "restore_light_attributes", False
        )
        scenes = list(manager.stored_data.keys())
        scenes_select = sorted(scenes)

        schema = vol.Schema(
            {
                vol.Required(
                    "restore_light_attributes", default=restore_light_attributes
                ): bool,
                vol.Optional("delete_scene"): vol.In(scenes_select),
                vol.Optional("rename_from"): vol.In(scenes_select),
                vol.Optional("rename_to", default=""): str,
            }
        )

        if user_input is not None:
            delete_id = user_input.pop("delete_scene", None)
            rename_from = user_input.pop("rename_from", None)
            rename_to = user_input.pop("rename_to", "").strip()

            if delete_id:
                await manager.delete_scene(delete_id)

            if rename_from and rename_to != "":
                if rename_from in manager.stored_data:
                    if rename_to in manager.stored_data and rename_to != rename_from:
                        # Could show an error or warning to the user
                        _LOGGER.warning(
                            "Scene %s already exists, skipping rename from %s",
                            rename_to,
                            rename_from,
                        )
                    else:
                        manager.stored_data[rename_to] = manager.stored_data.pop(
                            rename_from
                        )
                        await manager.store.async_save(manager.stored_data)
                        # Dispatch signals for removed and added scenes
                        async_dispatcher_send(
                            self.hass, f"{DOMAIN}_scene_removed", rename_from
                        )
                        async_dispatcher_send(
                            self.hass, f"{DOMAIN}_scene_added", rename_to
                        )

            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=schema)
