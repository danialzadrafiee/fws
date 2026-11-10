import tempfile, unittest, threading
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import patch
from datetime import datetime, timezone
import pipeline as p

class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patches = [patch.object(p,'DB',root/'test.db'),patch.object(p,'THUMBS',root),patch.object(p,'CFG',{'posts_to_check':2,'posts_for_activity':12,'thumbnail_workers':2}),patch.object(p,'pause'),patch.object(p,'log'),patch.object(p,'summary')]
        for x in self.patches:
            x.start(); self.addCleanup(x.stop)
        self.con=p.db(); self.addCleanup(self.con.close)
        p.add_candidate(self.con,'alice','1','test'); self.con.commit()
        self.info=S(pk='1',full_name='Alice',biography='',follower_count=1,following_count=1,media_count=2,is_private=False,external_url='')
        self.medias=[S(pk=str(k),taken_at=datetime.now(timezone.utc),caption_text='',media_type=1,thumbnail_url=f'https://example.test/{k}.jpg',resources=[]) for k in range(2)]
        self.client=unittest.mock.Mock()
        self.client.user_info.return_value=self.info
        self.client.user_medias.return_value=self.medias
        x=patch.object(p,'ig',return_value=self.client); x.start(); self.addCleanup(x.stop)
    def run_stage(self): p.enrich(self.con,S(limit=100))
    def test_parallel_download_and_checkpoint(self):
        barrier=threading.Barrier(2)
        def download(job):
            barrier.wait(timeout=3)
            return job['path'],0.1
        with patch.object(p,'download_thumb',side_effect=download): self.run_stage()
        row=self.con.execute('select * from accounts').fetchone()
        self.assertEqual(row['status'],'enriched')
        self.assertEqual(row['enrich_stage'],'media')
        self.assertEqual(len(__import__('json').loads(row['thumbs'])),2)
    def test_failed_image_retry_without_api(self):
        with patch.object(p,'download_thumb',side_effect=RuntimeError('CDN down')): self.run_stage()
        self.assertEqual(self.con.execute('select status from accounts').fetchone()[0],'new')
        self.client.reset_mock()
        with patch.object(p,'download_thumb',side_effect=lambda j:(j['path'],0)): self.run_stage()
        self.client.user_info.assert_not_called(); self.client.user_medias.assert_not_called()
        self.assertEqual(self.con.execute('select status from accounts').fetchone()[0],'enriched')
    def test_posts_failure_resumes_after_profile(self):
        self.client.user_medias.side_effect=RuntimeError('offline')
        self.run_stage()
        self.assertEqual(self.con.execute('select enrich_stage from accounts').fetchone()[0],'profile')
        self.client.reset_mock(); self.client.user_medias.side_effect=None
        with patch.object(p,'download_thumb',side_effect=lambda j:(j['path'],0)): self.run_stage()
        self.client.user_info.assert_not_called(); self.client.user_medias.assert_called_once()
    def test_private_account_needs_no_download(self):
        self.info.is_private=True
        with patch.object(p,'download_thumb') as download: self.run_stage()
        download.assert_not_called(); self.client.user_medias.assert_not_called()
        self.assertEqual(self.con.execute('select status from accounts').fetchone()[0],'enriched')
    def test_expired_urls_refresh_posts_only(self):
        import requests
        error=requests.HTTPError('expired',response=S(status_code=403))
        with patch.object(p,'download_thumb',side_effect=error): self.run_stage()
        self.assertEqual(self.con.execute('select enrich_stage from accounts').fetchone()[0],'profile')
        self.client.reset_mock()
        with patch.object(p,'download_thumb',side_effect=lambda j:(j['path'],0)): self.run_stage()
        self.client.user_info.assert_not_called(); self.client.user_medias.assert_called_once()
    def test_next_profile_overlaps_download(self):
        p.add_candidate(self.con,'bob','2','test'); self.con.commit()
        entered=threading.Event(); next_profile=threading.Event()
        def info(user_id):
            if user_id=='2':
                self.assertTrue(entered.wait(3))
                next_profile.set()
            return self.info
        def download(job):
            entered.set()
            if not next_profile.wait(3): raise RuntimeError('downloads blocked API')
            return job['path'],0
        self.client.user_info.side_effect=info
        with patch.object(p,'download_thumb',side_effect=download): self.run_stage()
        self.assertEqual(self.con.execute("select count(*) from accounts where status='enriched'").fetchone()[0],2)
    def test_atomic_cache_and_partial_cleanup(self):
        import requests
        job={'url':'https://example.test/a.jpg','path':str(Path(self.tmp.name)/'image.jpg')}
        response=unittest.mock.MagicMock()
        response.__enter__.return_value=response
        response.iter_content.return_value=iter([b'image'])
        with patch('requests.get',return_value=response) as get:
            p.download_thumb(job); p.download_thumb(job)
            self.assertEqual(get.call_count,1)
        Path(job['path']).unlink()
        def broken():
            yield b'partial'
            raise requests.ConnectionError('broken')
        response.iter_content.return_value=broken()
        with patch('requests.get',return_value=response):
            with self.assertRaises(requests.ConnectionError): p.download_thumb(job)
        self.assertFalse(Path(job['path']).exists())
        self.assertFalse(Path(job['path']+'.part').exists())

if __name__=='__main__': unittest.main()
