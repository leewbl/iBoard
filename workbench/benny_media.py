"""Collect and inspect the attachments named by one frozen Benny report."""

import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from model import glm_vision
from runner import sandbox
from workbench import cli
from workbench.benny_slack import SlackError


MAX_FILES = 8
MAX_LOG = 256_000
MAX_VIDEO = 20_000_000
MAX_EXCERPT = 4_800
MAX_TOTAL = 32_000_000
TEXT = {'text/plain', 'text/csv', 'application/json', 'application/x-ndjson', 'text/markdown'}
IMAGE = {'image/png', 'image/jpeg', 'image/webp'}


def _remaining(deadline, cap=30):
    value = min(cap, int(deadline - time.monotonic()))
    if value < 1:
        raise ValueError('attachment inspection exceeded the deadline')
    return value


def _save(path, content):
    path.write_bytes(content)
    return {'path': path.name, 'bytes': len(content), 'sha256': cli.digest(path)}


def _log(data):
    text = data.decode('utf-8-sig')
    if not text.strip() or '\0' in text:
        raise ValueError('attachment is not a nonempty UTF-8 log or text file')
    if len(text) <= MAX_EXCERPT:
        return [text], True
    half = MAX_EXCERPT // 2
    head, tail = text[:half], text[-half:]
    return [head.rpartition('\n')[0] or head, tail.partition('\n')[2] or tail], False


def _image(data, expected):
    media, extension = glm_vision.image_type(data)
    if media != expected:
        raise ValueError('screenshot bytes do not match its Slack media type')
    if media == 'image/png':
        sandbox._validated_png(data)
    return extension


def _video_frames(path, deadline):
    ffprobe, ffmpeg = shutil.which('ffprobe'), shutil.which('ffmpeg')
    if not ffprobe or not ffmpeg:
        raise ValueError('FFmpeg and FFprobe are required for MP4 sampling')
    common = ['-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', 'mov', '-enable_drefs', '0']
    try:
        probe = subprocess.run([ffprobe, *common, '-show_entries', 'format=duration:stream=codec_type,width,height',
                                '-of', 'json', str(path)], timeout=_remaining(deadline, 12), capture_output=True, check=True)
        metadata = json.loads(probe.stdout)
        duration = float(metadata['format']['duration'])
        stream = next(row for row in metadata['streams'] if row['codec_type'] == 'video')
        if (not math.isfinite(duration) or not 0.25 <= duration <= 90
                or not 1 <= stream['width'] <= 3840 or not 1 <= stream['height'] <= 2160):
            raise ValueError('MP4 is outside the supported duration or dimensions')
        for timecode in (0.1, 0.5, 0.85):
            offset = round(duration * timecode, 3)
            decoded = subprocess.run([ffmpeg, '-nostdin', *common, '-threads', '1', '-ss', str(offset), '-i', str(path),
                                      '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1', '-vf', 'scale=min(1200\\,iw):-2',
                                      '-c:v', 'png', '-threads', '1', '-f', 'image2pipe', 'pipe:1'],
                                     timeout=_remaining(deadline, 12), capture_output=True, check=True).stdout
            _image(decoded, 'image/png')
            yield offset, decoded
    except (OSError, subprocess.SubprocessError, StopIteration, KeyError, TypeError) as error:
        raise ValueError('could not sample the attached MP4') from error


def _describe(data, vision, deadline):
    note = vision(data, _remaining(deadline))
    if not isinstance(note, str) or not note.strip() or len(note) > 2500 or '\0' in note:
        raise ValueError('media observation is empty or exceeds its evidence limit')
    return note.strip()


def ids(snapshot):
    occurrences = {}
    for message in snapshot['messages']:
        for file_id in message['files']:
            timestamps = occurrences.setdefault(file_id, [])
            if message['ts'] not in timestamps:
                timestamps.append(message['ts'])
    return occurrences


def collect(slack, snapshot, workspace, out, deadline, *, download=False, inspect_media=False, vision=glm_vision.describe):
    attachments, excerpts, vendor_failed = [], {}, False
    occurrences = ids(snapshot)
    budget = MAX_TOTAL

    def fetch(item, limit):
        nonlocal budget
        data = slack.download(item, workspace, deadline, min(limit, budget))
        budget -= len(data)
        return data

    if occurrences and (download or inspect_media):
        out.mkdir(mode=0o700)
    for index, (file_id, timestamps) in enumerate(occurrences.items(), 1):
        rec = {'id': file_id, 'messages': timestamps, 'status': 'unread'}
        attachments.append(rec)
        if vendor_failed or index > MAX_FILES or not (download or inspect_media):
            rec['reason'] = ('media provider stopped' if vendor_failed else 'attachment count limit' if index > MAX_FILES
                             else 'attachment reading was not selected')
            continue
        try:
            item = slack.file_info(file_id, deadline)
            media = item['mimetype'].lower()
            rec['media_type'] = media
            if media in TEXT:
                data = fetch(item, MAX_LOG)
                parts, complete = _log(data)
                rec['file'] = _save(out/f'{file_id}.txt', data)
                for n, part in enumerate(parts, 1):
                    excerpts[f'A{index}.{n}'] = {'kind': 'attachment-text', 'text': part, 'file_id': file_id,
                                                 'sha256': rec['file']['sha256'], 'excerpt': not complete}
                rec['status'] = 'read' if complete else 'excerpted'
            elif media in IMAGE:
                data = fetch(item, glm_vision.MAX_IMAGE_BYTES)
                suffix = _image(data, media)
                rec['file'] = _save(out/f'{file_id}.{suffix}', data)
                rec['status'] = 'downloaded'
                if inspect_media:
                    note = _describe(data, vision, deadline)
                    excerpts[f'V{index}'] = {'kind': 'media-observation', 'text': note, 'file_id': file_id,
                                            'sha256': rec['file']['sha256'], 'scope': 'screenshot'}
                    rec['status'] = 'read'
            elif media == 'video/mp4':
                data = fetch(item, MAX_VIDEO)
                if len(data) < 12 or data[4:8] != b'ftyp':
                    raise ValueError('unsupported MP4 container header')
                path = out/f'{file_id}.mp4'
                rec['file'] = _save(path, data)
                rec['status'] = 'downloaded'
                if inspect_media:
                    rec['frames'] = []
                    for frame, (offset, content) in enumerate(_video_frames(path, deadline), 1):
                        saved = _save(out/f'{file_id}-{frame}.png', content)
                        rec['frames'].append({**saved, 'offset_seconds': offset})
                        note = _describe(content, vision, deadline)
                        excerpts[f'V{index}.{frame}'] = {'kind': 'media-observation', 'text': note, 'file_id': file_id,
                                                        'sha256': saved['sha256'], 'offset_seconds': offset,
                                                        'scope': 'video-frame'}
                    rec['status'] = 'sampled'
            else:
                rec['reason'] = 'unsupported attachment format'
        except RuntimeError:
            rec['reason'] = 'configured media model failed; further model calls were withheld'
            vendor_failed = True
        except (SlackError, OSError, ValueError, UnicodeError, sandbox.SandboxError) as error:
            rec['reason'] = str(error)[:220]
    return attachments, excerpts, vendor_failed


def unchanged(out, attachments):
    for item in attachments:
        files = [item['file']] if 'file' in item else []
        for retained in [*files, *item.get('frames', ())]:
            path = out/retained['path']
            if (path.is_symlink() or not path.is_file() or path.stat().st_size != retained['bytes']
                    or cli.digest(path) != retained['sha256']):
                return False
    return True
