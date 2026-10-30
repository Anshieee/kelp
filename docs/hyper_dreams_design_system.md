# DESIGN.md — Hyper Dreams (`hyper-dreams.com`)

> **Scope note:** Free Dither Effects Pack excluded by owner decision 2026-10-30.

## 1. Project Overview & Brand Identity

* **Website**: [https://www.hyper-dreams.com](https://www.hyper-dreams.com)
* **Brand / Organization**: **HYPER DREAMS** — Artist Bookings & Management Agency (based in Berlin, Germany)
* **Industry**: Underground Electronic Music, Techno, Trance, Industrial, & Club Culture
* **Design & Development Studio**: **NaughtyDuk©** (UK)
* **Recognition**: Awwwards Honorable Mention, CSS Design Awards, One Page Love Featured One-Pager
* **Aesthetic Persona**: **Gritty Cyber-Rave Minimalism / Industrial HUD One-Pager**

### Design Philosophy
Hyper Dreams represents high-energy underground electronic artists (such as Cassie Raptor, CLTX, DBBD, DJ AYA, Kalte Liebe, and Miss Bashful). Rather than using a conventional corporate agency multi-page layout, the website functions as an **immersive, single-viewport digital flyer and technical dossier**. It pairs a pitch-black club void with gritty dither/glitch shaders, cybernetic monospace typography, and structured swiss-grid artist profile modals. Its dither/glitch texture and accent palette are drawn from two Ivan Kamzyst inspiration PDFs — **Bitmap Dreams Color Edition** and **Dither Matter**.

---

## 2. Technology Stack & Architecture

| Layer | Technology / Implementation | Purpose |
| :--- | :--- | :--- |
| **Core Platform & CMS** | **Webflow** (`cdn.prod.website-files.com`) | Hosts the single-page layout and dynamically populates the Artist Roster (`role="list"`) and modal dossier fields via Webflow CMS Collections. |
| **Motion & Choreography** | **GSAP (GreenSock)** | Drives the sequenced intro loader, per-character logo hover wave, and crisp modal open/close transitions. |
| **Visual Shaders & FX** | **WebGL / HTML5 Canvas** | Powers the reactive glitch/dither background textures and the liquid warp distortion on the "About Us" view. |
| **Commerce Integration** | External Subdomain (`shop.hyper-dreams.com`) | Dedicated merchandise & vinyl storefront linked from the top-right HUD. |
| **Booking Workflow** | Dynamic `mailto:` Routing | Routes booking inquiries directly to the assigned agent (`carlo@`, `fabio@`, `paul@`, `guerand@`, or `bookings@hyper-dreams.com`). |

---

## 3. Color Palette & Surface Tokens

The color language traces to two Ivan Kamzyst inspiration PDFs — **Bitmap Dreams Color Edition** (paper-cream grounds, ink/charcoal text, jade/sea-green + sea-glass blue + umber) and **Dither Matter** (vermilion + ultramarine + acid green + lilac-pink with marbled/cellular dither). The implemented dark-mode surfaces below carry these hues as accents and dither fields.

### From Bitmap Dreams Color Edition

| Token Name | Hex / Value | Role & Usage |
| :--- | :--- | :--- |
| `--color-cream` | `#F2EADF` | Paper-cream ground for dossier panels, flyer-style card surfaces, and warm overlays. |
| `--color-charcoal` | `#22201C` | Ink/charcoal text color for high-legibility editorial copy over cream grounds. |
| `--color-jade` | `#2E8B67` | Jade/sea-green primary accent for links, highlights, and dither fields. |
| `--color-sea-glass` | `#8FD0D8` | Sea-glass blue secondary accent for chromatic aberration and cool balance. |
| `--color-umber` | `#73553B` | Umber for warm borders, tonal grounding, and shadow depth. |

### From Dither Matter

| Token Name | Hex / Value | Role & Usage |
| :--- | :--- | :--- |
| `--color-vermilion` | `#E4572E` | Vermilion primary CTA/interactive accent and glitch flares. |
| `--color-ultramarine` | `#3D5A80` | Ultramarine edge accent for chromatic aberration and shader splits. |
| `--color-acid-green` | `#C6FA00` | Acid green high-energy accent for alerts and kinetic moments. |
| `--color-lilac-pink` | `#E5A9D6` | Lilac-pink secondary accent for hover feedback and soft states. |
| `--dither-marbled` | WebGL/Canvas | Marbled dither grain (Dither Matter) for ambient background texture. |
| `--dither-cellular` | WebGL/Canvas | Cellular halftone dither (Dither Matter) for grid/grain background textures. |

### Surface Tokens (Dark Mode)

| Token Name | Hex / Value | Role & Usage |
| :--- | :--- | :--- |
| `--bg-void` | `#000000` | Primary viewport background; evokes a dark club/warehouse space. |
| `--bg-surface-modal` | `#0D0D0D` – `#141414` | Artist Profile modal background and cookie notice container. |
| `--bg-surface-glass` | `rgba(0, 0, 0, 0.85)` | Backdrop overlay when an artist modal or info drawer is active. |
| `--text-primary` | `#FFFFFF` | Primary typography, artist names, active headings, and white brand emblem. |
| `--text-secondary` | `#8E8E8E` / `rgba(255,255,255,0.55)` | Technical field labels (`ARTIST / ALIAS`, `AGENT(S)`, `SET TYPE`, `BASED IN`, `TYPE`) and inactive roster items on sibling hover. |
| `--border-hairline` | `rgba(255, 255, 255, 0.18)` | 1px technical grid dividers inside the Artist Profile modal and HUD chrome. |

---

## 4. Typography System

The typographic hierarchy relies on the tension between an experimental, boxy technical monospace font and a classic neo-grotesque sans-serif for extended reading.

### Font Families
1. **Display & Technical HUD Font (`--font-mono`)**: **`Cygnito Mono`**
   * **Characteristics**: Futuristic, industrial monospace with sharp geometric cuts and rave-flyer attitude.
   * **Usage**: Artist roster list (`<h1>`), modal headers, metadata category labels, navigation links, and buttons.
2. **Body & Editorial Font (`--font-sans`)**: **`Helvetica Neue`** *(Fallbacks: `Helvetica, Arial, sans-serif`)*
   * **Characteristics**: Neutral, high-legibility neo-grotesque sans-serif.
   * **Usage**: Artist biography paragraphs (`<p>`), legal/privacy policy text, and cookie banner copy.

### Typographic Scale & Rules

| Element | Font Family | Case & Tracking | Style Notes |
| :--- | :--- | :--- | :--- |
| **Roster Artist Title (`h1`)** | `Cygnito Mono` | `UPPERCASE`, `tracking: 0.04em` | Large interactive display links in the main vertical roster; shifts opacity/color on hover. |
| **Modal Artist Name (`h1`)** | `Cygnito Mono` | `UPPERCASE`, `tracking: 0.03em` | Prominent header inside the open Artist Profile card. |
| **Metadata Value (`h2`)** | `Helvetica Neue` / `Cygnito Mono` | `Title Case` or `UPPERCASE` | Displays agent names (`Fabio Venezia`), set type (`DJ`), city (`Berlin, DE`), and contract scope (`Mgmt + Bookings`). |
| **HUD / Field Labels** | `Cygnito Mono` | `UPPERCASE`, `tracking: 0.10em` | `10px–12px` micro-labels (`ARTIST PROFILE`, `CLOSE`, `ARTIST / ALIAS`, `BASED IN`). |
| **Biography Copy (`p`)** | `Helvetica Neue` | `Sentence case`, `line-height: 1.5` | `14px–16px` clean body copy summarizing the artist's sound and career milestones. |

---

## 5. Layout & Spatial Architecture

The site uses a **Fixed Perimeter HUD + Scrollable Roster + Center Modal** architecture:

```
+-----------------------------------------------------------------------+
| [ARTIST BOOKINGS & MANAGEMENT]   [LOGO: H Y P E R  D R E A M S] [Shop 2025 ©] | <-- Fixed Top Banner
+-----------------------------------------------------------------------+
|                                                                       |
|  ANTONYM                         +---------------------------------+  |
|  CASSIE RAPTOR                   | ARTIST PROFILE            CLOSE |  |
|  CLTX                            +---------------------------------+  |
|  DBBD                            | ARTIST / ALIAS   |              |  |
|  DJ AYA                          | CLTX             |   [Artist    |  |
|  DJ GUESTLIST                    |------------------|    Portrait  |  |
|  DJ SEXSTASY                     | Agent: F.Venezia |    Image]    |  |
|  DJ SPORTSCHUH                   | Based: Berlin,DE |              |  |
|  EARGASM GOD                     +------------------+--------------+  |
|  ...                             | Biography paragraph text...     |  |
|  (Left/Main Artist Roster)       | [Book Artist]     [SC][IG][TK]  |  |
|                                  +---------------------------------+  |
|                                    (Centered Artist Modal Overlay)    |
|                                                                       |
| [Cookie Notice Banner: By closing this notice you agree...]       [X] |
+-----------------------------------------------------------------------+
```

### 1. Top Perimeter Banner (`role="banner"`)
* **Left Zone**: Static agency descriptor (`ARTIST BOOKINGS & MANAGEMENT`).
* **Center Zone**: Brand link featuring the white chrome/tribal emblem (`WHITE-HD-WEB.png`, `125px` width) and a split-character wordmark:
  ```html
  <div>
    <span>H</span><span>Y</span><span>P</span><span>E</span><span>R</span>
    <span> </span>
    <span>D</span><span>R</span><span>E</span><span>A</span><span>M</span><span>S</span>
  </div>
  ```
* **Right Zone**: External link to `Shop` (`https://shop.hyper-dreams.com`, `target="_blank"`) paired with the copyright stamp (`2025 ©`).

### 2. Ambient Centerpiece & Interactive Canvas
* Beneath the UI sits a centered `450px` non-draggable Hyper Dreams emblem (`WHITE-HD-WEB.png`) layered with a WebGL/Canvas glitch and dither background that responds to pointer coordinates and navigation state.

### 3. Artist Roster List (`role="list"`)
* A vertical list of `role="listitem"` nodes representing each artist on the roster (`ANTONYM`, `CASSIE RAPTOR`, `CLTX`, `DBBD`, `DJ AYA`, `DJ GUESTLIST`, `DJ SEXSTASY`, `DJ SPORTSCHUH`, `EARGASM GOD`, etc.).
* Each list item contains the trigger `<h1>` and its corresponding pre-rendered modal DOM tree for instant zero-latency opening.

---

## 6. Component Specifications

### Component A: Artist Profile Modal (Dossier Pop-up)
Triggered upon clicking any artist `<h1>` in the roster. Centered in the viewport with a structured, industrial specification-sheet layout:

1. **Modal Top Bar**:
   * Left: `ARTIST PROFILE` label
   * Right: `CLOSE` interactive dismiss button
2. **Upper Split Grid (Metadata + Portrait)**:
   * **Left Column (Technical Metadata)**:
     * `ARTIST / ALIAS`: `<h1>{Artist Name}</h1>`
     * **Row 1**:
       * `Agent(S)`: `<h2>{Agent 1}</h2>` `<h2>{Agent 2 (Optional)}</h2>`
       * `SET Type`: `<h2>{DJ / Live}</h2>`
     * **Row 2**:
       * `BASED IN`: `<h2>{City, Country ISO}</h2>` (e.g., `Berlin, DE`, `Paris, FR`, `Warsaw, PL`)
       * `TYPE`: `<h2>{Bookings | Mgmt + Bookings}</h2>`
   * **Right Column (Visual Portrait)**:
     * High-contrast press photograph (`<img>` with `loading="lazy"`), framed tightly within the grid cell.
3. **Lower Content Block (Biography & Actions)**:
   * **Bio**: 2–4 sentence summary (`<p>`) detailing the artist's sonic style, notable venues (e.g., Boiler Room, Panorama Bar, KitKat Club, RSO), and cultural impact.
   * **Action Bar**:
     * **Primary CTA**: `Book Artist` (or `Request`) button wired to `mailto:{agent}@hyper-dreams.com`.
     * **Social Links Bar**: Up to 4 icon links (`<i>` inside `<a target="_blank">`) connecting to **SoundCloud**, **Instagram**, **TikTok**, and **Bandcamp**. Empty CMS links are hidden automatically.

### Component B: Cookie Consent Micro-Bar
* Positioned unobtrusively at the edge of the viewport.
* Copy: *"By closing this notice you agree to the use of Cookies, as well as our Privacy Policy."* (links to `/privacy-policy`).
* Dismissed via a minimal icon button (`<i></i>`).

---

## 7. Motion, Micro-Interactions & Special Effects

1. **Intro Loader Sequence**:
   * On initial load, a choreographed intro animation scales and reveals the central Hyper Dreams emblem with a glitch/dither transition before fading in the perimeter HUD and artist roster.
2. **Split-Character Logo Hover**:
   * Hovering the top-center `HYPER DREAMS` link triggers a staggered character animation across the individual `<span>` nodes.
3. **Roster Hover Sensitivity & Dither Feedback**:
   * Hovering over an artist's `<h1>` highlights the active artist while dimming non-hovered siblings, accompanied by gritty dithered visual feedback in the background canvas.
4. **Liquid / Glitch Distortion ("About Us" & Background)**:
   * Interacting with the background canvas or toggling the agency info state applies a fluid, organic warp/distortion shader (`WebGL`) to the visuals, reinforcing the psychedelic/hyper-digital brand name.

---

## 8. Responsive Adaptation (Desktop vs. Mobile)

* **Desktop (`> 991px`)**:
  * Locked, app-like viewport feel.
  * Left-aligned interactive roster with ample negative space in the center/right for the 450px watermark and WebGL glitch canvas.
  * Artist Profile modal opens as a centered floating dossier window over the canvas.
* **Tablet & Mobile (`<= 991px`)**:
  * Top HUD condenses; the split-character text under the logo hides or scales down to preserve horizontal space.
  * Artist Roster becomes a touch-friendly vertical scroll list.
  * Artist Profile Modal converts into a full-viewport scrollable overlay, stacking the metadata grid above the artist portrait, biography text, and full-width `Book Artist` button.