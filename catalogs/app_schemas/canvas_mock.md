# canvas_mock Schema

**Deploy order**: 13 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8013)  
**Base URL**: `http://172.17.46.46:8013/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `canvasJSON` | `FabricCanvasJSON \| null` | Serialized Fabric canvas. Default: `null`; after editor initialization it is the live canvas serialization |
| `canvasImage` | `null` | Always persisted as `null` |
| `timestamp` | `string` | ISO timestamp. Generated in the default and refreshed on each persistence |

Selection, zoom, pan mode, undo/redo stacks, and save-status indicators are component state and are not part of the injected/persisted schema.

### FabricCanvasJSON fields

The app loads and saves Fabric JSON. The fields relied on by its own code are `{ version?, objects[], background? }`.

### Fabric object fields

Serialized Fabric objects contain Fabric's standard properties. Fields directly created, edited, compared, or explicitly preserved by this app are:

`{ id, name?, type, left, top, width, height, scaleX, scaleY, angle, fill, stroke, strokeWidth, opacity, visible, selectable, lockMovementX, lockMovementY, lockRotation, lockScalingX, lockScalingY }`

Type-specific fields:

- circle: `{ radius }`
- line: `{ x1, y1, x2, y2 }`
- text/IText: `{ text, fontFamily, fontSize, fontWeight, fontStyle, textAlign }`
- image: `{ src }` plus its serialized dimensions/scales

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8013/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "canvasJSON": {
          "objects": [{"id": "rect-1", "type": "Rect", "left": 100, "top": 100, "width": 100, "height": 100, "fill": "#3498db"}],
          "background": "#ffffff"
        },
        "canvasImage": null,
        "timestamp": "2026-01-01T00:00:00.000Z"
      }
    }
  }
}
```

## Observable State Changes (for LLM evaluation)

All persisted actions refresh `timestamp` and keep `canvasImage` as `null`.

| User Action | State Field Changed |
|-------------|---------------------|
| Add a rectangle, circle, text object, line, or image | `canvasJSON.objects` (object appended) |
| Move, resize, rotate, restyle, align, edit text, or toggle formatting | matching fields in `canvasJSON.objects[*]` |
| Lock/unlock or show/hide a layer, then save | lock/visibility fields in `canvasJSON.objects[*]` |
| Delete selected object(s) | `canvasJSON.objects` |
| Clear/reset the canvas | `canvasJSON.objects`, `canvasJSON.background` |
| Undo or redo, then save | `canvasJSON` (loaded history snapshot) |
| Import canvas JSON | `canvasJSON` (replaced by imported serialization) |
| Start from a template/create payload | `canvasJSON.objects`, `canvasJSON.background` |
| Click Save | `canvasJSON`, `timestamp` |

