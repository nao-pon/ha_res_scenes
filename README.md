# Restorable Scenes for Home Assistant

This custom integration lets you create **persistent scenes** that survive Home Assistant restarts.  
By contrast, Home Assistant's built-in `scene.create` only generates temporary scenes that disappear after a restart.

## ⚙️ What it does

- Capture the current state of selected entities and save them as a persistent scene.  
- Persist the saved scene so it remains available after a reboot.  
- Activate any created persistent scene using the standard `scene.turn_on` service.  
- Delete persistent scenes using the provided delete service.  
- Supports many common domains (light, switch, climate, media_player, etc.).  
- However, **full restoration is not guaranteed for all devices or attributes**.  
  If you need additional support, please file an Issue.

---

## 🚨 Limitations

- Some entities or attributes may not restore perfectly.  
- Behavior varies depending on the device/integration.  
- For support of new device types, please open an Issue.

---

## 📦 Installation

### HACS (recommended)

1. Open HACS → **Integrations**  
2. Add this repository as a **Custom Repository** (category: Integration)  
3. Install the integration and restart Home Assistant

### Manual installation

1. Copy `custom_components/res_scene/` to your Home Assistant `custom_components/` directory  
2. Restart Home Assistant

---

## 🛠 Services

---

### `res_scene.create`

Create a persistent scene by capturing the current state of the specified entities.

**Service fields**

| Field | Required | Description |
|-------|----------|-------------|
| `scene_id` | Yes | Identifier for the persistent scene |
| `snapshot_entities` | Yes | List of entity IDs to snapshot |

**Example**

```yaml
service: res_scene.create
data:
  scene_id: evening_mode
  snapshot_entities:
    - light.living_room
    - switch.aircon
    - media_player.tv
````

Once created, this scene becomes available as:

```
scene.evening_mode
```

Activate it via:

```yaml
service: scene.turn_on
target:
  entity_id: scene.evening_mode
```

---

### `res_scene.delete`

Delete a previously created persistent scene.

⚠ **Important:**
This service now uses **`entity_id`**, not `scene_id`.

**Example**

```yaml
service: res_scene.delete
data:
  entity_id: scene.evening_mode
```

---

## 🤝 Contributing

If certain devices or attributes are not restored correctly, or you need more domains supported — please open an Issue.

## 📄 License

MIT License