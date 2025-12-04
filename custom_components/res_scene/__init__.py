from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_VERSION
from .scene_manager import ResSceneManager

PLATFORMS = ["scene", "select"]


async def async_setup_entry(hass, entry):
    store = Store(hass, STORE_VERSION, f"{DOMAIN}.json")
    stored_data = await store.async_load() or {}

    # make DOMAIN key
    hass.data.setdefault(DOMAIN, {})
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
        scene_id = call.data.get("scene_id")
        snapshot_entities = call.data.get("snapshot_entities", [])
        options = {
            key: call.data.get(key, default)
            for key, default in manager._user_options.items()
        }
        if scene_id and snapshot_entities:
            await manager.save_scene(scene_id, snapshot_entities, options)

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
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["scene"])

    hass.data.get(DOMAIN, {}).pop("manager", None)
    return unload_ok
