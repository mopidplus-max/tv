from __future__ import annotations

import pytest

from config import Config
from database import db
from stream_manager import stream_manager


@pytest.fixture()
def app(tmp_path):
    Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    Config.STORAGE_DIR = tmp_path / "storage"
    Config.HLS_DIR = tmp_path / "hls"
    Config.LOG_DIR = tmp_path / "logs"
    Config.ENGINE_ENABLED = False
    Config.TESTING = True

    from app import create_app

    application = create_app(start_engine=False)
    application.config.update(TESTING=True)
    yield application

    with application.app_context():
        db.session.remove()
        db.drop_all()
    stream_manager.stop_cleanup()


@pytest.fixture()
def client(app):
    return app.test_client()
