"""Offline job, metadata, publication package and fail-closed upload ledger.

No command uploads media, contacts YouTube, or stores credentials. Evidence must
come from the real downloader, media review, and authenticated upload surface.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

from media_hash_cache import MediaHashCache

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'workspace'
JST = timezone(timedelta(hours=9))
CHECKS = ('decode', 'speech_boundaries', 'captions', 'privacy_blur', 'story_flow', 'audio_mix')
# Archive remains a supported legacy kind; newly created jobs never request it.
KINDS = ('short', 'topic', 'digest', 'archive')
DEFAULT_KINDS = ('short', 'topic', 'digest')
DELIVERY_POLICY = 'clips_only_v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never silently remove a stale lock: another upload may still be in progress.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, f'{os.getpid()} {now()}'.encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def media_cache(j, args=None):
    cache = MediaHashCache(j['root'], enabled=not getattr(args, 'no_hash_cache', False),
                           force=getattr(args, 'force_rehash', False))
    if args is not None:
        args._hash_cache = cache
    return cache


def delivery_scope(j, kinds):
    """Keep new jobs clips-only; do not migrate or disable legacy archive jobs."""
    policy = j.get('delivery_policy')
    require(policy in (None, DELIVERY_POLICY), 'Unknown delivery policy')
    if policy == DELIVERY_POLICY:
        require(all(kind in DEFAULT_KINDS for kind in kinds),
                'New jobs allow short, topic and digest only; FullArchive is retired')


def inside(root, value, exists=True):
    root = Path(root).resolve()
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    require(path.is_relative_to(root), f'Path must stay inside job: {path}')
    if exists:
        require(path.is_file(), f'Missing file: {path}')
    return path


def vod_id(url):
    p = urlsplit(url.strip())
    require(p.scheme == 'https' and p.hostname in ('twitch.tv', 'www.twitch.tv', 'm.twitch.tv')
            and p.username is None and p.password is None and p.port in (None, 443),
            'Use a genuine HTTPS Twitch /videos/<numeric ID> URL.')
    m = re.fullmatch(r'/videos/([1-9][0-9]*)/?', p.path)
    require(m is not None, 'A full VOD URL is required; clips/live channel URLs are different inputs.')
    return m.group(1)


def job_at(path):
    j = read(path)
    require(j.get('schema') == 1, 'Unknown job schema')
    root = Path(j['root']).resolve()
    for key in ('work', 'source_dir', 'delivery'):
        require(Path(j[key]).resolve().is_relative_to(root), 'Job paths escaped workspace')
    return j


def init(args):
    ident = vod_id(args.url)
    root = Path(args.root).resolve()
    profile = args.profile if isinstance(args.profile, dict) else read(args.profile)
    if 'profile' in profile:
        profile = profile['profile']
    require(re.fullmatch(r'[a-z0-9_]{1,25}', profile.get('twitch_login', '')),
            'Configure the recipient Twitch login before creating a job')
    profile.setdefault('verified_related_links', [])
    profile.setdefault('youtube_channel_id', '')
    profile.setdefault('youtube_channel_url', '')
    profile.setdefault('twitch_url', 'https://www.twitch.tv/' + profile['twitch_login'])
    key = 'twitch_' + ident
    work = root / 'editing' / 'archives' / key
    path = work / 'job.json'
    with locked(work / 'job.lock'):
        if path.exists():
            j = job_at(path)
            require(j['vod_id'] == ident and Path(j['root']).resolve() == root, 'Existing job identity mismatch')
            require(j['profile']['twitch_login'] == profile['twitch_login'], 'Existing job belongs to another configured Twitch login')
            return {'job': str(path), 'reused': True}
        j = {'schema': 1, 'vod_id': ident, 'archive_id': key,
             'source_url': f'https://www.twitch.tv/videos/{ident}', 'input_url': args.url,
             'root': str(root), 'work': str(work), 'source_dir': str(root / 'VOD' / key),
             'delivery': str(root / 'Archives' / key), 'created_at': now(),
             'profile': profile, 'state': 'initialized',
             'delivery_policy': DELIVERY_POLICY, 'expected_kinds': list(DEFAULT_KINDS)}
        for directory in (j['source_dir'], work / 'logs', work / 'review',
                          Path(j['delivery']) / 'Shorts', Path(j['delivery']) / 'Topics',
                          Path(j['delivery']) / 'Digest'):
            Path(directory).mkdir(parents=True, exist_ok=True)
        write(path, j)
    return {'job': str(path), 'reused': False}


def metadata(args):
    j = job_at(args.job)
    lines = Path(args.raw).read_text(encoding='utf-8-sig').splitlines()
    first = next((line for line in lines if line.strip()), '')
    v = (json.loads(first).get('data') or {}).get('video')
    require(isinstance(v, dict), 'VOD metadata is null or malformed; do not continue with a deleted VOD.')
    # TD 1.56.5 Raw VideoInfo does not contain the requested video ID.
    # Bind to the recorded CLI request now and to chat.video.id at prepare.
    require(args.requested_id == j['vod_id'], 'Metadata requested VOD ID mismatch')
    require(v.get('id') is None or str(v['id']) == j['vod_id'], 'Metadata VOD ID mismatch')
    owner = v.get('owner') or {}
    length = float(v.get('lengthSeconds', 0))
    require(owner.get('login') and v.get('title') and math.isfinite(length) and length > 0,
            'Missing owner/title/positive source duration')
    dt = datetime.fromisoformat(v['createdAt'].replace('Z', '+00:00'))
    require(dt.tzinfo is not None, 'Source time needs timezone')
    j['metadata'] = {'title': v['title'], 'created_at_utc': dt.astimezone(timezone.utc).isoformat(),
                     'created_at_jst': dt.astimezone(JST).isoformat(),
                     'duration_seconds': float(v['lengthSeconds']), 'owner_login': owner['login'],
                     'owner_name': owner.get('displayName') or owner['login'],
                     'category': (v.get('game') or {}).get('displayName') or (v.get('game') or {}).get('name'),
                     'requested_vod_id': args.requested_id,
                     'raw_path': str(Path(args.raw).resolve()), 'raw_sha256': sha(args.raw)}
    j['state'] = 'metadata_saved'
    with locked(Path(j['work']) / 'job.lock'):
        write(args.job, j)
    return {'metadata': j['metadata'], 'owner_matches_profile': owner['login'].lower() == j['profile']['twitch_login'].lower()}


def timestamp(seconds):
    n = int(seconds)
    return f'{n // 3600}:{n % 3600 // 60:02d}:{n % 60:02d}' if n >= 3600 else f'{n // 60:02d}:{n % 60:02d}'


def adopt(args):
    """Attach a positively identified legacy archive, without moving its files."""
    j = job_at(args.job)
    cache = media_cache(j, args)
    root = Path(j['root'])
    config_path = inside(root / 'editing' / 'archives', args.config)
    c = read(config_path)
    video = inside(root, c['source'])
    chat_path = inside(root, c['chat'])
    require(video.is_relative_to((root / 'VOD').resolve()) and chat_path.is_relative_to((root / 'VOD').resolve()),
            'Legacy source must be inside workspace VOD')
    delivery = inside(root, c['delivery'], exists=False)
    legacy_work = inside(root, c['work'], exists=False)
    require(delivery.is_dir() and delivery.is_relative_to((root / 'Archives').resolve()), 'Invalid legacy delivery')
    require(legacy_work.is_dir() and legacy_work.is_relative_to((root / 'editing').resolve()), 'Invalid legacy work')
    chat = read(chat_path)
    cv = chat.get('video') or {}
    owner = chat.get('streamer') or {}
    require(str(cv.get('id')) == j['vod_id'], 'Legacy chat belongs to a different VOD')
    require(cv.get('title') and owner.get('login') and isinstance(chat.get('comments'), list), 'Invalid legacy chat metadata')
    duration = float(cv.get('length', 0))
    require(math.isfinite(duration) and duration > 0, 'Legacy source duration missing; inspect original metadata')
    created = datetime.fromisoformat(cv['created_at'].replace('Z', '+00:00'))
    require(created.tzinfo is not None, 'Legacy source time needs timezone')
    require(not (Path(j['work']) / 'publication.json').exists(), 'Package already prepared; do not switch sources mid-job')
    lp = ledger_path(j)
    with locked(lp.with_suffix('.lock')):
        require(not any(v['vod_id'] == j['vod_id'] for v in ledger_read(lp)['uploads'].values()),
                'Upload history exists; reconcile before changing job locations')
        with locked(Path(j['work']) / 'job.lock'):
            j.update({'source_dir': str(video.parent), 'delivery': str(delivery), 'legacy_work': str(legacy_work),
                      'adopted_source': str(video), 'adopted_chat': str(chat_path),
                      'adopted_config': str(config_path), 'state': 'adopted_needs_review'})
            j['metadata'] = {'title': cv['title'], 'created_at_utc': created.astimezone(timezone.utc).isoformat(),
                             'created_at_jst': created.astimezone(JST).isoformat(), 'duration_seconds': duration,
                             'owner_login': owner['login'], 'owner_name': owner.get('name') or owner['login'],
                             'category': cv.get('game'), 'requested_vod_id': j['vod_id'],
                             'provenance': 'Existing TwitchDownloader chat metadata, not a fresh info request',
                             'chat_path': str(chat_path), 'chat_sha256': cache.hash(chat_path)}
            write(args.job, j)
    return {'job': str(Path(args.job).resolve()), 'delivery': str(delivery), 'state': j['state'],
            'files_moved': False, 'qa_imported': False, 'upload_status_inferred': False}


def text_ok(value, field, limit, byte_limit=False):
    require(isinstance(value, str) and value.strip(), f'{field} is empty')
    require(not any(c in value for c in '<>\x00'), f'{field} contains unsupported characters')
    require((len(value.encode('utf-8')) if byte_limit else len(value)) <= limit, f'{field} exceeds {limit}')


def media_info(path, ffprobe, upload_limits=True):
    raw = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                         check=True, capture_output=True, text=True, encoding='utf-8')
    p = json.loads(raw.stdout)
    video = next((s for s in p['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in p['streams'] if s['codec_type'] == 'audio'), None)
    require(video and audio, 'Both video and audio streams are required')
    duration = float(p['format']['duration'])
    require(math.isfinite(duration) and duration > 0, 'Invalid media duration')
    if upload_limits:
        require(duration <= 43200, 'Above YouTube 12-hour limit; do not trim or partition without user authorization')
        require(path.stat().st_size <= 256 * 1024 ** 3, 'Above YouTube file-size limit')
    return {'duration_seconds': duration, 'width': video['width'], 'height': video['height'],
            'video_codec': video['codec_name'], 'audio_codec': audio['codec_name'], 'bytes': path.stat().st_size,
            'frame_rate': video.get('r_frame_rate'), 'sample_rate': audio.get('sample_rate')}


def archive_title(j, item):
    """Keep the source title intact; a date suffix and authorized part label are added."""
    suffix = datetime.fromisoformat(j['metadata']['created_at_jst']).strftime('【%Y.%m.%d】')
    part = item.get('archive_part')
    label = ''
    if part:
        index, count = part.get('index'), part.get('count')
        require(type(index) is int and type(count) is int and 1 <= index <= count and count > 1,
                'Archive part needs a valid integer index and count')
        label = ('【前編】' if index == 1 else '【後編】') if count == 2 else f'【第{index}部】'
    return j['metadata']['title'] + label + suffix


def archive_timeline(j, item, src, info):
    """Validate declared continuity; real frame/audio evidence is separately mandatory QA."""
    path = inside(j['delivery'], item.get('archive_timeline', ''))
    value = read(path)
    duration = float(src['duration_seconds'])
    require(value.get('source_sha256') == src['video']['sha256'], 'Archive timeline source fingerprint mismatch')
    require(abs(float(value.get('source_duration_seconds', -1)) - duration) <= 0.001,
            'Archive timeline source duration mismatch')
    segments = value.get('segments')
    require(isinstance(segments, list) and len(segments) == 1,
            'Archive is one uninterrupted original source interval per file')
    segment = segments[0]
    start, end = float(segment['source_start']), float(segment['source_end'])
    require(math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + 0.001,
            'Invalid archive source interval')
    require(segment.get('speed') == 1 and float(segment.get('output_start', -1)) == 0,
            'Archive must keep original speed and start without an intro')
    require(abs(float(segment['output_end']) - info['duration_seconds']) <= 0.001,
            'Archive timeline output duration does not match actual media')
    require(abs(info['duration_seconds'] - (end - start)) <= 0.12,
            'Archive duration changed: no cuts, gaps, speed changes or outro')
    require(value.get('insertions') == [] and value.get('frames_preserved') is True
            and value.get('audio_preserved') is True,
            'Archive requires unchanged frame order and original audio, with no inserted content')
    require(not item.get('thumbnail'), 'Full archives do not use custom thumbnails')
    if not item.get('archive_part'):
        require(start == 0 and abs(end - duration) <= 0.001, 'Full archive must cover the entire source')
    return {'path': str(path), 'sha256': sha(path), 'source_start': start, 'source_end': end,
            'source_sha256': src['video']['sha256'], 'source_duration_seconds': duration}


def check_archive_partition(j, items, plan, source_duration):
    """No source omission/overlap; splitting is exceptional and explicitly authorized."""
    if not items:
        return None
    partial = [a for a in items if a.get('archive_part')]
    if not partial:
        require(len(items) == 1, 'Only one full archive is allowed per publication plan')
        return None
    require(len(partial) == len(items), 'Do not mix a full archive and partitioned archives')
    approval = plan.get('archive_partition') or {}
    require(approval.get('approved') is True, 'Archive partition requires explicit user authorization')
    evidence = inside(j['work'], approval.get('evidence', ''))
    require(evidence.stat().st_size > 0, 'Archive partition authorization evidence is empty')
    consent = read(evidence)
    require(consent.get('vod_id') == j['vod_id'] and consent.get('authorized_by') == 'user'
            and consent.get('scope') == 'continuous_archive_partition'
            and isinstance(consent.get('statement'), str) and consent['statement'].strip(),
            'Archive partition authorization must identify this VOD and the direct user instruction')
    ordered = sorted(items, key=lambda a: a['archive_part']['index'])
    count = len(ordered)
    require([a['archive_part']['index'] for a in ordered] == list(range(1, count + 1))
            and all(a['archive_part']['count'] == count for a in ordered), 'Archive parts are incomplete or duplicated')
    boundary = 0.0
    for a in ordered:
        timeline = a['archive_timeline']
        require(abs(timeline['source_start'] - boundary) <= 0.001, 'Archive partition contains a gap or overlap')
        boundary = timeline['source_end']
    require(abs(boundary - source_duration) <= 0.001, 'Archive partition must cover the full original source')
    return {'approved': True, 'evidence': str(evidence), 'evidence_sha256': sha(evidence)}


def publication_scope(j, plan, assets):
    """Track the requested sets; a partially prepared package is not a finished workflow."""
    expected = j.get('expected_kinds')
    scope = plan.get('authorized_scope')
    scope_result = None
    if scope:
        expected = scope.get('kinds')
        require(scope.get('authority') == 'user', 'Partial publication scope needs a direct user instruction')
        evidence = inside(j['work'], scope.get('evidence', ''))
        consent = read(evidence)
        require(consent.get('vod_id') == j['vod_id'] and consent.get('authorized_by') == 'user'
                and consent.get('scope') == 'publication_kinds' and consent.get('kinds') == expected
                and isinstance(consent.get('statement'), str) and consent['statement'].strip(),
                'Partial scope evidence must identify this VOD, the kinds and the direct user instruction')
        scope_result = {'kinds': expected, 'authority': 'user', 'evidence': str(evidence),
                        'evidence_sha256': sha(evidence)}
    if expected is not None:
        require(isinstance(expected, list) and expected and len(expected) == len(set(expected))
                and all(k in KINDS for k in expected), 'Invalid expected delivery kinds')
        delivery_scope(j, expected)
    delivery_scope(j, [a['kind'] for a in assets])
    if scope:
        require(all(a['kind'] in expected for a in assets), 'An asset is outside the user-requested publication scope')
    omitted = []
    for entry in plan.get('omitted_kinds', []):
        kind = entry.get('kind')
        require(kind in ('short', 'topic') and kind not in [a['kind'] for a in assets]
                and kind not in [o['kind'] for o in omitted], 'Only absent Shorts/Topics can have a zero-result reason')
        require(expected is None or kind in expected, 'Omitted kind is outside the expected scope')
        require(isinstance(entry.get('reason'), str) and entry['reason'].strip(), 'Explain the actual zero-result decision')
        evidence = inside(j['work'], entry.get('evidence', ''))
        report = read(evidence)
        require(report.get('vod_id') == j['vod_id'] and report.get('kind') == kind,
                'Zero-result evidence must identify this VOD and delivery kind')
        omitted.append({'kind': kind, 'reason': entry['reason'], 'evidence': str(evidence),
                        'evidence_sha256': sha(evidence)})
    return {'expected_kinds': expected, 'authorized_scope': scope_result, 'omitted_kinds': omitted}


def description(j, item, duration, related):
    m = j['metadata']
    d = datetime.fromisoformat(m['created_at_jst']).strftime('%Y年%m月%d日 %H:%M（日本時間）')
    require(isinstance(item.get('summary'), str) and item['summary'].strip(), 'Write a scene-specific summary')
    chunks = [item['summary'].strip(), f"元配信タイトル：{m['title']}\n配信日時：{d}\n配信者：{m['owner_name']}\n元配信の長さ：{timestamp(m['duration_seconds'])}"]
    if m.get('category'):
        chunks[-1] += '\nカテゴリ：' + m['category']
    chunks.append(f"Twitchで配信中！\n{j['profile']['twitch_url']}\n\n元配信アーカイブ\n{j['source_url']}\n\nアーカイブチャンネル\n{j['profile']['youtube_channel_url']}")
    for link in related:
        require(link.get('label') and link.get('evidence'), 'Related links need a label and verification evidence')
        u = urlsplit(link['url'])
        require(u.scheme == 'https' and u.netloc and u.username is None and u.password is None, 'Invalid related URL')
        chunks.append(link['label'] + '\n' + link['url'])
    chapters = item.get('chapters', [])
    if item['kind'] == 'digest' or chapters:
        require(len(chapters) >= 3 and chapters[0]['seconds'] == 0,
                'YouTube chapters need at least 3 entries beginning at 0; topic chapters may be omitted')
        starts = [float(c['seconds']) for c in chapters]
        require(all(math.isfinite(t) for t in starts), 'Invalid chapter time')
        require(all(b - a >= 10 for a, b in zip(starts, starts[1:] + [duration])), 'Chapters must be ordered, inside video, at least 10 seconds')
        require(all(isinstance(c.get('title'), str) and c['title'].strip() for c in chapters), 'Chapter title missing')
        chunks.append('目次\n' + '\n'.join(timestamp(c['seconds']) + ' ' + c['title'] for c in chapters))
    if item.get('credits'):
        chunks.append(item['credits'].strip())
    chunks.append('#配信アーカイブ' if item['kind'] == 'archive'
                  else '#切り抜き' + (' #Shorts' if item['kind'] == 'short' else ''))
    result = '\n\n'.join(chunks)
    text_ok(result, 'description (UTF-8 bytes)', 5000, True)
    return result


def check_review(j, item, media_hash):
    review_path = inside(j['work'], item['review'])
    r = read(review_path)
    require(r.get('media_sha256') == media_hash, 'QA is stale: final media hash changed')
    required = CHECKS + (('full_timeline',) if item['kind'] == 'archive' else ())
    required += (('thumbnail',) if item['kind'] in ('topic', 'digest') or item.get('thumbnail') else ())
    for check in required:
        entry = r.get('checks', {}).get(check, {})
        require(entry.get('passed') is True, f'QA incomplete: {check}')
        evidence = inside(j['work'], entry.get('evidence', ''))
        require(evidence.stat().st_size > 0, f'Empty QA evidence: {check}')
    if item.get('thumbnail'):
        thumb = inside(j['delivery'], item['thumbnail'])
        require(r.get('thumbnail_sha256') == sha(thumb), 'Thumbnail QA is stale')
    return str(review_path)


def prepare(args):
    j = job_at(args.job)
    cache = media_cache(j, args)
    require('metadata' in j, 'Import source metadata first')
    require(re.fullmatch(r'UC[A-Za-z0-9_-]{22}', j['profile'].get('youtube_channel_id', '')),
            'Set and verify a recipient YouTube channel ID before preparing an upload')
    require(j['metadata']['owner_login'].lower() == j['profile']['twitch_login'].lower(),
            'Source owner differs from profile. Resolve intent and credits; do not label it as the user stream.')
    plan = read(args.plan)
    # Reject retired output kinds before probing or hashing any large media.
    delivery_scope(j, [a['kind'] for a in plan.get('assets', [])])
    delivery_scope(j, (plan.get('authorized_scope') or {}).get('kinds', []))
    source_review = inside(j['work'], plan['source_review'])
    src = read(source_review)
    require(src.get('vod_id') == j['vod_id'], 'Source review VOD mismatch')
    for name in ('video', 'chat'):
        file = inside(Path(j['root']) / 'VOD', src[name]['path'])
        require(cache.hash(file) == src[name]['sha256'], f'Source {name} changed or unverified')
    mode = src.get('acquisition_mode', 'fresh_download')
    require(mode in ('fresh_download', 'legacy_revalidated'), 'Unknown source acquisition mode')
    if mode == 'legacy_revalidated':
        require(j.get('adopted_config'), 'Legacy revalidation requires an identified adopted archive')
    source_checks = ('duration_and_streams', 'chat_identity_and_range', 'decode')
    source_checks += ('provenance', 'timeline_integrity') if mode == 'legacy_revalidated' else ('download_logs',)
    for check in source_checks:
        evidence = src.get('checks', {}).get(check, {})
        require(evidence.get('passed') is True, f'Source check incomplete: {check}')
        require(inside(j['work'], evidence.get('evidence', '')).stat().st_size > 0, f'Missing source evidence: {check}')
    actual_duration = float(src['duration_seconds'])
    require(math.isfinite(actual_duration) and actual_duration > 0, 'Invalid verified source duration')
    chat = read(inside(Path(j['root']) / 'VOD', src['chat']['path']))
    require(str(chat.get('video', {}).get('id')) == j['vod_id'], 'Actual chat VOD ID mismatch')
    require(chat.get('streamer', {}).get('login', '').lower() == j['metadata']['owner_login'].lower(), 'Actual chat owner mismatch')
    require(isinstance(chat.get('comments'), list), 'Chat comments must be an array')
    require(float(chat['video'].get('start', -1)) == 0 and float(chat['video'].get('end', 0)) >= actual_duration - 2,
            'Chat export does not cover full source range')
    for c in chat['comments']:
        t = float(c['content_offset_seconds'])
        require(math.isfinite(t) and 0 <= t <= actual_duration + 2, 'Chat timestamp outside source')
        require(c.get('content_id') is None or str(c['content_id']) == j['vod_id'], 'Comment belongs to a different VOD')
    j['metadata']['duration_seconds'] = actual_duration
    assets = plan.get('assets', [])
    require(assets, 'No assets in plan')
    require(len({a['id'] for a in assets}) == len(assets), 'Asset IDs must be unique')
    results = []
    for a in assets:
        require(re.fullmatch(r'[a-zA-Z0-9_-]+', a['id']), 'Invalid stable asset ID')
        require(a['kind'] in KINDS, 'kind must be short, topic, digest or archive')
        path = inside(j['delivery'], a['file'])
        require(path.suffix.lower() == '.mp4', 'Publish MP4 outputs only')
        info = media_info(path, args.ffprobe)
        require(info['video_codec'] == 'h264' and info['audio_codec'] == 'aac', 'Expected H.264/AAC delivery')
        if a['kind'] == 'short':
            require((info['width'], info['height']) == (1080, 1920) and info['duration_seconds'] <= 180,
                    'Short must be 1080x1920 and within 3 minutes; do not truncate speech to satisfy this')
        elif a['kind'] != 'archive':
            require((info['width'], info['height']) == (1920, 1080), 'Topic and digest must be 1920x1080')
        if a['kind'] == 'topic':
            require(path.is_relative_to((Path(j['delivery']) / 'Topics').resolve()),
                    'Topic media must stay in the archive Topics directory')
        if a['kind'] == 'archive':
            require(path.is_relative_to((Path(j['delivery']) / 'FullArchive').resolve()),
                    'Archive media must stay in the archive FullArchive directory')
            original = media_info(inside(Path(j['root']) / 'VOD', src['video']['path']), args.ffprobe,
                                  upload_limits=False)
            require((info['width'], info['height']) == (original['width'], original['height'])
                    and Fraction(info['frame_rate']) == Fraction(original['frame_rate'])
                    and info['sample_rate'] == original['sample_rate'],
                    'Full archive must preserve source dimensions and cadence')
        digest = cache.hash(path)
        review = check_review(j, a, digest)
        title = a['title'].strip()
        text_ok(title, 'title', 100)
        require('\n' not in title and '\r' not in title, 'Title must be a single line')
        suffix = datetime.fromisoformat(j['metadata']['created_at_jst']).strftime('【%Y.%m.%d】')
        require(title.endswith(suffix), 'Title must end with the actual stream date in JST: ' + suffix)
        if a['kind'] == 'archive':
            require(title == archive_title(j, a), 'Archive title must be verbatim source title plus part/date suffix')
        else:
            require(re.match(r'^\s*(?:\d{1,3}[_＿]|\d{1,3}[.．、]\s|\d{1,3}\s*[)）:：]\s)', title) is None,
                    'Remove a leading management serial number from the public title')
        thumb = inside(j['delivery'], a['thumbnail']) if a.get('thumbnail') else None
        require(a['kind'] not in ('topic', 'digest') or thumb, 'Topic and digest thumbnail required')
        require(a['kind'] not in ('short', 'archive') or not thumb, 'Shorts and full archives do not use custom thumbnails')
        if thumb:
            require(thumb.suffix.lower() in ('.jpg', '.jpeg', '.png'), 'Use a JPG or PNG thumbnail')
        desc = description(j, a, info['duration_seconds'], plan.get('verified_related_links', j['profile']['verified_related_links']))
        results.append({'id': a['id'], 'kind': a['kind'], 'file': str(path), 'sha256': digest,
                        'media': info, 'review': review, 'title': title, 'description': desc,
                        'thumbnail': str(thumb) if thumb else None, 'thumbnail_sha256': sha(thumb) if thumb else None,
                        'privacy_status': 'private', 'publish_at': None,
                        'expected_channel_id': j['profile']['youtube_channel_id']})
        if a['kind'] == 'archive':
            results[-1]['archive_timeline'] = archive_timeline(j, a, src, info)
            if a.get('archive_part'):
                results[-1]['archive_part'] = a['archive_part']
    require(len({a['sha256'] for a in results}) == len(results), 'Duplicate media bytes in plan')
    partition = check_archive_partition(j, [a for a in results if a['kind'] == 'archive'], plan, actual_duration)
    scope = publication_scope(j, plan, results)
    for a in results:
        a['package_signature'] = package_signature(a)
    output = Path(j['work']) / 'publication.json'
    with locked(Path(j['work']) / 'job.lock'):
        write(output, {'schema': 1, 'vod_id': j['vod_id'], 'prepared_at': now(),
                       'archive_partition': partition,
                       **scope,
                       'source_review': str(source_review), 'source_review_sha256': sha(source_review), 'assets': results})
        for a in results:
            d = Path(j['delivery']) / '投稿資料'
            d.mkdir(exist_ok=True)
            (d / (a['id'] + '_概要欄.txt')).write_text(a['description'], encoding='utf-8')
            (d / (a['id'] + '_タイトル.txt')).write_text(a['title'], encoding='utf-8')
    return {'publication': str(output), 'count': len(results), 'uploaded': False}


def asset_at(j, ident, verify_source=True, hash_cache=None):
    cache = hash_cache if hash_cache is not None else media_cache(j)
    package = read(Path(j['work']) / 'publication.json')
    require(package['vod_id'] == j['vod_id'], 'Package VOD mismatch')
    if verify_source:
        check_package_source(j, package, cache)
    a = next((v for v in package['assets'] if v['id'] == ident), None)
    require(a is not None, 'Unknown asset ID')
    delivery_scope(j, [a['kind']])
    require(a['expected_channel_id'] == j['profile']['youtube_channel_id'], 'Package target mismatch')
    require(a['privacy_status'] == 'private' and not a.get('publish_at'), 'Nonprivate package rejected')
    require(cache.hash(inside(j['delivery'], a['file'])) == a['sha256'], 'Media changed since prepare')
    if a.get('thumbnail'):
        require(sha(inside(j['delivery'], a['thumbnail'])) == a['thumbnail_sha256'], 'Thumbnail changed since prepare')
    if a['kind'] == 'archive':
        timeline = a['archive_timeline']
        require(sha(inside(j['delivery'], timeline['path'])) == timeline['sha256'], 'Archive timeline changed since prepare')
    check_review(j, a, a['sha256'])
    return a


def check_package_source(j, package, hash_cache=None):
    cache = hash_cache if hash_cache is not None else media_cache(j)
    delivery_scope(j, [a['kind'] for a in package.get('assets', [])])
    delivery_scope(j, package.get('expected_kinds') or [])
    review = inside(j['work'], package['source_review'])
    require(sha(review) == package['source_review_sha256'], 'Source review changed since prepare')
    src = read(review)
    scope = package.get('authorized_scope')
    evidence_records = ([scope] if scope else []) + package.get('omitted_kinds', [])
    for entry in evidence_records:
        require(sha(inside(j['work'], entry['evidence'])) == entry['evidence_sha256'],
                'Publication scope or zero-result evidence changed since prepare')
    partition = package.get('archive_partition')
    if partition:
        require(partition.get('approved') is True and
                sha(inside(j['work'], partition['evidence'])) == partition['evidence_sha256'],
                'Archive partition authorization changed since prepare')
    for name in ('video', 'chat'):
        require(cache.hash(inside(Path(j['root']) / 'VOD', src[name]['path'])) == src[name]['sha256'],
                f'Source {name} changed since prepare')


def ledger_path(j):
    return Path(j['root']) / 'editing' / 'publishing' / 'upload-ledger.json'


def ledger_read(path):
    return read(path) if path.exists() else {'schema': 1, 'uploads': {}}


def key_for(j, a):
    return j['profile']['youtube_channel_id'] + ':' + j['vod_id'] + ':' + a['id']


def package_signature(a):
    fields = ('sha256', 'title', 'description', 'thumbnail_sha256', 'expected_channel_id', 'privacy_status', 'publish_at')
    content = {k: a[k] for k in fields}
    if a.get('kind') == 'archive':
        content['archive_timeline'] = a['archive_timeline']
        content['archive_part'] = a.get('archive_part')
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def begin(args):
    j = job_at(args.job)
    cache = media_cache(j, args)
    a = asset_at(j, args.asset, hash_cache=cache)
    require(args.channel == j['profile']['youtube_channel_id'], 'Authenticated channel mismatch; nothing sent')
    evidence = inside(j['work'], args.evidence)
    require(evidence.stat().st_size > 0, 'Channel verification evidence missing')
    path = ledger_path(j)
    with locked(path.with_suffix('.lock')):
        ledger = ledger_read(path)
        key = key_for(j, a)
        old = ledger['uploads'].get(key)
        require(old is None or old['state'] == 'confirmed_absent',
                'Existing upload intent/result. Reconcile it; do not send again or change IDs to bypass.')
        require(not any(v['sha256'] == a['sha256'] and v['channel_id'] == args.channel
                        and v['state'] != 'confirmed_absent' for v in ledger['uploads'].values()),
                'Identical media already has an upload intent/result')
        entry = {'vod_id': j['vod_id'], 'asset_id': a['id'], 'kind': a['kind'],
                 'sha256': a['sha256'], 'file': a['file'],
                 'channel_id': args.channel, 'state': 'in_progress', 'started_at': now(),
                 'title': a['title'], 'description': a['description'], 'thumbnail_sha256': a['thumbnail_sha256'],
                 'package_signature': package_signature(a),
                 'channel_evidence': str(evidence), 'history': old.get('history', []) if old else []}
        if old:
            entry['history'].append({k: v for k, v in old.items() if k != 'history'})
        ledger['uploads'][key] = entry
        write(path, ledger)
    return {'ledger_key': key, 'state': 'in_progress', 'uploaded': False}


def record(args):
    j = job_at(args.job)
    cache = media_cache(j, args)
    package = read(Path(j['work']) / 'publication.json')
    require(package['vod_id'] == j['vod_id'], 'Package VOD mismatch')
    a = next((v for v in package['assets'] if v['id'] == args.asset), None)
    require(a is not None, 'Unknown asset ID')
    local_error = None
    try:
        asset_at(j, args.asset, hash_cache=cache)
    except (ValueError, OSError, KeyError, TypeError) as e:
        # Preserve a real remote ID even when local media/evidence changed while
        # uploading. It must remain needs_review, never silently sent again.
        local_error = str(e)
    receipt_path = inside(j['work'], args.receipt)
    r = read(receipt_path)
    require(re.fullmatch(r'[A-Za-z0-9_-]{11}', str(r.get('video_id', ''))), 'Actual YouTube video ID required')
    evidence = inside(j['work'], r['evidence'])
    require(evidence.stat().st_size > 0 and r.get('observed_at'), 'Readback evidence/time required')
    path = ledger_path(j)
    with locked(path.with_suffix('.lock')):
        ledger = ledger_read(path)
        old = ledger['uploads'].get(key_for(j, a))
        require(old is not None, 'No matching upload intent')
        require(not old.get('video_id') or old['video_id'] == r['video_id'], 'Different video ID; duplicate needs reconciliation')
        require(not any(k != key_for(j, a) and v.get('video_id') == r['video_id']
                        for k, v in ledger['uploads'].items()), 'This remote video ID belongs to another asset')
        observed = datetime.fromisoformat(r['observed_at'].replace('Z', '+00:00'))
        require(observed.tzinfo is not None, 'Readback time needs timezone')
        fresh = (datetime.fromisoformat(old['started_at']) <= observed <= datetime.now(timezone.utc) + timedelta(minutes=5))
        good = (r.get('channel_id') == old['channel_id'] and r.get('privacy_status') == 'private'
                and not r.get('publish_at') and r.get('title') == a['title']
                and r.get('description') == a['description'] and fresh
                and r.get('package_signature') == package_signature(a)
                and local_error is None and old['sha256'] == a['sha256'])
        complete = (good and r.get('processing_status') == 'succeeded' and r.get('checks_reviewed') is True
                    and r.get('playback_reviewed') is True
                    and (not a['thumbnail_sha256'] or r.get('thumbnail_verified') is True))
        old.update({'video_id': r['video_id'], 'url': 'https://www.youtube.com/watch?v=' + r['video_id'],
                    'state': 'verified_private' if complete else 'needs_review', 'receipt': str(receipt_path),
                    'package_signature': package_signature(a),
                    'local_validation_error': local_error,
                    'title': a['title'], 'description': a['description'], 'thumbnail_sha256': a['thumbnail_sha256'],
                    'last_readback': r, 'updated_at': now()})
        write(path, ledger)
    return {'video_id': r['video_id'], 'state': old['state'], 'complete': complete, 'local_validation_error': local_error}


def absent(args):
    j = job_at(args.job)
    cache = media_cache(j, args)
    a = asset_at(j, args.asset, hash_cache=cache)
    evidence = inside(j['work'], args.evidence)
    require(evidence.stat().st_size > 0 and args.reason.strip(), 'Explain actual reconciliation evidence')
    path = ledger_path(j)
    with locked(path.with_suffix('.lock')):
        ledger = ledger_read(path)
        old = ledger['uploads'].get(key_for(j, a))
        require(old is not None and not old.get('video_id'), 'Known uploaded IDs cannot be reset as absent')
        old.update({'state': 'confirmed_absent', 'reconciliation_evidence': str(evidence),
                    'reconciliation_reason': args.reason, 'updated_at': now()})
        write(path, ledger)
    return {'state': 'confirmed_absent', 'uploaded': False}


def status(args):
    j = job_at(args.job)
    cache = media_cache(j, args)
    ledger = ledger_read(ledger_path(j))
    entries = [v for v in ledger['uploads'].values() if v['vod_id'] == j['vod_id']]
    package = Path(j['work']) / 'publication.json'
    package_data = read(package) if package.exists() else {}
    planned = package_data.get('assets', [])
    expected = package_data.get('expected_kinds', j.get('expected_kinds'))
    represented = {a['kind'] for a in planned} | {v['kind'] for v in package_data.get('omitted_kinds', [])}
    missing = [k for k in expected or [] if k not in represented]
    done = 0
    stale = []
    if package.exists():
        try:
            check_package_source(j, package_data, cache)
        except (ValueError, OSError, KeyError, TypeError) as e:
            return {'vod_id': j['vod_id'], 'planned': len(planned), 'verified_private': 0,
                    'complete': False, 'source_error': str(e)}
    for a in planned:
        try:
            asset_at(j, a['id'], verify_source=False, hash_cache=cache)
        except (ValueError, OSError, KeyError, TypeError) as e:
            stale.append({'asset_id': a['id'], 'reason': str(e)})
            continue
        done += any(v['asset_id'] == a['id'] and v['sha256'] == a['sha256'] and v['state'] == 'verified_private'
                    and v.get('package_signature') == package_signature(a) for v in entries)
    return {'vod_id': j['vod_id'], 'planned': len(planned), 'verified_private': done,
            'complete': bool(planned) and done == len(planned) and not missing, 'stale_assets': stale,
            'expected_kinds': expected, 'missing_kinds': missing,
            'omitted_kinds': package_data.get('omitted_kinds', []),
            'uploads': [{'asset_id': v['asset_id'], 'state': v['state'], 'url': v.get('url')} for v in entries]}


def hash(args):
    """Compute genuine full media digests once, or audit them with --force-rehash."""
    j = {'root': str(Path(args.root).resolve())}
    cache = media_cache(j, args)
    results = []
    for value in args.file:
        path = inside(j['root'], value)
        results.append({'file': str(path), 'sha256': cache.hash(path)})
    return {'files': results}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    s = commands.add_parser('init'); s.add_argument('--url', required=True)
    s.add_argument('--root', default=str(DEFAULT_ROOT))
    s.add_argument('--profile', required=True, help='Recipient local settings or profile JSON; never a bundled personal profile')
    s = commands.add_parser('metadata'); s.add_argument('--job', required=True); s.add_argument('--raw', required=True)
    s.add_argument('--requested-id', required=True, help='Actual --id used for the captured TD info command')
    s = commands.add_parser('adopt'); s.add_argument('--job', required=True); s.add_argument('--config', required=True)
    s = commands.add_parser('prepare'); s.add_argument('--job', required=True); s.add_argument('--plan', required=True)
    s.add_argument('--ffprobe', default='ffprobe')
    s = commands.add_parser('begin'); s.add_argument('--job', required=True); s.add_argument('--asset', required=True)
    s.add_argument('--channel', required=True); s.add_argument('--evidence', required=True)
    s = commands.add_parser('record'); s.add_argument('--job', required=True); s.add_argument('--asset', required=True)
    s.add_argument('--receipt', required=True)
    s = commands.add_parser('absent'); s.add_argument('--job', required=True); s.add_argument('--asset', required=True)
    s.add_argument('--evidence', required=True); s.add_argument('--reason', required=True)
    s = commands.add_parser('status'); s.add_argument('--job', required=True)
    s = commands.add_parser('hash', help='Compute/cache media digests from the actual files')
    s.add_argument('--root', default=str(DEFAULT_ROOT))
    s.add_argument('--file', action='append', required=True)
    for name in ('adopt', 'prepare', 'begin', 'record', 'absent', 'status', 'hash'):
        parser = commands.choices[name]
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--force-rehash', action='store_true',
                           help='Read media fully and refresh the cache, even when its identity is unchanged')
        group.add_argument('--no-hash-cache', action='store_true',
                           help='Read media fully without reading or writing cache records')
    args = p.parse_args()
    try:
        result = globals()[args.command](args)
        if hasattr(args, '_hash_cache'):
            result['hash_cache'] = args._hash_cache.stats
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, StopIteration, subprocess.CalledProcessError) as e:
        print(json.dumps({'error': str(e), 'command': args.command}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
