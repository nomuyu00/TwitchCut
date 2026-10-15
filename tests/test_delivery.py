"""Synthetic render -> prepare -> ledger integration. No external actions."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import workflow
import render
from media_hash_cache import MediaHashCache
from environment import render_environment


class DeliveryIntegrationTests(unittest.TestCase):
    def test_rendered_asset_prepares_and_receipt_requires_private_readback(self):
        ffmpeg, ffprobe, font, family = render_environment()
        scratch = ROOT / '.test-work'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='delivery-', dir=scratch) as temp:
            root = Path(temp)
            channel = 'UC' + 'a' * 22
            profile = {'twitch_login': 'fixture', 'youtube_channel_id': channel,
                       'youtube_channel_url': f'https://www.youtube.com/channel/{channel}'}
            created = workflow.init(SimpleNamespace(url='https://www.twitch.tv/videos/123456789', root=root, profile=profile))
            job_path = Path(created['job'])
            job = workflow.job_at(job_path)
            work, delivery, sources = (Path(job[key]) for key in ('work', 'delivery', 'source_dir'))
            source = sources / 'source.mp4'
            subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30:duration=2',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True)
            chat = sources / 'chat.json'
            workflow.write(chat, {'video': {'id': job['vod_id'], 'start': 0, 'end': 2},
                                  'streamer': {'login': 'fixture'}, 'comments': []})
            job['metadata'] = {'title': 'Synthetic source', 'created_at_jst': '2026-01-01T09:00:00+09:00',
                               'owner_login': 'fixture', 'owner_name': 'Fixture', 'duration_seconds': 2}
            workflow.write(job_path, job)
            settings = {'schema': 1, '_config_path': str(root / 'settings.json'), 'workspace': str(root),
                        'tools': {'ffmpeg': ffmpeg, 'ffprobe': ffprobe},
                        'render': {'encoder': 'libx264', 'font': str(font), 'font_name': family}}
            edl = {'schema': 1, 'id': 'fixture_short', 'kind': 'short', 'title': 'Synthetic', 'source': str(source),
                   'source_date': '2026-01-01', 'cuts': [{'start': 0, 'end': 1.5, 'crop': {'x': 110, 'y': 0, 'w': 100, 'h': 180}}],
                   'captions': [{'start': .2, 'end': 1.2, 'speaker': 'streamer', 'text': '字幕の検査'}]}
            edl_path = work / 'fixture.json'
            workflow.write(edl_path, edl)
            output = delivery / 'Shorts/fixture.mp4'
            rendered = render.render(edl_path, settings, output)
            cache = MediaHashCache(root)
            # Test doubles exercise acceptance/transition logic, not human QA certification.
            evidence = work / 'review/synthetic-only.json'
            workflow.write(evidence, {'synthetic_test_fixture': True, 'not_a_human_review': True})
            workflow.write(work / 'source-review.json', {
                'vod_id': job['vod_id'], 'duration_seconds': 2,
                'video': {'path': str(source), 'sha256': cache.hash(source)},
                'chat': {'path': str(chat), 'sha256': cache.hash(chat)},
                'checks': {k: {'passed': True, 'evidence': str(evidence)} for k in
                           ('duration_and_streams', 'chat_identity_and_range', 'decode', 'download_logs')}})
            workflow.write(work / 'review/fixture.json', {'media_sha256': rendered['sha256'],
                           'checks': {k: {'passed': True, 'evidence': str(evidence)} for k in workflow.CHECKS}})
            scope_evidence = work / 'review/scope.json'
            workflow.write(scope_evidence, {'vod_id': job['vod_id'], 'authorized_by': 'user', 'scope': 'publication_kinds',
                                           'kinds': ['short'], 'statement': 'Synthetic test authorization; no real upload.'})
            plan = work / 'plan.json'
            workflow.write(plan, {'source_review': 'source-review.json',
                                 'authorized_scope': {'kinds': ['short'], 'authority': 'user', 'evidence': str(scope_evidence)},
                                 'assets': [{'id': 'fixture', 'kind': 'short', 'file': 'Shorts/fixture.mp4',
                                             'title': '合成テスト【2026.01.01】', 'summary': 'Synthetic test.', 'review': 'review/fixture.json'}]})
            prepared = workflow.prepare(SimpleNamespace(job=job_path, plan=plan, ffprobe=ffprobe))
            self.assertFalse(prepared['uploaded'])
            asset = workflow.read(prepared['publication'])['assets'][0]
            self.assertNotIn('#お絵描き', asset['description'])
            workflow.begin(SimpleNamespace(job=job_path, asset='fixture', channel=channel, evidence=evidence))
            receipt = work / 'review/receipt.json'
            data = {'video_id': 'TESTONLY001', 'channel_id': channel, 'privacy_status': 'public', 'publish_at': None,
                    'processing_status': 'succeeded', 'title': asset['title'], 'description': asset['description'],
                    'checks_reviewed': True, 'playback_reviewed': True, 'observed_at': workflow.now(),
                    'package_signature': asset['package_signature'], 'evidence': str(evidence)}
            workflow.write(receipt, data)
            result = workflow.record(SimpleNamespace(job=job_path, asset='fixture', receipt=receipt))
            self.assertEqual(result['state'], 'needs_review')
            self.assertFalse(workflow.status(SimpleNamespace(job=job_path))['complete'])
            data.update(privacy_status='private', observed_at=workflow.now())
            workflow.write(receipt, data)
            self.assertEqual(workflow.record(SimpleNamespace(job=job_path, asset='fixture', receipt=receipt))['state'], 'verified_private')
            self.assertTrue(workflow.status(SimpleNamespace(job=job_path))['complete'])
            with self.assertRaises(ValueError):
                workflow.begin(SimpleNamespace(job=job_path, asset='fixture', channel=channel, evidence=evidence))
            output.write_bytes(output.read_bytes() + b'changed')
            self.assertFalse(workflow.status(SimpleNamespace(job=job_path))['complete'])


if __name__ == '__main__':
    unittest.main()
