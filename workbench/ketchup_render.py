"""Render a silent evidence slideshow and match its decoded frames to the slides."""

import base64
from collections import Counter
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryFile
from threading import Timer

from workbench.automate_me import _require


WIDTH, HEIGHT, FPS, STRIP = 1280, 720, 24, 92
MATCH_WIDTH, MATCH_HEIGHT = 160, 90
TRANSITION = 8
CJK = r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]'
WORDS = re.compile(CJK + r"|[^\W_]+(?:['’.-][^\W_]+)*", re.UNICODE)
STYLE = '''
*{box-sizing:border-box}
html,body{margin:0;width:1280px;height:720px;overflow:hidden}
body{font:25px/1.35 system-ui,-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;
color:#16253b;background:#f4f7fb}
.content{height:628px;padding:38px 64px 25px;display:flex;flex-direction:column;gap:22px}
h1{font-size:42px;line-height:1.2;margin:0;font-weight:720;letter-spacing:-.02em}
.scene{position:relative;display:flex;align-items:center;justify-content:center;min-height:155px;
border:1px solid #dbe5f0;background:white;border-radius:17px;overflow:hidden}
svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.scene>div{position:absolute;display:flex;align-items:center;justify-content:center;
padding:10px 12px;text-align:center;border-radius:11px;background:#edf3fb;border:1px solid #d4e0f0;
color:#25476b;font-size:23px;font-weight:620;min-height:64px;width:155px}
.scene>.focus{background:#14499b;color:white;border-color:#14499b;box-shadow:0 7px 19px #14499b44}
.normal .scene{height:288px;flex-shrink:0}
.support{flex:1;min-height:0;display:flex;gap:26px;justify-content:center;align-items:center}
.value{display:inline-flex;flex-direction:column;padding:9px 26px 13px;background:#e7effd;
border-radius:14px;font-size:25px;color:#274d78;max-width:820px;text-align:center;align-self:center}
.number{font-size:53px;line-height:1.08;font-weight:720;color:#14499b;white-space:nowrap}
.support .media{margin:0;max-width:100%;height:100%;display:flex;flex:1;min-width:0;
flex-direction:column;gap:6px;align-items:center}
.media img{min-height:0;height:100%;max-width:100%;object-fit:contain;border-radius:8px}
.media figcaption{font-size:17px;color:#274d78;text-align:center;white-space:nowrap}
pre{width:100%;margin:0;align-self:center;padding:18px 24px;border-radius:12px;background:#ecf2fa;
font:23px/1.45 ui-monospace,"Menlo",monospace}
.strip{height:92px;width:100%;display:flex;align-items:center;justify-content:center;
padding:0 62px;background:#11243e;color:white;border-top:3px solid #4080d8;font-size:27px}
.strip span,[data-oneline]{white-space:nowrap}
'''


def words(value):
    # Count CJK characters conservatively and Latin words as words.
    return WORDS.findall(re.sub(r'(' + CJK + r')', r' \1 ', value.casefold()))


def _labels(story, slide, evidence):
    labels = [node['label'] for node in story['nodes']]
    for item in slide.get('media', []):
        labels.append(item['label'])
    if 'number' in slide:
        item = slide['number']
        labels.extend((item['label'], str(evidence[item['id']]['facts'][item['field']])))
    if 'excerpt' in slide:
        labels.append(evidence[slide['excerpt']]['excerpt'])
    labels.append(slide['title'])
    return Counter(token for phrase in labels for token in words(phrase))


def schedule(story, evidence):
    slots, prior, offset = [], Counter(), 0
    for index, slide in enumerate(story['slides']):
        labels = _labels(story, slide, evidence)
        added = len(words(slide['subtitle'])) + sum((labels - prior).values())
        frames = max(3 * FPS, math.ceil(added * FPS / 3))
        slots.append({'slide': index + 1, 'start_frame': offset, 'frames': frames, 'words_added': added})
        offset += frames + TRANSITION
        prior = labels
    return slots


