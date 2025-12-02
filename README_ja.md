# Home Assistant 永続シーン (Restorable Scenes)

このカスタム統合は、Home Assistant の再起動後も残る **永続シーン** を作成できるようにします。  
標準の `scene.create` では再起動のたびにシーンが消えてしまいますが、この統合ではシーンを保存して維持できます。

## ⚙️ 主な機能

- 指定エンティティの現在状態をキャプチャし、永続シーンとして保存  
- 保存されたシーンは再起動後も保持される  
- 標準サービス `scene.turn_on` でシーンを有効化  
- シーン削除用のサービスを提供  
- light / switch / climate / media_player など、主要なドメインには概ね対応  
- ただし **すべての機器・属性の完全復元は保証しません**  
  追加対応が必要な場合は Issue をご利用ください

---

## 🚨 注意事項

- 機器や統合によっては復元動作が異なる場合があります  
- 一部の属性は正しく再現できない可能性があります  
- 問題があれば GitHub の Issue へ報告してください

---

## 📦 インストール

### HACS（推奨）

1. HACS → **Integrations**  
2. 「Custom repositories」で本リポジトリを追加（カテゴリ: Integration）  
3. インストール後、Home Assistant を再起動  

### 手動インストール

1. `custom_components/res_scene/` を `custom_components/` にコピー  
2. Home Assistant を再起動

---

## 🛠 サービス

---

### `res_scene.create`

指定したエンティティの現在状態を保存し、永続シーンを作成します。

**サービスデータ**

| 項目 | 必須 | 説明 |
|------|------|------|
| `scene_id` | 必須 | 永続シーンの ID |
| `snapshot_entities` | 必須 | スナップショット対象の entity_id リスト |

**例**

```yaml
service: res_scene.create
data:
  scene_id: evening_mode
  snapshot_entities:
    - light.living_room
    - switch.aircon
    - media_player.tv
````

作成されるエンティティは:

```
scene.evening_mode
```

有効化するには:

```yaml
service: scene.turn_on
target:
  entity_id: scene.evening_mode
```

---

### `res_scene.delete`

永続シーンを削除します。

⚠ **重要:**
このサービスは `scene_id` ではなく **`entity_id`（例: `scene.xxx`）** を受け取ります。

**例**

```yaml
service: res_scene.delete
data:
  entity_id: scene.evening_mode
```

---

## 🤝 貢献・Issue

特定のデバイスが正しく復元されない場合、または追加ドメイン対応が必要な場合は Issue を作成してください。

## 📄 ライセンス

MIT License