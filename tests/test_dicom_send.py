# -*- coding: utf-8 -*-
"""Тесты DICOM-доставки RTLC: локальный C-STORE SCP (pynetdicom),
синтетические валидные DICOM-файлы (pydicom). Только 127.0.0.1."""
import logging
import os
import random
import socket
import time
from types import SimpleNamespace

import pytest

pydicom = pytest.importorskip('pydicom', reason='нужен pydicom')
pynetdicom = pytest.importorskip('pynetdicom', reason='нужен pynetdicom')

from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid  # noqa: E402
from pynetdicom import AE, AllStoragePresentationContexts, evt  # noqa: E402
from pynetdicom.sop_class import Verification  # noqa: E402

from conftest import local_files  # noqa: E402

logging.getLogger('pynetdicom').setLevel(logging.CRITICAL)


def write_real_dicom(path, seed=1):
    """Минимальный валидный DICOM Part 10 (Secondary Capture 8x8)."""
    rnd = random.Random(seed)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = 'OT'
    ds.PatientName = f'SYNTH^{seed}'
    ds.PatientID = f'SYNTH{seed}'
    ds.Rows = 8
    ds.Columns = 8
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.PixelData = bytes(rnd.getrandbits(8) for _ in range(8 * 8 * 2))
    try:
        ds.save_as(path, enforce_file_format=True)
    except TypeError:
        ds.save_as(path, write_like_original=False)
    return ds


@pytest.fixture
def make_scp():
    """Фабрика C-STORE SCP на 127.0.0.1 (эфемерный или заданный порт)."""
    servers = []

    def _start(port=0):
        received = []

        def on_c_store(event):
            ds = event.dataset
            ds.file_meta = event.file_meta
            received.append(ds)
            return 0x0000

        ae = AE(ae_title='TESTSCP')
        ae.supported_contexts = AllStoragePresentationContexts
        ae.add_supported_context(Verification)
        srv = ae.start_server(('127.0.0.1', port),
                              block=False,
                              evt_handlers=[(evt.EVT_C_STORE, on_c_store)])
        servers.append(srv)
        return SimpleNamespace(port=srv.server_address[1],
                               received=received,
                               server=srv)

    yield _start
    for srv in servers:
        srv.shutdown()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def dicom_opts(scp, **extra):
    o = dict(to_dicom='true',
             dicom_host='127.0.0.1',
             dicom_port=scp.port,
             dicom_called_aet='TESTSCP',
             dicom_timeout='10')
    o.update(extra)
    return o