def _diagram(story, slide):
    nodes = story['nodes']
    positions = {node['id']: (index + .5) * 1152 / len(nodes) for index, node in enumerate(nodes)}
    paths = []
    for origin, target in story['links']:
        source, end = positions[origin], positions[target]
        direction = 1 if end > source else -1
        start_x, end_x = source + 78 * direction, end - 82 * direction
        if abs(positions[origin] - positions[target]) > 1.5 * (1152 / len(nodes)):
            a, b = (f'{source:.1f} 48', f'{end:.1f} 48')
            curve = f'M{a} C{source:.1f} 2,{end:.1f} 2,{b}'
        else:
            curve = f'M{start_x:.1f} 80 H{end_x:.1f}'
        color = '#245aa3' if slide['focus'] == target else '#88a5c7'
        paths.append(f'<path d="{curve}" fill="none" stroke="{color}" stroke-width="3" marker-end="url(#arrow)"/>')
    diagram = ('<svg viewBox="0 0 1152 160" preserveAspectRatio="none" aria-hidden="true">'
               '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
               'orient="auto"><path fill="#5379a8" d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
               + ''.join(paths) + '</svg>')
    nodes_html = []
    for node in nodes:
        css = 'focus' if node['id'] == slide['focus'] else ''
        top = positions[node['id']] / 1152 * 100
        nodes_html.append(f'<div class="{css}" style="left:calc({top:.4f}% - 77.5px)">{escape(node["label"])}</div>')
    return diagram + ''.join(nodes_html)


def _data_image(path):
    return 'data:image/png;base64,' + base64.b64encode(Path(path).read_bytes()).decode('ascii')


def _page(story, slide, evidence, images):
    qualification = 'normal'
    visual = ''
    if slide.get('media'):
        qualification = 'media-scene'
        visual = ''.join(f'<figure class="media"><img src="{_data_image(images[item["id"]])}" alt="">'
                         f'<figcaption data-oneline>{escape(item["label"])}</figcaption></figure>'
                         for item in slide['media'])
    elif 'number' in slide:
        fact = slide['number']
        value = escape(str(evidence[fact['id']]['facts'][fact['field']]))
        visual = f'<div class="value"><div class="number" data-oneline>{value}</div>{escape(fact["label"])}</div>'
    elif 'excerpt' in slide:
        visual = f'<pre data-block>{escape(evidence[slide["excerpt"]]["excerpt"])}</pre>'
    return ('<!doctype html><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; '
            'img-src data:; style-src &apos;unsafe-inline&apos;; script-src &apos;unsafe-inline&apos;">'
            f'<style>{STYLE}</style><body><main class="content {qualification}"><h1 data-oneline>{escape(slide["title"])}</h1>'
            f'<section class="scene">{_diagram(story, slide)}</section><section class="support">{visual}</section></main>'
            f'<footer class="strip"><span data-oneline>{escape(slide["subtitle"])}</span></footer>'
            '<script>const fit = [...document.querySelectorAll("[data-oneline],[data-block]")].every(e => '
            'e.scrollWidth <= e.clientWidth + 2 && e.getBoundingClientRect().right <= 1218 && '
            'e.getBoundingClientRect().bottom <= 719);document.body.dataset.layout=(fit?"fit":"overflow");</script>')


def _call(command, remaining, **options):
    return subprocess.run(command, capture_output=True, check=True, timeout=remaining(), **options).stdout


def _chrome():
    name = next((shutil.which(item) for item in ('chromium', 'google-chrome', 'chromium-browser')
                 if shutil.which(item)), None)
    app = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    return name or (str(app) if app.is_file() else None)


def _still(chrome, path, output, profile, remaining):
    result = _call([chrome, '--headless', '--disable-gpu', '--disable-background-networking',
                    '--disable-component-update', '--no-first-run', '--no-default-browser-check',
                    '--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1280,720',
                    '--host-resolver-rules=MAP * ~NOTFOUND', f'--user-data-dir={profile}',
                    f'--screenshot={output}', '--dump-dom', path.as_uri()], remaining)
    _require(b'data-layout="fit"' in result and output.is_file(), 'slide text overflowed or Chromium did not render it')


def _raw(ffmpeg, path, remaining, *, match=False, once=True):
    width, height = (MATCH_WIDTH, MATCH_HEIGHT) if match else (WIDTH, HEIGHT)
    colors = 'gray' if match else 'rgb24'
    frames = ['-frames:v', '1'] if once else []
    return _call([ffmpeg, '-v', 'error', '-i', str(path), '-vf', f'scale={width}:{height}:flags=area',
                  *frames, '-f', 'rawvideo', '-pix_fmt', colors, '-threads', '1', '-'], remaining)


def _motion(before, after, step):
    stride = WIDTH * 3
    cut = round(WIDTH * step / (TRANSITION + 1)) * 3
    return b''.join(before[row + cut:row + stride] + after[row:row + cut]
                    for row in range(0, len(before), stride))


