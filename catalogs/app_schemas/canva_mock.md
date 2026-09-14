# canva_mock Schema

**Deploy order**: 14 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8014)  
**Base URL**: `http://172.17.46.46:8014/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `canvasConfig` | `CanvasConfig` | Canvas dimensions/colors. Default: `{width:800, height:600, backgroundColor:"#ffffff"}` |
| `elements` | `Element[]` | Ordered canvas objects (back to front). Default: `[]` |
| `uploads` | `Upload[]` | Uploaded image records. Default: `[]` |
| `lastShareLink` | `string` | Last generated mock sharing URL. Loaded as `""`; added to persisted state on the first save |
| `timestamp` | `string` | ISO timestamp written whenever the persisted state is saved; not present in the pristine default object |

`selectedId` appears in `/go.current_state`, but is transient selection state: it is neither loaded from injected state nor persisted.

### CanvasConfig fields

`{ width, height, backgroundColor }`

### Element fields

All elements use `{ id, type, x, y }`. Fields actually used by the supported element types are:

- rectangle: `{ width, height, fill }`
- circle: `{ radius, fill }`
- star: `{ numPoints, innerRadius, outerRadius, fill }`
- text: `{ text, fontSize, fontFamily, fontWeight?, fontStyle?, fill }`
- image: `{ src, width, height, bgRemoved? }`
- fields produced by editing/rendering: `{ rotation?, opacity?, visible? }`

### Upload fields

`{ id, url, name, type, createdAt }`

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8014/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "canvasConfig": {"width": 800, "height": 600, "backgroundColor": "#ffffff"},
        "elements": [{"id": "title-1", "type": "text", "x": 80, "y": 80, "text": "Hello", "fontSize": 48, "fontFamily": "Arial", "fill": "#000000"}],
        "uploads": [],
        "lastShareLink": ""
      }
    }
  }
}
```

## Observable State Changes (for LLM evaluation)

Every persisted change below also refreshes `timestamp`.

| User Action | State Field Changed |
|-------------|---------------------|
| Add a shape, text, or uploaded image to the canvas | `elements` (new item appended) |
| Drag, resize, rotate, recolor, edit text/font, change opacity, or remove an image background | matching fields in `elements[*]` |
| Delete or duplicate an element | `elements` |
| Move an element forward/backward/front/back | order of `elements` |
| Hide/show an element | `elements[*].visible` |
| Apply a template | `elements` (replaced with template elements and fresh IDs) |
| Undo or redo | `elements` (replaced with a history snapshot) |
| Upload an image | `uploads` (new `{id,url,name,type,createdAt}` entry) |
| Resize the canvas | `canvasConfig.width`, `canvasConfig.height` |
| Generate a share link | `lastShareLink` |

