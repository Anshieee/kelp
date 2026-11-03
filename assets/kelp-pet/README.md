# Kelp motion kit

For Akshat's desktop app, phone app, and website. These are clean vector recreations of the owner-supplied round Kelp character, preserving the blue/green/peach/gray state palette and expressions. They are not crops of the reference sheet. Everything has a transparent background; no labels or UI are baked into the artwork.

## Start here

```sh
git fetch origin
git switch --track origin/feat/kelp-pet-animations
python -m http.server 8000
# Open http://localhost:8000/assets/kelp-pet/preview.html
```

The standalone gallery also opens directly from a downloaded folder. Serve over HTTP when testing the separate web component (external ES modules do not load from `file://`). It includes all ten loops, light/dark canvases, and a pause control. Copy this folder into the frontend's public assets directory. The kit does not modify any app, production site, build pipeline, or backend state.

## Files

| File | Use |
| --- | --- |
| `<state>.webp` | Transparent animated WebP, 384 × 384, 2.56 seconds, loops forever |
| `<state>.png` | Native sprite atlas, 2048 × 2048, 8 columns × 8 rows |
| `<state>.svg` | Scalable still for reduced motion / fallback / initial loading |
| `manifest.json` | State names, labels, filenames, atlas geometry and timing |
| `kelp-pet.js` | Zero-dependency web component for browser and desktop WebViews |
| `preview.gif` | Animated overview of all ten states (light canvas, for sharing) |
| `preview.html` | Responsive ten-state gallery with pause and theme controls |

Atlases contain **64 frames**, left-to-right then top-to-bottom. Each frame is **256 × 256**, lasts **40 ms** (25 fps), and shares the same anchor and transparent canvas. Bodies may move inside the canvas; do not trim individual frames. WebP encoders can combine identical frames; use the encoded frame durations rather than assuming every WebP frame is 40 ms.

## States and motion

| State | Expression / motion | Suggested trigger |
| --- | --- | --- |
| `idle` | Blue, gentle float, occasional blink | Connected, no active task |
| `working` | Green, rhythmic bounce, orbiting activity dots | Task executing |
| `waiting` | Peach, slow breathing, amber approval ring | Needs user input or approval |
| `done` | Blue smiling eyes, happy landing and sparkles | Confirmed task completion |
| `offline` | Gray, muted eyes, very quiet drift | Disconnected |
| `curious` | Head tilt, raised glance, question mark | Listening / exploring |
| `happy` | Smiling eyes, soft sway | Positive feedback |
| `excited` | Cheering eyes, two hops, sparkles | Celebration |
| `thinking` | Eyes looking side to side, three pulsing dots | Planning / processing |
| `surprised` | Wider eyes, quick lift, expression marks | Unexpected event |

`done` and `excited` are loops for asset reuse. In the app, normally show them for one 2.56-second cycle and return to the current real state. Do not reset animation on every streamed token. Debounce frequent status updates and preserve `waiting` until the actual request is resolved. The animation is visual feedback only: it does not grant approval or prove that a payment or login succeeded. Keep approval details and Approve/Reject buttons in the app UI.

## Website / Electron / Tauri / mobile WebView

```html
<script type="module" src="/assets/kelp-pet/kelp-pet.js"></script>
<kelp-pet id="companion" state="idle" style="width:160px"></kelp-pet>
<script>
  const pet = document.querySelector('#companion');
  pet.setAttribute('state', 'working');
  // For a different hosted asset folder:
  // pet.setAttribute('asset-base', '/static/kelp-pet/');
  // pet.toggleAttribute('paused', true);
</script>
```

The component supplies a screen-reader label, falls back to `idle` for unknown states, uses SVG stills for `prefers-reduced-motion`, and pauses when the document is hidden. Set `label="Kelp: waiting for your approval"` for a more specific accessible label. It does not announce every frame. Add a separate polite live status region if your interface needs announcements.

React can render `<kelp-pet state={state} />` after importing the module in a browser-only entry point. For TypeScript, declare the custom element in your framework's JSX element types, or wrap it in a component. Do not import the browser module during server-side rendering. A plain `<img src=".../idle.webp" alt="Kelp" />` also works, but the app must then handle reduced motion and lifecycle pausing itself.

## Native phone / desktop renderer

Use WebP only when your image decoder supports **animated** WebP with alpha. Otherwise use the PNG atlas; it needs no animated-image codec and is suitable for a Canvas, Skia, SpriteKit, or game-style renderer. Decode only the active atlas and avoid caching all ten decoded sheets on a phone (each RGBA sheet is approximately 16 MiB).

```js
// elapsedMs uses a monotonic animation clock; reset on a deliberate state change.
const frame = Math.floor(elapsedMs / 40) % 64;
const sourceX = (frame % 8) * 256;
const sourceY = Math.floor(frame / 8) * 256;
// Draw source rect (sourceX, sourceY, 256, 256) into your destination square.
context.drawImage(atlas, sourceX, sourceY, 256, 256, 0, 0, size, size);
```

Use the manifest values instead of hard-coding these numbers in production. Pause the clock when offscreen or the app backgrounds. Respect the OS reduced-motion setting using the SVG poster (or the first atlas cell if the native renderer cannot decode SVG). Never use a timer that advances by one frame per callback: calculate from elapsed time to avoid drifting when the app drops frames.

## Rebuild / change the artwork

The editable SVG drawing and motion curves live in `assets/kelp-pet/build_pet.py`:

```sh
python -m pip install Pillow cairosvg
python assets/kelp-pet/build_pet.py
```

These are optional **art export** dependencies, not Kelp runtime dependencies. Changing palettes, eyes, timing, or motion in the generator regenerates every format consistently. Rebuilding can take several minutes because the WebP output is lossless. Generated assets may vary byte-for-byte between encoder versions; the geometry and timing contract is in the manifest.

Before shipping, check the gallery on light and dark backgrounds, reduced motion, small phone sizes, and the actual native decoder. This branch supplies the kit; Akshat still needs to connect it to the apps' actual status events.
