from homeassistant import config_entries

from .const import DOMAIN
from .options_flow import ResSceneOptionsFlow


class ResSceneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Main config flow."""

    async def async_step_user(self, user_input=None):
        # check registed
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        return self.async_create_entry(title="Res Scene", data={})

    @staticmethod
    def async_get_options_flow(config_entry):
        return ResSceneOptionsFlow()