def _movie(ffmpeg, stills, slots, target, remaining):
    command = [ffmpeg, '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{WIDTH}x{HEIGHT}',
               '-framerate', str(FPS), '-i', '-', '-an', '-c:v', 'libx264', '-threads', '2',
               '-preset', 'fast', '-crf', '15', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
               '-n', str(target)]
    with TemporaryFile() as errors, subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors) as process:
        timer = Timer(remaining(), process.kill)
        timer.start()
        try:
            stream = process.stdin
            for index, (still, slot) in enumerate(zip(stills, slots)):
                for _ in range(slot['frames']):
                    remaining()
                    stream.write(still)
                if index + 1 < len(stills):
                    for step in range(1, TRANSITION + 1):
                        stream.write(_motion(still, stills[index + 1], step))
            stream.close()
            status = process.wait(timeout=remaining())
            errors.seek(0)
            _require(status == 0, 'FFmpeg encoding failed: ' + errors.read(600).decode(errors='replace'))
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()


def _delta(left, right, cut):
    body = sum(abs(a - b) for a, b in zip(left[:cut], right[:cut])) / cut
    caption = sum(abs(a - b) for a, b in zip(left[cut:], right[cut:])) / (len(left) - cut)
    return body, caption


def audit(video, stills, slots, ffmpeg, ffprobe, remaining):
    raw = json.loads(_call([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(video)], remaining))
    streams = raw['streams']
    _require(len(streams) == 1 and streams[0]['codec_type'] == 'video'
             and (streams[0]['width'], streams[0]['height'], streams[0]['r_frame_rate']) == (WIDTH, HEIGHT, f'{FPS}/1'),
             'video dimensions, speed, or silence differ from the design')
    decoded = _raw(ffmpeg, video, remaining, match=True, once=False)
    size = MATCH_WIDTH * MATCH_HEIGHT
    total = slots[-1]['start_frame'] + slots[-1]['frames']
    _require(len(decoded) == total * size, 'rendered frame count differs from the storyboard')
    samples = [_raw(ffmpeg, path, remaining, match=True) for path in stills]
    cut = round((HEIGHT - STRIP) / HEIGHT * MATCH_HEIGHT) * MATCH_WIDTH
    reports = []
    for index, slot in enumerate(slots):
        start, end = slot['start_frame'], slot['start_frame'] + slot['frames']
        errors = [_delta(decoded[frame*size:(frame+1)*size], samples[index], cut) for frame in range(start, end)]
        verified = sum(body < 2.75 and strip < 3.25 for body, strip in errors)
        _require(verified == slot['frames'] and verified * 3 >= slot['words_added'] * FPS,
                 f'slide {index + 1} did not hold its own rendered art and subtitle long enough')
        if index + 1 < len(slots):
            position = end + TRANSITION // 2
            mid = decoded[position*size:(position+1)*size]
            _require(all(sum(_delta(mid, still, cut)) > 2 for still in samples[index:index+2]),
                     'inter-slide motion was not visible in the rendered video')
        reports.append({**slot, 'matching_frames': verified, 'seconds': verified / FPS,
                        'worst_body_delta': round(max(item[0] for item in errors), 3),
                        'worst_subtitle_delta': round(max(item[1] for item in errors), 3)})
    return {'width': WIDTH, 'height': HEIGHT, 'fps': FPS, 'duration': total / FPS, 'frames': total,
            'audio_streams': 0, 'transitions': len(slots) - 1, 'subtitle_strip_height': STRIP, 'holds': reports}


def render(story, evidence, images, out, remaining, *, browser_profile, chrome=None, ffmpeg=None, ffprobe=None):
    chrome, ffmpeg, ffprobe = chrome or _chrome(), ffmpeg or shutil.which('ffmpeg'), ffprobe or shutil.which('ffprobe')
    _require(chrome and ffmpeg and ffprobe, 'install Chromium, FFmpeg, and FFprobe before rendering')
    slots = schedule(story, evidence)
    names = []
    for slot, slide in zip(slots, story['slides']):
        stem = out / f'slide-{slot["slide"]:02d}'
        source, image = stem.with_suffix('.html'), stem.with_suffix('.png')
        source.write_text(_page(story, slide, evidence, images), encoding='utf-8')
        _still(chrome, source, image, browser_profile, remaining)
        names.append(image)
    frames = [_raw(ffmpeg, image, remaining, match=False) for image in names]
    _require(all(len(item) == WIDTH*HEIGHT*3 for item in frames), 'slide source has unexpected dimensions')
    video = out / 'ketchup.mp4'
    _movie(ffmpeg, frames, slots, video, remaining)
    proof = audit(video, names, slots, ffmpeg, ffprobe, remaining)
    return video, names, proof
