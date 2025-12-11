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
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

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
        self.stored_data: dict[str, Any] = stored_data
        # stored_data: {scene_id: {entity_id: {"state": ..., "attributes": {...}}}}
        self._user_options: dict[str, Any] = {}

    async def async_call_and_wait_state(
        self,
        entity_id: str,
        domain: str,
        service: str,
        service_data: dict | None = None,
        timeout: float = 5.0,
        expected: str | None = None,
    ):
        """
        Call a service and wait for entity state change.

        Args:
            entity_id: Target entity to observe
            domain: Service domain (e.g. 'light')
            service: Service name (e.g. 'turn_on')
            service_data: Dict passed to hass.services.async_call()
            timeout: Max seconds to wait
            expected: If provided, wait until state == expected

        Returns:
            dict: {
                "entity_id": str,
                "old_state": State | None,
                "new_state": State | None,
                "old_value": str | None,
                "new_value": str | None,
                "matched": bool,
                "timeout": bool,
            }
        """
        hass = self.hass

        future: asyncio.Future = asyncio.get_running_loop().create_future()

        @callback
        def _state_changed(event: Event[EventStateChangedData]):
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            new_value = new_state.state if new_state else None
            old_value = old_state.state if old_state else None

            # wait until matched state to expected
            if expected is not None and new_value != expected:
                return

            if not future.done():
                future.set_result(
                    {
                        "entity_id": entity_id,
                        "old_state": old_state,
                        "new_state": new_state,
                        "old_value": old_value,
                        "new_value": new_value,
                        "matched": (new_value == expected) if expected else True,
                        "timeout": False,
                    }
                )

        # regist event listener
        remove_listener = async_track_state_change_event(
            hass, [entity_id], _state_changed
        )

        # call service
        await hass.services.async_call(
            domain,
            service,
            service_data or {},
            target={"entity_id": entity_id},
            blocking=False,
        )

        try:
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            result = {
                "entity_id": entity_id,
                "old_value": None,
                "new_value": None,
                "old_state": None,
                "new_state": None,
                "matched": False,
                "timeout": True,
            }
        finally:
            remove_listener()

        return result

    async def async_run_actions_sequentially(self, actions: list[dict]):
        """
        Run actions sequentially.
        Each dict must contain:
            - domain
            - service
            - data (service_data)
            - entity_id (to observe)
            - expected (optional)
            - timeout (optional, default 5.0)

        Returns:
            list[dict]: Result dictionaries with 'timeout' and 'matched' flags.
                Callers must validate results to detect failures.
        """
        results = []
        for action in actions:
            result = await self.async_call_and_wait_state(
                entity_id=action["entity_id"],
                domain=action["domain"],
                service=action["service"],
                service_data=action.get("data", {}),
                expected=action.get("expected"),
                timeout=action.get("timeout", 5.0),
            )
            results.append(result)
        return results

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
            results = await self.async_run_actions_sequentially(
                [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_id": eid,
                        "data": None,
                        "expected": "on",
                    },
                    {
                        "domain": "light",
                        "service": "turn_off",
                        "entity_id": eid,
                        "data": None,
                        "expected": None,
                    },
                ]
            )
            turn_on_result = results[0]
            if turn_on_result.get("timeout") or not turn_on_result.get("matched"):
                _LOGGER.warning(
                    "Failed to capture light attributes for %s: turn_on %s",
                    eid,
                    "timed out"
                    if turn_on_result.get("timeout")
                    else "did not match expected state",
                )
                return None
            return {"state_obj": turn_on_result.get("new_state"), "save_state": state}

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
                if result is None:
                    continue
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
        """
        Restore an entity's saved state and attributes by calling the appropriate Home Assistant services with short delays between service calls.

        Parameters:
                eid (str): The entity_id to restore (e.g., "light.kitchen").
                info (dict): Saved scene data for the entity. Expected keys:
                        - "state": The saved state value (string).
                        - "attributes": A mapping of attribute names to saved values.
                options (dict): Runtime options that affect restoration behavior. Recognized key:
                        - "restore_light_attributes" (bool): If true, restore light attributes even when the saved state is "off".
        """
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
            await asyncio.sleep(1)  # 1 second interval, can be changed as needed

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
                results = await self.async_run_actions_sequentially(
                    [
                        {
                            "domain": "light",
                            "service": "turn_on",
                            "entity_id": eid,
                            "data": data,
                            "expected": "on",
                        },
                    ]
                )
                if results[0].get("timeout") or not results[0].get("matched"):
                    _LOGGER.warning(
                        "Failed to restore light %s to 'on' state: %s",
                        eid,
                        "timeout" if results[0].get("timeout") else "state mismatch",
                    )

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

    def get_scene(self, scene_id: str):
        """
        Retrieve stored data for a scene by its identifier.

        Returns:
            The scene data dictionary if found, otherwise None.
        """
        return self.stored_data.get(scene_id)

    def set_user_options(self, user_options: dict):
        """
        Store a deep copy of per-user scene restoration options.

        Parameters:
            user_options (dict): Mapping of user-specific option keys to values; the input is deep-copied and replaces the manager's current user options.
        """
        self._user_options = deepcopy(user_options)

    def get_user_options(self):
        """
        Return a deep copy of the currently stored per-user scene options.

        Returns:
            dict: A deep copy of the internal user options mapping.
        """
        return deepcopy(self._user_options)
