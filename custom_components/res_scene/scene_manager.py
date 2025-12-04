import asyncio
import logging
from copy import deepcopy
from typing import Any

from homeassistant.const import (
    STATE_CLOSED,
    STATE_IDLE,
    STATE_OFF,
    STATE_ON,
    STATE_OPEN,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DISPATCHER_UPDATE = "res_scene_updated"

COLOR_MODE_ATTRS = {
    "onoff": set(),
    "brightness": {"brightness", "brightness_pct"},
    "hs": {"hs_color", "brightness", "brightness_pct"},
    "rgb": {"rgb_color", "brightness", "brightness_pct"},
    "rgbw": {"rgbw_color", "brightness", "brightness_pct"},
    "rgbww": {"rgbww_color", "brightness", "brightness_pct"},
    "xy": {"xy_color", "brightness", "brightness_pct"},
    "color_temp": {"color_temp", "kelvin", "brightness", "brightness_pct"},
}

COMMON_LIGHT_ATTRS = {"effect", "flash", "transition", "white_value", "profile"}


class ResSceneManager:
    """Managing restorable scenes"""

    def __init__(self, hass: HomeAssistant, store, stored_data):
        self.hass = hass
        self.store = store
        self.stored_data: dict[str, Any] = (
            stored_data  # {scene_id: {entity_id: {"state": ..., "attributes": {...}}}}
        )
        self._user_options: dict[str, Any] = {}

    async def restore_scenes(self):
        """Restore saved scenes on restart (EntityRegistry creation)"""
        for scene_id in self.stored_data.keys():
            # await self.create_or_update_scene(scene_id)
            async_dispatcher_send(self.hass, f"{DOMAIN}_scene_added", scene_id)
        _LOGGER.info("Restored %s scenes", len(self.stored_data))

    async def save_scene(
        self, scene_id: str, snapshot_entities: list, options: dict | None = None
    ):
        """Save the state and attributes of the specified entity"""
        _options = deepcopy(self._user_options)
        _options.update(options or {})
        states = {}

        async def snapshot_light(eid: str, state: str):
            """Take snapshot for a single light"""
            state_obj = self.hass.states.get(eid)
            future = asyncio.Future()

            def callback(entity_id, old_state, new_state, fut=future):
                if not fut.done() and new_state.state == "on" and new_state.attributes:
                    fut.set_result(new_state)

            unsub = async_track_state_change(self.hass, [eid], callback)

            try:
                # temporarily turn on
                await self.hass.services.async_call(
                    "light", "turn_on", {"entity_id": eid}, blocking=True
                )

                # wait for state to reflect
                try:
                    state_obj = await asyncio.wait_for(future, timeout=1.0)
                except asyncio.TimeoutError:
                    state_obj = self.hass.states.get(eid)  # fallback

                # restore turn off
                await self.hass.services.async_call(
                    "light", "turn_off", {"entity_id": eid}
                )
            finally:
                unsub()

            return {"state_obj": state_obj, "save_state": state}

        tasks = []
        for eid in snapshot_entities:
            domain = eid.split(".")[0]
            if domain in (
                "sensor",
                "binary_sensor",
                "device_tracker",
                "camera",
                "vacuum",
                "scene",
                "script",
            ):
                _LOGGER.warning(
                    "Domain %s (%s) is not support in %s.", domain, eid, DOMAIN
                )
                continue

            if state_obj := self.hass.states.get(eid):
                if (
                    _options.get("restore_light_attributes")
                    and domain == "light"
                    and state_obj.state == "off"
                ):
                    # Make async snapshot task
                    tasks.append(snapshot_light(eid, "off"))
                else:
                    # Add what is readily available immediately
                    states[eid] = {
                        "state": state_obj.state,
                        "attributes": deepcopy(state_obj.attributes),
                    }

        # Run in parallel and combine the results
        if tasks:
            results = await asyncio.gather(*tasks)
            for result in results:
                state_obj = result["state_obj"]
                save_state = result["save_state"]
                if state_obj:
                    eid = state_obj.entity_id
                    states[eid] = {
                        "state": save_state,
                        "attributes": deepcopy(state_obj.attributes),
                    }

        states["_options"] = options
        self.stored_data[scene_id] = states
        await self.store.async_save(self.stored_data)
        async_dispatcher_send(self.hass, f"{DOMAIN}_scene_added", scene_id)
        _LOGGER.info("Saved res scene '%s' with %s entities", scene_id, len(states))

    async def delete_scene(self, scene_id):
        """Remove scene"""
        # remove from stored data
        if scene_id in self.stored_data:
            del self.stored_data[scene_id]
            await self.store.async_save(self.stored_data)
            _LOGGER.info("Deleted scene data %s", scene_id)
        else:
            _LOGGER.warning("Scene %s not found in store", scene_id)

        # dispatcher notify
        async_dispatcher_send(self.hass, f"{DOMAIN}_scene_removed", scene_id)

    async def apply_scene(self, scene_id) -> bool:
        """Apply a saved scene"""
        if scene_id not in self.stored_data:
            _LOGGER.warning("Scene %s not found", scene_id)
            return False

        states = deepcopy(self.stored_data[scene_id] or {})
        _options = deepcopy(self._user_options)
        _options.update(states.pop("_options", {}))

        success = True

        async def safe_apply(eid, info):
            nonlocal success
            try:
                await self.apply_state(eid, info, _options)
            except Exception as e:  # noqa: BLE001
                success = False
                _LOGGER.error(
                    "Failed to apply state for %s in scene %s: %s", eid, scene_id, e
                )

        await asyncio.gather(*(safe_apply(eid, info) for eid, info in states.items()))

        if success:
            _LOGGER.info("Applied scene %s successfully", scene_id)
        else:
            _LOGGER.warning("Scene %s applied with errors", scene_id)
        return success

    async def apply_state(self, eid: str, info: dict, options: dict):
        """Restore state + attributes for each entity with sequential calls and delay"""
        domain = eid.split(".")[0]
        state = info.get("state")
        target = {"entity_id": eid}
        attrs = {}
        for _key, _value in info.get("attributes", {}).items():
            if _value is not None:
                attrs[_key] = _value

        if not state:
            _LOGGER.warning("Saved state is None, skip.")
            return

        target_state = self.hass.states.get(eid)
        if target_state is None or target_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            _LOGGER.warning("Target entity is unusable.")
            return

        # skip non-restorable domains
        if domain in (
            "sensor",
            "binary_sensor",
            "device_tracker",
            "camera",
            "vacuum",
            "scene",
            "script",
        ):
            _LOGGER.debug("Domain %s is not restorable, skip.", domain)
            return

        # check state
        if domain == "lock" and state not in ("locked", "unlocked"):
            _LOGGER.debug("Domain %s is not restorable state %s , skip.", domain, state)
            return

        # small helper for sequential calls with delay
        async def call_service(service_domain, service, data, target):
            await self.hass.services.async_call(service_domain, service, data, target)
            await asyncio.sleep(1)  # 1秒間隔、必要に応じて変更可能

        # ---- light ----
        if domain == "light":
            restore_attrs = options.get("restore_light_attributes", False)
            should_restore = (state == "on") or restore_attrs

            color_mode = attrs.get("color_mode", "onoff")
            allowed_attrs = COLOR_MODE_ATTRS.get(color_mode, set())
            safe_attrs = {
                k: v
                for k, v in attrs.items()
                if k in allowed_attrs or k in COMMON_LIGHT_ATTRS
            }

            if should_restore:
                data = {"entity_id": eid, **safe_attrs}
                await call_service("light", "turn_on", data, target=target)

            if state == "off":
                await call_service(
                    "light", "turn_off", {"entity_id": eid}, target=target
                )

        # ---- cover ----
        elif domain == "cover":
            position = attrs.get("position")
            tilt = attrs.get("tilt_position")

            if position is not None:
                await call_service(
                    "cover",
                    "set_cover_position",
                    {"entity_id": eid, "position": position},
                    target,
                )
            if tilt is not None:
                await call_service(
                    "cover",
                    "set_cover_tilt_position",
                    {"entity_id": eid, "tilt_position": tilt},
                    target,
                )

            if position is None and tilt is None:
                if state in (STATE_OPEN, "open"):
                    service = "open_cover"
                elif state in (STATE_CLOSED, "closed"):
                    service = "close_cover"
                else:
                    service = "open_cover" if state == "on" else "close_cover"
                await call_service("cover", service, {"entity_id": eid}, target)

        # ---- climate ----
        elif domain == "climate":
            hvac_mode = state if state not in (None, "") else None

            if hvac_mode:
                data = {
                    "entity_id": eid,
                    "hvac_mode": hvac_mode,
                }
                if (
                    hvac_mode == "heat_cool"
                    and "target_temp_low" in attrs
                    and "target_temp_high" in attrs
                ):
                    data.update(
                        {
                            "target_temp_low": attrs["target_temp_low"],
                            "target_temp_high": attrs["target_temp_high"],
                        }
                    )
                    await call_service("climate", "set_temperature", data, target)
                elif "temperature" in attrs:
                    data.update({"temperature": attrs["temperature"]})
                    await call_service("climate", "set_temperature", data, target)

                # 3. other sub-attributes
                for key in ["fan_mode", "swing_mode", "preset_mode", "aux_heat"]:
                    if key in attrs:
                        svc = f"set_{key}"
                        await call_service(
                            "climate", svc, {"entity_id": eid, key: attrs[key]}, target
                        )

        # ---- media_player ----
        elif domain == "media_player":
            if state in (STATE_ON, "on"):
                service = "turn_on"
            elif state == STATE_OFF:
                service = "turn_off"
            elif state == STATE_PLAYING:
                service = "media_play"
            elif state == STATE_PAUSED:
                service = "media_pause"
            elif state == STATE_IDLE:
                service = "media_stop"
            else:
                _LOGGER.warning("Unknown media_player state: %s", state)
                return
            await call_service(domain, service, {"entity_id": eid}, target)

            # if "media_content_id" in attrs and "media_content_type" in attrs:
            #     await call_service(
            #         domain,
            #         "play_media",
            #         {
            #             "entity_id": eid,
            #             "media": {
            #                 "media_content_id": attrs["media_content_id"],
            #                 "media_content_type": attrs["media_content_type"],
            #             },
            #         },
            #         target,
            #     )
            if "volume_level" in attrs:
                await call_service(
                    domain,
                    "volume_set",
                    {
                        "entity_id": eid,
                        "volume_level": attrs["volume_level"],
                    },
                    target,
                )
            if "source" in attrs:
                await call_service(
                    domain,
                    "select_source",
                    {"entity_id": eid, "source": attrs["source"]},
                    target,
                )
            # if "media_position" in attrs:
            #     await call_service(
            #         domain,
            #         "media_seek",
            #         {"entity_id": eid, "seek_position": attrs["media_position"]},
            #         target,
            #     )

        # ---- lock ----
        elif domain == "lock":
            service = "lock" if state == "lock" else "unlock"
            data = {"entity_id": eid}
            await call_service(domain, service, data, target)

        # ---- simple on/off domains ----
        elif domain in (
            "fan",
            "humidifier",
            "remote",
            "siren",
            "switch",
            "input_boolean",
        ):
            service = "turn_on" if state == "on" else "turn_off"
            await call_service(domain, service, {"entity_id": eid}, target)

        # ---- input_number ----
        elif domain == "input_number":
            await call_service(
                "input_number",
                "set_value",
                {"entity_id": eid, "value": float(state)},
                target,
            )

        # ---- input_select ----
        elif domain == "input_select":
            await call_service(
                "input_select",
                "select_option",
                {"entity_id": eid, "option": state},
                target,
            )

        # ---- input_text ----
        elif domain == "input_text":
            await call_service(
                "input_text", "set_value", {"entity_id": eid, "value": state}, target
            )

        else:
            _LOGGER.debug("Domain %s not handled, skip.", domain)

    def get_scene(self, scene_id):
        return self.stored_data.get(scene_id)

    def set_user_options(self, user_options: dict):
        self._user_options = user_options
