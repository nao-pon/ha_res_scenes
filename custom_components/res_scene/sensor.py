import json

from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up ResScene integration."""
    manager = hass.data[DOMAIN]["manager"]

    # Register the Scene List Sensor
    # sensor = ResSceneListSensor(hass, manager)
    sensor = ResSceneAttributesSensor(
        hass, manager, "select.res_scene_selector_no_apply"
    )
    async_add_entities([sensor])

    return True


class ResSceneAttributesSensor(Entity):
    """Sensor that exposes selected scene's attributes from a select entity."""

    _attr_name = "Res Scene Attributes"
    _attr_unique_id = "res_scene_attributes"

    def __init__(self, hass: HomeAssistant, manager, selector_entity_id: str):
        self.hass = hass
        self.manager = manager
        self.selector_entity_id = selector_entity_id
        self._attr_state = None
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self):
        """Track select entity state changes."""
        async_track_state_change_event(
            self.hass, self.selector_entity_id, self._select_changed
        )

    async def _select_changed(self, event: Event[EventStateChangedData]):
        """Called when select entity changes."""
        entity = self.hass.states.get(self.selector_entity_id)
        if not entity:
            self._attr_state = None
            self._attr_extra_state_attributes = {}
        else:
            self._attr_state = entity.state
            self._attr_extra_state_attributes = {
                "rawdata": self.manager.stored_data.get(self._attr_state, {})
            }

            pretty = json.dumps(
                self._attr_extra_state_attributes["rawdata"],
                ensure_ascii=False,
                indent=2,
            )

            self._attr_extra_state_attributes["json"] = pretty

        self.async_write_ha_state()

    @property
    def state(self):
        return self._attr_state or "None"

    @property
    def extra_state_attributes(self):
        return self._attr_extra_state_attributes

    async def async_update(self):
        # get current selected option from select entity by entity_id
        selected = self.hass.states.get(self.selector_entity_id)
        current_option = selected.state if selected else None

        self._attr_state = current_option

        if current_option:
            self._attr_extra_state_attributes = self.manager.stored_data.get(
                current_option, {}
            )
        else:
            self._attr_extra_state_attributes = {}

        self.async_write_ha_state()


class ResSceneListSensor(Entity):
    """Sensor exposing a list of all saved ResScenes."""

    def __init__(self, hass, manager):
        """Initialize with HA instance and manager."""
        self.hass = hass
        self.manager = manager
        self._attr_name = "Res Scene List"
        self._attr_unique_id = "res_scene_list"

    @property
    def state(self):
        """Sensor state is None; we store the scene list in attributes."""
        return None

    @property
    def extra_state_attributes(self):
        """Return dictionary of stored scene IDs."""
        return {"scenes": list(self.manager.stored_data.keys())}

    async def async_added_to_hass(self):
        """Register a callback to update the sensor when scene list changes."""
        # self.manager.register_scene_change_callback(self.async_update_sensor)
        async_dispatcher_connect(
            self.hass, f"{DOMAIN}_scene_added", self.async_update_sensor
        )
        async_dispatcher_connect(
            self.hass, f"{DOMAIN}_scene_removed", self.async_update_sensor
        )

    async def async_update_sensor(self, scene_id):
        """Update HA state to reflect current scene list."""
        self.async_write_ha_state()
