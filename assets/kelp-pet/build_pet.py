"""Rebuild Kelp's original vector artwork and transparent animation exports.
Run: python -m pip install Pillow cairosvg; python assets/kelp-pet/build_pet.py
Art reference: the owner-supplied Kelp state sheet. No source image is embedded.
"""
import io
import json
import math
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
STATES = {
    'idle': ('#b5e7f7', '#80cbe5', 'Ready when you are', 'Gentle float and blink'),
    'working': ('#a9dfbd', '#70b88d', 'Working', 'Steady bounce and orbiting dots'),
    'waiting': ('#ffc8ab', '#e99c78', 'Approval needed', 'Slow breath and amber pulse'),
    'done': ('#b5e7f7', '#80cbe5', 'Task completed', 'Happy landing and sparkles'),
    'offline': ('#eeeeec', '#c9ccca', 'Disconnected', 'Settled float and muted eyes'),
    'curious': ('#b5e7f7', '#80cbe5', 'Listening', 'Head tilt and questioning glance'),
    'happy': ('#b5e7f7', '#80cbe5', 'Happy', 'Soft side-to-side smile'),
    'excited': ('#b5e7f7', '#80cbe5', 'Good news', 'Two energetic hops and cheering eyes'),
    'thinking': ('#b5e7f7', '#80cbe5', 'Thinking', 'Looking side to side with thinking dots'),
    'surprised': ('#b5e7f7', '#80cbe5', 'Something new', 'Quick lift and wide eyes'),
}
FRAMES, MS = 64, 40


def svg(state, t=0):
    light, dark, _, _ = STATES[state]
    a = 2 * math.pi * t
    bob, tilt, sx, sy = -3 * math.sin(a), 0, 1, 1
    eye_x, eye_y = 0, 0
    extras = ''
    if state == 'working':
        bob = -4 * (1 - math.cos(2 * a)) / 2
        for i in range(3):
            q = a + i * 2 * math.pi / 3
            x, y = 128 + 95 * math.cos(q), 124 + 34 * math.sin(q)
            extras += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#4d9f72" opacity=".55"/>'
    elif state == 'waiting':
        sx = sy = 1 + .018 * math.sin(a)
        extras = f'<ellipse cx="128" cy="214" rx="{69 + 5 * math.sin(a):.2f}" ry="15" fill="none" stroke="#e5a17c" stroke-width="2" opacity="{.2 + .16 * (1 + math.sin(a)):.2f}"/>'
    elif state == 'offline':
        bob = -math.sin(a)
    elif state == 'curious':
        tilt = -9 * math.sin(a)
        eye_x, eye_y = 3 * math.sin(a), -2
        extras = '<text x="207" y="66" font-family="sans-serif" font-size="24" font-weight="600" fill="#2c5967">?</text>'
    elif state == 'happy':
        tilt = 5 * math.sin(a)
        bob = -4 * math.sin(a) ** 2
    elif state in ('done', 'excited'):
        hop = max(0, math.sin(a if state == 'done' else 2 * a))
        bob = - (13 if state == 'done' else 20) * hop
        sx, sy = 1 - .025 * hop, 1 + .035 * hop
        for i, (x, y) in enumerate(((40, 87), (215, 74), (205, 158))):
            opacity = max(0, math.sin(a + i * .6))
            extras += f'<path d="M{x-5} {y}h10 M{x} {y-5}v10" stroke="#65adc2" stroke-width="2.5" stroke-linecap="round" opacity="{opacity:.2f}"/>'
    elif state == 'thinking':
        eye_x = 5 * math.sin(a)
        for i in range(3):
            opacity = .22 + .65 * max(0, math.sin(a - i * .7))
            extras += f'<circle cx="{109+i*19}" cy="37" r="3.7" fill="#28515b" opacity="{opacity:.2f}"/>'
    elif state == 'surprised':
        pulse = max(0, math.sin(a)) ** 5
        bob, sx, sy = -10 * pulse, 1 - .035 * pulse, 1 + .06 * pulse
        extras = f'<path d="M33 116l-10 -4 M35 129l-12 1 M218 89l8 -8 M224 102l11 -3" stroke="#28515b" stroke-width="3" stroke-linecap="round" opacity="{.25+.65*pulse:.2f}"/>'
    # Brief smooth blink; keep the resting poster open-eyed.
    blink = 1 - .93 * max(0, 1 - abs(t - .72) / .055)
    if state in ('happy', 'done'):
        eyes = '<path d="M96 127q10 -22 20 0 M145 127q10 -22 20 0" fill="none" stroke="#102d32" stroke-width="7" stroke-linecap="round"/>'
    elif state == 'excited':
        eyes = '<path d="M99 112l15 10 -15 10 M160 112l-15 10 15 10" fill="none" stroke="#102d32" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    else:
        h = 43 if state == 'surprised' else 37
        opacity = '.56' if state == 'offline' else '1'
        eyes = f'<g opacity="{opacity}" transform="translate({eye_x:.2f} {eye_y:.2f}) translate(0 124) scale(1 {blink:.3f}) translate(0 -124)">'
        for x in (96, 143):
            eyes += f'<rect x="{x}" y="{124-h/2}" width="18" height="{h}" rx="9" fill="url(#eye)"/>'
            if state not in ('thinking', 'surprised', 'offline'):
                eyes += f'<circle cx="{x+9}" cy="{124+h/2-7}" r="1.6" fill="#f7ffff" opacity=".95"/>'
        eyes += '</g>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="384" height="384" viewBox="0 0 256 256" role="img" aria-label="Kelp {state}">
