/*
 * Generates the Kelp PWA icons without any image dependency.
 *
 * Draws a dark rounded square with three wavy kelp strands in kelp green,
 * then encodes RGBA pixels as PNG (zlib from the Node standard library).
 *
 *   node scripts/make-icons.mjs
 *
 * Outputs public/icons/icon-{192,512}.png (any) and
 * public/icons/icon-maskable-{192,512}.png (full-bleed, motif in the
 * 80% safe zone).
 */
import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const outDir = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "icons");

// Palette (matches the app's dark theme + kelp-green accent).
const BG = [10, 14, 12, 255]; // near-black with a green cast
const STRAND = [62, 207, 142, 255]; // #3ecf8e
const STRAND_DIM = [38, 142, 99, 255];

const crcTable = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (const byte of bytes) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

function encodePng(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0; // filter: none
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Signed distance to a rounded rectangle; <= 0 means inside. */
function roundedRectSdf(px, py, cx, cy, halfW, halfH, radius) {
  const dx = Math.abs(px - cx) - (halfW - radius);
  const dy = Math.abs(py - cy) - (halfH - radius);
  const ox = Math.max(dx, 0);
  const oy = Math.max(dy, 0);
  return Math.hypot(ox, oy) + Math.min(Math.max(dx, dy), 0) - radius;
}

/** Distance from point (px,py) to a vertical sine strand. */
function strandDistance(px, py, size, strand) {
  const { baseX, amplitude, wavelength, phase, topY, thickness } = strand;
  const y0 = Math.max(0, Math.floor(py - thickness * 2));
  const y1 = Math.min(size - 1, Math.ceil(py + thickness * 2));
  let best = Infinity;
  for (let y = y0; y <= y1; y++) {
    if (y < topY) continue;
    const cx = baseX + amplitude * Math.sin((y / size) * wavelength + phase);
    best = Math.min(best, Math.hypot(px - cx, y - py));
  }
  // Rounded cap at the top end of the strand.
  const capX = baseX + amplitude * Math.sin((topY / size) * wavelength + phase);
  best = Math.min(best, Math.hypot(px - capX, py - topY));
  return best;
}

function makeStrands(size, inset) {
  // Three strands rising from the bottom, staggered heights, in the safe zone.
  const usable = size - inset * 2;
  const mk = (i, count, heightFrac, ampFrac) => {
    const baseX = inset + (usable * (i + 0.5)) / count;
    return {
      baseX,
      amplitude: usable * ampFrac,
      wavelength: Math.PI * 2 * (2.2 + i * 0.35),
      phase: i * 1.9,
      topY: size - inset - usable * heightFrac,
      thickness: size * 0.035,
    };
  };
  return [mk(0, 3, 0.52, 0.055), mk(1, 3, 0.72, 0.065), mk(2, 3, 0.4, 0.05)];
}

function drawIcon(size, { maskable }) {
  const rgba = Buffer.alloc(size * size * 4);
  const inset = maskable ? size * 0.22 : size * 0.08; // maskable safe zone
  const radius = maskable ? 0 : size * 0.22;
  const strands = makeStrands(size, inset);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const px = x + 0.5;
      const py = y + 0.5;
      let color = [0, 0, 0, 0];
      if (maskable) {
        color = BG;
      } else {
        const d = roundedRectSdf(px, py, size / 2, size / 2, size / 2, size / 2, radius);
        if (d <= 0) color = BG;
      }
      if (color[3] !== 0) {
        for (let i = 0; i < strands.length; i++) {
          const dist = strandDistance(px, py, size, strands[i]);
          const half = strands[i].thickness;
          if (dist <= half) {
            const edge = Math.min(1, (half - dist) / Math.max(1, size * 0.008));
            const tint = i === 1 ? STRAND : STRAND_DIM;
            color = [
              Math.round(tint[0] * edge + color[0] * (1 - edge)),
              Math.round(tint[1] * edge + color[1] * (1 - edge)),
              Math.round(tint[2] * edge + color[2] * (1 - edge)),
              255,
            ];
          }
        }
      }
      const offset = (y * size + x) * 4;
      rgba[offset] = color[0];
      rgba[offset + 1] = color[1];
      rgba[offset + 2] = color[2];
      rgba[offset + 3] = color[3];
    }
  }
  return encodePng(size, size, rgba);
}

mkdirSync(outDir, { recursive: true });
for (const size of [192, 512]) {
  writeFileSync(join(outDir, `icon-${size}.png`), drawIcon(size, { maskable: false }));
  writeFileSync(join(outDir, `icon-maskable-${size}.png`), drawIcon(size, { maskable: true }));
  console.log(`wrote icon-${size}.png and icon-maskable-${size}.png`);
}