def deliver_until(inst, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst.walker()
        if predicate():
            return True
        time.sleep(0.15)
    return predicate()


def same_study(ds_a, ds_b):
    return (ds_a.SOPInstanceUID == ds_b.SOPInstanceUID
            and ds_a.PatientID == ds_b.PatientID
            and len(ds_a.PixelData) == len(ds_b.PixelData))


# --- Сценарий: оба стока -------------------------------------------------

def test_both_sinks_folder_and_dicom(paths, make_rtlc, make_scp):
    scp = make_scp()
    inst = make_rtlc(**dicom_opts(scp))
    src = os.path.join(paths['remote'], 'img1.dcm')
    ds = write_real_dicom(src, seed=1)
    src_bytes = open(src, 'rb').read()
    src_mtime = os.stat(src).st_mtime

    assert deliver_until(inst, lambda: len(scp.received) == 1)
    files = local_files(paths['local'])
    assert len(files) == 1, 'копия в папке не создана'
    assert open(os.path.join(paths['local'], files[0]), 'rb').read() == src_bytes
    assert same_study(scp.received[0], ds)
    # инвариант: источник не изменён и не удалён
    assert open(src, 'rb').read() == src_bytes
    assert os.stat(src).st_mtime == src_mtime
    assert not inst.remoteList


# --- Сценарий: только DICOM, папка не требуется ---------------------------

def test_dicom_only_no_local_folder_needed(paths, make_rtlc, make_scp):
    scp = make_scp()
    missing_local = os.path.join(paths['work'], 'no_such_dir')
    inst = make_rtlc(local=missing_local,
                     **dicom_opts(scp, to_folder='false'))
    assert inst.toDicom and not inst.toFolder
    ds = write_real_dicom(os.path.join(paths['remote'], 'img2.dcm'), seed=2)

    assert deliver_until(inst, lambda: len(scp.received) == 1)
    assert not os.path.exists(missing_local), 'local папка не должна создаваться'
    assert same_study(scp.received[0], ds)


# --- Сценарий: узел лежит, затем поднялся — ретрай без дубля в папке -----

def test_dicom_node_down_then_up_retries_without_folder_duplicate(
        paths, make_rtlc, make_scp):
    port = free_port()  # никого не слушает
    inst = make_rtlc(**dicom_opts(SimpleNamespace(port=port)))
    write_real_dicom(os.path.join(paths['remote'], 'img3.dcm'), seed=3)

    assert deliver_until(inst, lambda: len(local_files(paths['local'])) == 1)
    for _ in range(3):
        inst.walker()
    assert inst.remoteList, 'файл должен ждать недоступный DICOM-узел'
    entry = inst.state['files'][
        os.path.join(paths['remote'], 'img3.dcm')]
    assert entry.get('folder'), 'папка должна быть выполнена'
    assert not (entry.get('dicom') or {}).get('sent')
    assert entry['dicom']['attempts'] >= 2

    scp = make_scp(port)  # узел поднялся
    assert deliver_until(inst, lambda: len(scp.received) == 1)
    assert len(local_files(paths['local'])) == 1, 'дубль в папке!'
    assert not inst.remoteList


# --- Сценарий: не-DICOM файл — перманентная ошибка, без вечного ретрая ---

def test_non_dicom_file_permanent_error_no_endless_retry(paths, make_rtlc,
                                                         make_scp):
    scp = make_scp()
    inst = make_rtlc(**dicom_opts(scp))
    bad = os.path.join(paths['remote'], 'broken.dcm')
    with open(bad, 'wb') as f:
        f.write(b'this is not a dicom file at all')

    assert deliver_until(inst, lambda: len(local_files(paths['local'])) == 1)
    for _ in range(3):
        inst.walker()
    entry = inst.state['files'][bad]
    assert entry['dicom']['permanent'], 'ошибка должна стать перманентной'
    assert entry['dicom']['attempts'] == 1, 'ретраи бессмысленны'
    assert len(scp.received) == 0
    assert not inst.remoteList, 'перманентная ошибка завершает файл'
    assert inst.stats['dicom_failed'] >= 1
    # очередь жива: следующий нормальный файл обрабатывается
    ds = write_real_dicom(os.path.join(paths['remote'], 'ok.dcm'), seed=4)
    assert deliver_until(inst, lambda: len(scp.received) == 1)
    assert same_study(scp.received[0], ds)


# --- Валидация конфигурации ----------------------------------------------

def test_both_sinks_disabled_refused(paths, make_rtlc):
    with pytest.raises(SystemExit):
        make_rtlc(to_folder='false', to_dicom='false')


def test_dicom_without_host_refused(paths, make_rtlc):
    with pytest.raises(SystemExit):
        make_rtlc(to_dicom='true')


def test_put_config_delivery_validation(paths, make_rtlc):
    webui = pytest.importorskip('webui')
    from starlette.testclient import TestClient
    eng = webui.Engine()
    try:
        make_rtlc()
        app = webui.create_app(eng)
        with TestClient(app) as c:
            h = {'X-API-Key': app.state.web_config.webApiKey}
            assert c.put('/api/v1/config', headers=h, json={
                'to_folder': 'false', 'to_dicom': 'false'}).status_code == 400
            assert c.put('/api/v1/config', headers=h, json={
                'to_dicom': 'true', 'dicom_host': '', 'dicom_called_aet': ''
            }).status_code == 400
            assert c.put('/api/v1/config', headers=h, json={
                'dicom_port': '99999'}).status_code == 400
    finally:
        eng.stop()


# --- C-ECHO endpoint ------------------------------------------------------

def test_echo_endpoint(paths, make_rtlc, make_scp):
    webui = pytest.importorskip('webui')
    from starlette.testclient import TestClient
    scp = make_scp()
    eng = webui.Engine()
    try:
        make_rtlc(**dicom_opts(scp))
        app = webui.create_app(eng)
        with TestClient(app) as c:
            h = {'X-API-Key': app.state.web_config.webApiKey}
            r = c.post('/api/v1/dicom/echo', headers=h)
            assert r.status_code == 200
            assert r.json()['ok'], r.json()
    finally:
        eng.stop()


def test_echo_endpoint_node_down(paths, make_rtlc):
    webui = pytest.importorskip('webui')
    from starlette.testclient import TestClient
    eng = webui.Engine()
    try:
        make_rtlc(**dicom_opts(SimpleNamespace(port=free_port())))
        app = webui.create_app(eng)
        with TestClient(app) as c:
            h = {'X-API-Key': app.state.web_config.webApiKey}
            r = c.post('/api/v1/dicom/echo', headers=h)
            assert r.status_code == 200
            assert not r.json()['ok']
    finally:
        eng.stop()