<defs><radialGradient id="body" cx="33%" cy="25%" r="80%"><stop stop-color="{light}"/><stop offset=".64" stop-color="{light}"/><stop offset="1" stop-color="{dark}"/></radialGradient><linearGradient id="eye" x2="0" y2="1"><stop stop-color="#0b2528"/><stop offset="1" stop-color="#132c2e"/></linearGradient><radialGradient id="shadow"><stop stop-color="#365c61" stop-opacity=".24"/><stop offset="1" stop-color="#365c61" stop-opacity="0"/></radialGradient></defs>
<ellipse cx="128" cy="218" rx="{76+bob*.5:.2f}" ry="16" fill="url(#shadow)"/>{extras}
<g transform="translate(128 {128+bob:.2f}) rotate({tilt:.2f}) scale({sx:.4f} {sy:.4f}) translate(-128 -128)"><circle cx="128" cy="126" r="78" fill="url(#body)"/><path d="M63 85a77 77 0 0 1 113 -19" fill="none" stroke="white" stroke-opacity=".32" stroke-width="1.2" stroke-linecap="round"/>{eyes}</g></svg>'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {'version': 1, 'defaultState': 'idle', 'frameWidth': 256, 'frameHeight': 256,
                'columns': 8, 'rows': 8, 'frameCount': FRAMES, 'frameDurationMs': MS,
                'durationMs': FRAMES * MS, 'loop': True, 'alpha': True, 'states': {}}
    for state, (_, _, label, motion) in STATES.items():
        (OUT / f'{state}.svg').write_text(svg(state), encoding='utf-8')
        frames = [Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg(state, i/FRAMES).encode()))).convert('RGBA') for i in range(FRAMES)]
        frames[0].save(OUT / f'{state}.webp', save_all=True, append_images=frames[1:], duration=MS, loop=0, lossless=True, method=6)
        sheet = Image.new('RGBA', (2048, 2048))
        for i, frame in enumerate(frames):
            sheet.paste(frame.resize((256, 256), Image.Resampling.LANCZOS), ((i % 8)*256, (i // 8)*256))
        sheet.save(OUT / f'{state}.png', optimize=True)
        manifest['states'][state] = {'label': label, 'motion': motion, 'animation': f'{state}.webp',
                                      'poster': f'{state}.svg', 'sprite': f'{state}.png'}
        print(state)
    (OUT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sheets = [Image.open(OUT / f'{state}.png') for state in STATES]
    gallery = []
    font = ImageFont.load_default(size=16)
    for frame in range(FRAMES):
        canvas = Image.new('RGB', (1000, 480), '#f4f5f0')
        draw = ImageDraw.Draw(canvas)
        for i, (state, sheet) in enumerate(zip(STATES, sheets)):
            x, y = (frame % 8) * 256, (frame // 8) * 256
            cell = sheet.crop((x, y, x + 256, y + 256)).resize((200, 200), Image.Resampling.LANCZOS)
            canvas.paste(cell, ((i % 5) * 200, (i // 5) * 240), cell)
            draw.text(((i % 5) * 200 + 100, (i // 5) * 240 + 205), state.capitalize(),
                      font=font, fill='#244b44', anchor='mt')
        gallery.append(canvas)
    gallery[0].save(OUT / 'preview.gif', save_all=True, append_images=gallery[1:],
                    duration=MS, loop=0, optimize=True)



if __name__ == '__main__':
    main()
