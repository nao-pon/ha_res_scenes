import logging
from functools import cached_property
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.helpers import entity_registry
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN]["manager"]
    entities = [
        ResSceneEntity(hass, manager, scene_id, data)
        for scene_id, data in manager.stored_data.items()
    ]
    async_add_entities(entities)
    if not hass.data[DOMAIN].get("entities"):
        hass.data[DOMAIN]["entities"] = {}
    for entity in entities:
        hass.data[DOMAIN]["entities"][entity.entity_id] = entity

    # realtime apply via dispatcher
    async def scene_added(scene_id):
        # check registed
        if any(e._scene_id == scene_id for e in entities):
            return

        new_entity = ResSceneEntity(
            hass, manager, scene_id, manager.stored_data.get(scene_id, {})
        )
        async_add_entities([new_entity])
        entities.append(new_entity)
        hass.data[DOMAIN]["entities"][new_entity.entity_id] = new_entity
        _LOGGER.info("Added ResSceneEntity %s via dispatcher", scene_id)

    async def scene_removed(scene_id):
        ent_reg = entity_registry.async_get(hass)

        # copy list() for list edit
        for e in list(entities):
            if e._scene_id == scene_id:
                # 1st remove from entity_registry
                ent_reg.async_remove(e.entity_id)

                # next remove entity
                await e.async_remove()

                # Ensure entity is fully removed: remove from state machine
                hass.states.async_remove(e.entity_id)

                # remove local list
                entities.remove(e)
                hass.data[DOMAIN]["entities"].pop(e.entity_id, None)

                _LOGGER.info("Removed ResSceneEntity %s via dispatcher", scene_id)

    # regist dispatcher
    async_dispatcher_connect(hass, f"{DOMAIN}_scene_added", scene_added)
    async_dispatcher_connect(hass, f"{DOMAIN}_scene_removed", scene_removed)
    return True


class ResSceneEntity(Scene):
    """Restorable SceneEntity"""

    def __init__(self, hass, manager, scene_id, data: dict | None = None):
        self.hass = hass
        self.manager = manager
        self._scene_id = scene_id
        self._attr_name = f"Res: {scene_id}"
        self._attr_unique_id = f"res_{scene_id}"
        self._extra_data = data or {}

    @cached_property
    def icon(self):
        return "mdi:palette"

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose extra data to Home Assistant state machine."""
        return self._extra_data if self._extra_data else None

    def set_extra_state_attributes(self, data: dict):
        """Update extra_state_attributes dynamically."""
        self._extra_data = data
        self.async_write_ha_state()

    async def async_activate(self, **kwargs: Any):
        """Restore saved state + attributes"""
        await self.manager.apply_scene(self._scene_id)
        _LOGGER.debug("ResScene '%s' restored.", self._scene_id)
