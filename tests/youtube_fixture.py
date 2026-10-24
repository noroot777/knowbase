import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import maintenance, pipeline_state
from kb.feeds import registry as reg, sync
from kb.web import intake_api
from kb.youtube import media


class YouTubeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, attribute, value in ((reg, 'DB_PATH', self.root / 'feeds.db'),
                                         (maintenance, 'DATA_DIR', self.root),
                                         (sync, 'VAULT_ROOT', self.root / 'vault'),
                                         (media, 'DATA_DIR', self.root),
                                         (media, 'VIDEOS_ROOT', self.root / 'upstream')):
            override = patch.object(target, attribute, value)
            override.start()
            self.addCleanup(override.stop)
        old = deepcopy(pipeline_state._state)
        pipeline_state._state.update(running=False, stop_requested=False)
        self.addCleanup(lambda: pipeline_state._state.update(old))
        app = FastAPI()
        app.include_router(intake_api.router)
        self.client = TestClient(app)

    def channel(self, number=1, **values):
        return reg.save_subscription('youtube', {
            'name': f'频道 {number}', 'url': 'https://www.youtube.com/channel/UC' + f'{number:022d}',
            **values})

    def isolate(self):
        with reg.db() as connection:
            connection.execute("UPDATE subscriptions SET enabled=0 WHERE kind='youtube'")

    def entry(self, number=1, hours=1, views=1000):
        video_id = f'{number:011d}'
        return {'video_id': video_id, 'url': 'https://www.youtube.com/watch?v=' + video_id,
                'title': f'AI agent coding tutorial {number}', 'description': 'Practical AI workflow',
                'published': (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),
                'views': views}

    def note(self, row, *arguments):
        return {'url': row['entry']['url'], 'title': '智能体实操教程',
                'body': '本视频讲解智能体的具体实现和限制，内容已翻译为中文。\n\n完整中文转录。',
                'author': row['channel'], 'published': row['entry']['published'],
                'content_kind': 'markdown', 'body_provenance': 'youtube:uploaded_captions'}

    def settings(self, **values):
        current = reg.get_settings('youtube')['youtube']
        return reg.save_settings('youtube', {'youtube': {**current, **values}})

    def policy(self, source):
        return json.loads(source['config'])
