from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_VERSION
from .scene_manager import ResSceneManager

PLATFORMS = ["scene", "select"]


async def async_setup_entry(hass: HomeAssistant, entry):
    store = Store(hass, STORE_VERSION, f"{DOMAIN}.json")
    stored_data = await store.async_load() or {}

    # make DOMAIN key
    hass.data.setdefault(DOMAIN, {"entities": {}})

    # initialize manager
    if "manager" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["manager"] = ResSceneManager(hass, store, stored_data)

    manager = hass.data[DOMAIN]["manager"]

    manager.set_user_options(
        {
            "restore_light_attributes": entry.options.get(
                "restore_light_attributes", False
            ),
        }
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Restore saved scene
    await manager.restore_scenes()

    # regist service
    async def save_scene(call):
        scene_id = call.data.get("scene_id", "")

        # If no scene_id, raise an error
        if not scene_id:
            raise HomeAssistantError(
                "Missing required field: scene_id.",
                translation_key="no_scene_id",
                translation_domain=DOMAIN,
            )

        manager = hass.data[DOMAIN]["manager"]

        # Collect snapshot entities (initial list)
        snapshot_entities = set(call.data.get("snapshot_entities") or [])

        # Get entity registry
        ent_reg = entity_registry.async_get(hass)

        # Expand entities from areas
        snapshot_areas = call.data.get("snapshot_areas") or []
        for area_id in snapshot_areas:
            area_entries = entity_registry.async_entries_for_area(ent_reg, area_id)
            snapshot_entities.update(entry.entity_id for entry in area_entries)

        # Expand entities from labels
        snapshot_labels = call.data.get("snapshot_labels") or []
        for label_id in snapshot_labels:
            label_entities = [
                entry.entity_id
                for entry in ent_reg.entities.values()
                if label_id in getattr(entry, "labels", set())
            ]
            snapshot_entities.update(label_entities)

        # Filter out non-existing entities
        states = hass.states
        snapshot_entities = {
            entity_id
            for entity_id in snapshot_entities
            if states.get(entity_id) is not None
        }

        # If no valid entities remain, raise an error
        if not snapshot_entities:
            raise HomeAssistantError(
                "No valid entities found.",
                translation_key="no_valid_entities",
                translation_domain=DOMAIN,
            )

        # Build options dictionary using user options defaults
        options = {
            key: call.data.get(key, default)
            for key, default in manager._user_options.items()
        }

        # Save scene
        await manager.save_scene(scene_id, list(snapshot_entities), options)

    async def delete_scene(call):
        entity_id = call.data.get("entity_id")
        if entity_id:
            if entity := hass.data[DOMAIN]["entities"].get(entity_id):
                scene_id = entity._scene_id
                await manager.delete_scene(scene_id)

    async def apply_scene(call):
        entity_id = call.data.get("entity_id")
        if entity_id:
            if entity := hass.data[DOMAIN]["entities"].get(entity_id):
                scene_id = entity._scene_id
                await manager.apply_scene(scene_id)

    hass.services.async_register(DOMAIN, "create", save_scene)
    hass.services.async_register(DOMAIN, "delete", delete_scene)
    # hass.services.async_register(DOMAIN, "apply", apply_scene)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    hass.data.get(DOMAIN, {}).pop("manager", None)
    return unload_ok
