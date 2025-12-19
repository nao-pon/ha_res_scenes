import asyncio
import logging
from copy import deepcopy
from typing import Any

from homeassistant.components.climate.const import (
    ATTR_FAN_MODE,
    ATTR_HUMIDITY,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_SWING_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.lock.const import LockState
from homeassistant.components.media_player.const import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_VOLUME_LEVEL,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    ATTR_STATE,
    ATTR_TEMPERATURE,
    SERVICE_CLOSE_COVER,
    SERVICE_LOCK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_STOP,
    SERVICE_OPEN_COVER,
    SERVICE_SELECT_OPTION,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_UNLOCK,
    SERVICE_VOLUME_SET,
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

from .const import ACTION_TIMEOUT_DEFAULT, DOMAIN

_LOGGER = logging.getLogger(__name__)

DISPATCHER_UPDATE = "res_scene_updated"
SERVICE_CALL_DELAY = 1.0  # Delay in seconds when calling a service to the same entity

COLOR_MODE_ATTRS = {
    "onoff": set(),
    "brightness": {"brightness", "brightness_pct"},
    "hs": {"hs_color", "brightness", "brightness_pct"},
    "rgb": {"rgb_color", "brightness", "brightness_pct"},
    "rgbw": {"rgbw_color", "brightness", "brightness_pct"},
    "rgbww": {"rgbww_color", "brightness", "brightness_pct"},
    "xy": {"xy_color", "brightness", "brightness_pct"},
    "color_temp": {"color_temp", "color_temp_kelvin", "brightness", "brightness_pct"},
}

ATTR_ATTRS = {
    "profile": {"brightness", "brightness_pct"},
    "white": {"brightness", "brightness_pct"},
}

COMMON_LIGHT_ATTRS = {"effect", "flash", "white", "profile"}


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
        timeout: float = ACTION_TIMEOUT_DEFAULT,
        expected: str | None = None,
    ):
        """
        Call a service and wait for entity state change.

        Note: Captures any state change on entity_id, regardless of source.
        Callers must serialize operations on the same entity to avoid
        attributing state changes from concurrent operations.

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
                        ATTR_ENTITY_ID: entity_id,
                        "old_state": old_state,
                        "new_state": new_state,
                        "old_value": old_value,
                        "new_value": new_value,
                        "matched": True
                        if expected is None
                        else (new_value == expected),
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
            target={ATTR_ENTITY_ID: entity_id},
            blocking=False,
        )

        try:
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            result = {
                ATTR_ENTITY_ID: entity_id,
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
        Execute a sequence of service actions one after another, awaiting each action's observed state change.
        
        Parameters:
            actions (list[dict]): List of action dictionaries. Each action must include:
                - 'domain' (str): Service domain to call.
                - 'service' (str): Service name to call.
                - 'entity_id' (str): Entity to target and observe for state changes.
                - 'service_data' (dict, optional): Data to pass to the service call.
                - 'expected' (str, optional): Expected new state value to consider the action matched.
                - 'timeout' (float, optional): Seconds to wait for the expected state; falls back to ACTION_TIMEOUT_DEFAULT.
        
        Returns:
            list[dict]: A list of result dictionaries for each action. Each result contains keys such as
            'entity_id', 'old_state', 'new_state', 'matched' (true if expected state was observed), and
            'timeout' (true if the wait expired).
        """
        results = []
        for action in actions:
            result = await self.async_call_and_wait_state(
                entity_id=action[ATTR_ENTITY_ID],
                domain=action[ATTR_DOMAIN],
                service=action[ATTR_SERVICE],
                service_data=action.get(ATTR_SERVICE_DATA, {}),
                expected=action.get("expected"),
                timeout=action.get("timeout", ACTION_TIMEOUT_DEFAULT),
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
        """
        Save a snapshot of the specified entities' states and attributes under the given scene id.
        
        Takes current states for each entity in snapshot_entities and persists them as a scene. When enabled via options, lights that are off may be briefly toggled to capture full attributes (color, brightness, etc.). If an entity's current state is unavailable, a previously stored state for the same scene will be used when that previous state is valid. Persists the scene to storage and dispatches a scene-added signal.
        
        Parameters:
            scene_id (str): Identifier to store the scene under.
            snapshot_entities (list): Iterable of entity IDs to include in the snapshot.
            options (dict | None): Optional scene-specific options (merged with user options). Recognized keys include:
                - "action_timeout" (float): timeout in seconds for actions used to capture attributes.
                - "restore_light_attributes" (bool): if true, attempt to capture full light attributes by briefly turning lights on/off.
        """
        _options = deepcopy(self._user_options)
        _options.update(options or {})
        states = {}
        timeout = _options.get("action_timeout", ACTION_TIMEOUT_DEFAULT)

        async def snapshot_light(eid: str, state: str):
            """
            Capture a light's current on-state attributes by briefly turning it on and then off to create a snapshot.
            
            Parameters:
            	eid (str): Entity ID of the light to snapshot.
            	state (str): Original state string to record as the saved state.
            
            Returns:
            	dict: {"state_obj": <state object>, "save_state": <state str>} containing the captured state object and the saved state string if the snapshot succeeded, or None if the snapshot failed.
            """
            results = await self.async_run_actions_sequentially(
                [
                    {
                        ATTR_DOMAIN: "light",
                        ATTR_SERVICE: SERVICE_TURN_ON,
                        ATTR_ENTITY_ID: eid,
                        ATTR_SERVICE_DATA: {"transition": 0},
                        "expected": STATE_ON,
                        "timeout": timeout,
                    },
                    {
                        ATTR_DOMAIN: "light",
                        ATTR_SERVICE: SERVICE_TURN_OFF,
                        ATTR_ENTITY_ID: eid,
                        ATTR_SERVICE_DATA: {"transition": 0},
                        "expected": STATE_OFF,
                        "timeout": timeout,
                    },
                ]
            )
            turn_on_result = results[0]
            turn_off_result = results[1]
            if turn_on_result.get("timeout") or not turn_on_result.get("matched"):
                _LOGGER.warning(
                    "Failed to capture light attributes for %s: turn_on %s",
                    eid,
                    "timed out"
                    if turn_on_result.get("timeout")
                    else "did not match expected state",
                )
                return None
            if turn_off_result.get("timeout") or not turn_off_result.get("matched"):
                _LOGGER.warning(
                    "Light %s did not reach 'off' state after snapshot: %s",
                    eid,
                    "timed out"
                    if turn_off_result.get("timeout")
                    else "did not match expected state",
                )
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
                    and state_obj.state == STATE_OFF
                ):
                    # Make async snapshot task
                    tasks.append(snapshot_light(eid, STATE_OFF))
                else:
                    # Add what is readily available immediately
                    states[eid] = {
                        ATTR_STATE: state_obj.state,
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
                        ATTR_STATE: save_state,
                        "attributes": deepcopy(state_obj.attributes),
                    }

        # Restore a saved state if the specified entity's data is unavailable
        for prev_eid, prev_state in self.stored_data.get(scene_id, {}).items():
            if prev_eid in snapshot_entities and (
                prev_eid not in states
                or states.get(prev_eid, {}).get(ATTR_STATE) == STATE_UNAVAILABLE
            ):
                # Only use fallback if the previous state itself is valid
                if prev_state.get(ATTR_STATE) not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    states[prev_eid] = deepcopy(prev_state)
                    _LOGGER.info(
                        "Using fallback state for %s in scene '%s' (current state unavailable)",
                        prev_eid,
                        scene_id,
                    )
                else:
                    _LOGGER.warning(
                        "Skipping fallback for %s in scene '%s' (previous state also invalid)",
                        prev_eid,
                        scene_id,
                    )

        if options is not None:
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
        saved_options = states.pop("_options", {}) or {}
        if not isinstance(saved_options, dict):
            _LOGGER.warning(
                "Ignored invalid _options for scene %s: %r",
                scene_id,
                saved_options,
            )
        else:
            _options.update(saved_options)

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
        Restore a single entity to its saved state and attributes by invoking the appropriate Home Assistant services.
        
        Parameters:
            eid (str): The entity_id to restore (e.g., "light.kitchen").
            info (dict): Saved scene data for the entity. Expected keys:
                - "state": The saved state value (string).
                - "attributes": Mapping of attribute names to saved values; attributes with value None are ignored.
            options (dict): Runtime options that affect restoration behavior. Recognized keys:
                - "restore_light_attributes" (bool): If true, restore light attributes even when the saved state is STATE_OFF.
                - "action_timeout" (float): Timeout in seconds used when waiting for expected state changes.
        """
        domain = eid.split(".")[0]
        state = info.get(ATTR_STATE)
        target = {ATTR_ENTITY_ID: eid}
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

        timeout = options.get("action_timeout", ACTION_TIMEOUT_DEFAULT)

        # small helper for sequential calls with delay
        async def call_service(service_domain, service, data, target):
            """
            Call a Home Assistant service for the given target and wait a short delay to throttle subsequent calls.
            
            Parameters:
                service_domain (str): Domain of the service to call (e.g., "light", "switch").
                service (str): Service name within the domain (e.g., "turn_on", "set_temperature").
                data (dict | None): Service call data payload; may be None.
                target (dict | list | str | None): Target specification for the service call (entity_id(s) or target dict).
            """
            await self.hass.services.async_call(
                service_domain, service, data, blocking=False, target=target
            )
            await asyncio.sleep(SERVICE_CALL_DELAY)

        # ---- light ----
        if domain == "light":
            restore_attrs = options.get("restore_light_attributes", False)
            should_restore = (state == STATE_ON) or restore_attrs

            allowed_attrs = None
            for attr, allowed_keys in ATTR_ATTRS.items():
                if attr in attrs:
                    allowed_attrs = allowed_keys
                    break
            if allowed_attrs is None:
                color_mode = attrs.get("color_mode", "color_temp")
                allowed_attrs = COLOR_MODE_ATTRS.get(color_mode, set())
                if color_mode not in COLOR_MODE_ATTRS:
                    _LOGGER.warning(
                        "Unknown color_mode '%s' for %s, allowing only common attributes",
                        color_mode,
                        eid,
                    )

            safe_attrs = {
                k: v
                for k, v in attrs.items()
                if k in allowed_attrs or k in COMMON_LIGHT_ATTRS
            }
            if "color_temp_kelvin" in safe_attrs and "color_temp" in safe_attrs:
                safe_attrs.pop("color_temp")
            if "brightness" in safe_attrs and "brightness_pct" in safe_attrs:
                safe_attrs.pop("brightness_pct")

            if should_restore:
                data = {ATTR_ENTITY_ID: eid, "transition": 0, **safe_attrs}
                result = await self.async_call_and_wait_state(
                    entity_id=eid,
                    domain="light",
                    service=SERVICE_TURN_ON,
                    service_data=data,
                    expected=STATE_ON,
                    timeout=timeout,
                )
                if result.get("timeout") or not result.get("matched"):
                    _LOGGER.warning(
                        "Failed to restore light %s to 'on' state: %s",
                        eid,
                        f"timeout ({timeout}s)"
                        if result.get("timeout")
                        else "state mismatch",
                    )

            if state == STATE_OFF:
                data = {ATTR_ENTITY_ID: eid, "transition": 0}
                result = await self.async_call_and_wait_state(
                    entity_id=eid,
                    domain="light",
                    service=SERVICE_TURN_OFF,
                    service_data=data,
                    expected=STATE_OFF,
                    timeout=timeout,
                )
                if result.get("timeout") or not result.get("matched"):
                    _LOGGER.warning(
                        "Failed to restore light %s to 'off' state: %s",
                        eid,
                        f"timeout ({timeout}s)"
                        if result.get("timeout")
                        else "state mismatch",
                    )

        # ---- cover ----
        elif domain == "cover":
            position = attrs.get("position")
            tilt = attrs.get("tilt_position")

            if position is not None:
                await call_service(
                    "cover",
                    SERVICE_SET_COVER_POSITION,
                    {ATTR_ENTITY_ID: eid, "position": position},
                    target,
                )
            if tilt is not None:
                await call_service(
                    "cover",
                    SERVICE_SET_COVER_TILT_POSITION,
                    {ATTR_ENTITY_ID: eid, "tilt_position": tilt},
                    target,
                )

            if position is None and tilt is None:
                if state == STATE_OPEN:
                    service = SERVICE_OPEN_COVER
                elif state == STATE_CLOSED:
                    service = SERVICE_CLOSE_COVER
                else:
                    service = (
                        SERVICE_OPEN_COVER if state == STATE_ON else SERVICE_CLOSE_COVER
                    )
                await call_service("cover", service, {ATTR_ENTITY_ID: eid}, target)

        # ---- climate ----
        elif domain == "climate":
            hvac_mode = state if state not in (None, "") else None

            if hvac_mode:
                data = {
                    ATTR_ENTITY_ID: eid,
                    ATTR_HVAC_MODE: hvac_mode,
                }
                if (
                    hvac_mode == HVACMode.HEAT_COOL
                    and ATTR_TARGET_TEMP_LOW in attrs
                    and ATTR_TARGET_TEMP_HIGH in attrs
                ):
                    data.update(
                        {
                            ATTR_TARGET_TEMP_LOW: attrs[ATTR_TARGET_TEMP_LOW],
                            ATTR_TARGET_TEMP_HIGH: attrs[ATTR_TARGET_TEMP_HIGH],
                        }
                    )
                    await call_service("climate", SERVICE_SET_TEMPERATURE, data, target)
                elif ATTR_TEMPERATURE in attrs:
                    data.update({ATTR_TEMPERATURE: attrs[ATTR_TEMPERATURE]})
                    await call_service("climate", SERVICE_SET_TEMPERATURE, data, target)

                # 3. other sub-attributes
                for key in [
                    ATTR_FAN_MODE,
                    ATTR_SWING_MODE,
                    ATTR_PRESET_MODE,
                    ATTR_HUMIDITY,
                ]:
                    if key in attrs:
                        svc = f"set_{key}"
                        await call_service(
                            "climate",
                            svc,
                            {ATTR_ENTITY_ID: eid, key: attrs[key]},
                            target,
                        )

        # ---- media_player ----
        elif domain == "media_player":
            if state == STATE_ON:
                service = SERVICE_TURN_ON
            elif state == STATE_OFF:
                service = SERVICE_TURN_OFF
            elif state == STATE_PLAYING:
                service = SERVICE_MEDIA_PLAY
            elif state == STATE_PAUSED:
                service = SERVICE_MEDIA_PAUSE
            elif state == STATE_IDLE:
                service = SERVICE_MEDIA_STOP
            else:
                _LOGGER.warning("Unknown media_player state: %s", state)
                return
            await call_service(domain, service, {ATTR_ENTITY_ID: eid}, target)

            if ATTR_MEDIA_VOLUME_LEVEL in attrs:
                await call_service(
                    domain,
                    SERVICE_VOLUME_SET,
                    {
                        ATTR_ENTITY_ID: eid,
                        ATTR_MEDIA_VOLUME_LEVEL: attrs[ATTR_MEDIA_VOLUME_LEVEL],
                    },
                    target,
                )
            if ATTR_INPUT_SOURCE in attrs:
                await call_service(
                    domain,
                    SERVICE_SELECT_SOURCE,
                    {ATTR_ENTITY_ID: eid, ATTR_INPUT_SOURCE: attrs[ATTR_INPUT_SOURCE]},
                    target,
                )

        # ---- lock ----
        elif domain == "lock":
            service = SERVICE_LOCK if state == LockState.LOCKED else SERVICE_UNLOCK
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
            service = SERVICE_TURN_ON if state == STATE_ON else SERVICE_TURN_OFF
            await call_service(domain, service, {ATTR_ENTITY_ID: eid}, target)

        # ---- input_number ----
        elif domain == "input_number":
            try:
                value = float(state)
            except (ValueError, TypeError):
                _LOGGER.warning("Invalid input_number state for %s: %r", eid, state)
                return
            await call_service(
                "input_number",
                "set_value",
                {ATTR_ENTITY_ID: eid, "value": value},
                target,
            )

        # ---- input_select ----
        elif domain == "input_select":
            await call_service(
                "input_select",
                SERVICE_SELECT_OPTION,
                {ATTR_ENTITY_ID: eid, "option": state},
                target,
            )

        # ---- input_text ----
        elif domain == "input_text":
            await call_service(
                "input_text", "set_value", {ATTR_ENTITY_ID: eid, "value": state}, target
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