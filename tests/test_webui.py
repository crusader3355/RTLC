# -*- coding: utf-8 -*-
"""Тесты веб-интерфейса RTLC (FastAPI TestClient, реальный движок в потоке)."""
import os
import time

import pytest
from starlette.testclient import TestClient

from conftest import make_dicom_bytes, write_dicom

webui = pytest.importorskip('webui', reason='требуются fastapi/uvicorn')


@pytest.fixture
def engine():
    eng = webui.Engine()
    yield eng
    eng.stop()


@pytest.fixture
def client(paths, make_rtlc, engine):
    # make_rtlc создаёт рабочую директорию с config.ini и экземпляр движка
    engine.utility = make_rtlc()
    app = webui.create_app(engine)
    with TestClient(app) as c:
        c.api_key = app.state.web_config.webApiKey
        yield c


def auth(client):
    return {'X-API-Key': client.api_key}


def test_health_no_auth(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_index_page_served(client):
    r = client.get('/')
    assert r.status_code == 200
    assert 'RTLC' in r.text


def test_status_requires_api_key(client):
    assert client.get('/api/v1/status').status_code == 401
    assert client.get('/api/v1/status',
                      headers={'X-API-Key': 'wrong'}).status_code == 403
    r = client.get('/api/v1/status', headers=auth(client))
    assert r.status_code == 200
    assert r.json()['engine'] in ('stopped', 'running')


def test_config_get_masked_and_put_validation(paths, make_rtlc, engine):
    make_rtlc()  # рабочая директория с config.ini
    app = webui.create_app(engine)
    with TestClient(app) as c:
        key = app.state.web_config.webApiKey
        h = {'X-API-Key': key}

        cfg = c.get('/api/v1/config', headers=h).json()
        assert cfg['web_api_key'] == '***'  # секреты замаскированы
        assert cfg['remote'] == paths['remote']

        r = c.put('/api/v1/config', headers=h, json={'refresh_time': '5'})
        assert r.status_code == 200, r.text
        cfg2 = c.get('/api/v1/config', headers=h).json()
        assert cfg2['refresh_time'] == 5

        assert c.put('/api/v1/config', headers=h,
                     json={'refresh_time': '-1'}).status_code == 400
        assert c.put('/api/v1/config', headers=h,
                     json={'unknown_option': '1'}).status_code == 400
        # петля: назначение внутри источника — отклоняется до записи
        r = c.put('/api/v1/config', headers=h,
                  json={'local': os.path.join(paths['remote'], 'out')})
        assert r.status_code == 400
        # '***' не затирает реальный ключ
        c.put('/api/v1/config', headers=h, json={'web_api_key': '***'})
        assert app.state.web_config.webApiKey == key


def test_engine_control_copy_and_ledger(paths, make_rtlc, engine):
    make_rtlc()
    data = write_dicom(os.path.join(paths['remote'], 'via_api.dcm'),
                       4096, seed=11)
    app = webui.create_app(engine)
    with TestClient(app) as c:
        h = {'X-API-Key': app.state.web_config.webApiKey}

        r = c.post('/api/v1/control/scan_now', headers=h)
        assert r.status_code == 409  # движок остановлен

        r = c.post('/api/v1/control/start', headers=h)
        assert r.status_code == 200, r.text
        assert r.json()['engine'] == 'running'

        deadline = time.time() + 20
        snap = {}
        while time.time() < deadline:
            snap = c.get('/api/v1/status', headers=h).json()
            if snap['utility'] and snap['utility']['stats']['copied'] >= 1:
                break
            time.sleep(0.5)
        assert snap['utility']['stats']['copied'] >= 1, \
            f'файл не скопирован через движок: {snap}'
        assert data in [
            open(os.path.join(paths['local'], f), 'rb').read()
            for f in os.listdir(paths['local'])
        ]

        ledger = c.get('/api/v1/ledger', headers=h).json()
        assert ledger['total'] == 1
        item = ledger['items'][0]
        assert item['path'].endswith('via_api.dcm')
        assert item['size'] == len(data)
        assert item['copied_at']

        r = c.post('/api/v1/control/stop', headers=h)
        assert r.status_code == 200
        assert r.json()['engine'] == 'stopped'


def test_ledger_from_disk_when_engine_stopped(paths, make_rtlc, engine):
    inst = make_rtlc()
    write_dicom(os.path.join(paths['remote'], 'a.dcm'), 2048, seed=5)
    deadline = time.time() + 10
    while time.time() < deadline and not os.listdir(paths['local']):
        inst.walker()
        time.sleep(0.15)
    assert os.listdir(paths['local'])

    engine.utility = None  # движок «остановлен» — читаем state с диска
    app = webui.create_app(engine)
    with TestClient(app) as c:
        h = {'X-API-Key': app.state.web_config.webApiKey}
        ledger = c.get('/api/v1/ledger', headers=h).json()
        assert ledger['total'] == 1
        assert ledger['items'][0]['path'].endswith('a.dcm')


def test_log_tail(client):
    inst = client.app.state.engine.utility
    for i in range(10):
        inst.log.info(f'test line {i}')
    r = client.get('/api/v1/log?lines=5', headers=auth(client))
    assert r.status_code == 200
    lines = r.json()['lines']
    assert len(lines) == 5
    assert 'test line 9' in lines[-1]


def test_control_unknown_action(client):
    r = client.post('/api/v1/control/explode', headers=auth(client))
    assert r.status_code == 404
