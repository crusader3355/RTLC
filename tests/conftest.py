# -*- coding: utf-8 -*-
"""Тестовый стенд RTLC: temp-источник, temp-назначение, генератор
синтетических DICOM Part 10 файлов. Только локальные temp-папки."""
import logging
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rtlc  # noqa: E402


def make_dicom_bytes(size: int, seed: int = 42) -> bytes:
    """Синтетический DICOM Part 10: 128-байтный preamble + 'DICM' + тело."""
    header = b'\x00' * 128 + b'DICM'
    meta = (b'\x02\x00\x00\x00UL\x04\x00\x01\x00\x00\x00'
            b'\x02\x00\x02\x00UI\x12\x001.2.840.10008.5.1.4.1.1.7\x00')
    prefix = header + meta
    if size <= len(prefix):
        return prefix[:size]
    rnd = random.Random(seed)
    body = bytes(rnd.getrandbits(8) for _ in range(size - len(prefix)))
    return prefix + body


def write_dicom(path, size=4096, seed=42) -> bytes:
    data = make_dicom_bytes(size, seed)
    with open(path, 'wb') as f:
        f.write(data)
    return data


def local_files(local_dir):
    return sorted(p for p in os.listdir(local_dir))


def local_contents(local_dir):
    return sorted(
        open(os.path.join(local_dir, p), 'rb').read()
        for p in os.listdir(local_dir))


def write_config(paths, **opts):
    o = {
        'start_date': '2000-01-01 00:00',
        'last_file': '0',
        'refresh_time': '1',
        'extension_file': '.dcm',
        'stabilize_time': '0.3',
    }
    o.update({k: str(v) for k, v in opts.items() if k in o})
    remote = opts.get('remote', paths['remote'])
    local = opts.get('local', paths['local'])
    ini = (
        '[Options]\n'
        f"start_date = {o['start_date']}\n"
        f"last_file = {o['last_file']}\n"
        f"refresh_time = {o['refresh_time']}\n"
        f"extension_file = {o['extension_file']}\n"
        f"stabilize_time = {o['stabilize_time']}\n\n"
        '[Paths]\n'
        f'remote = {remote}\n'
        f'local = {local}\n\n'
        '[Logs]\nlevel = DEBUG\nsize = 5\nbackups = 2\n'
    )
    delivery = []
    for k in ('to_folder', 'to_dicom'):
        if k in opts:
            delivery.append(f'{k} = {opts[k]}')
    if delivery:
        ini += '\n[Delivery]\n' + '\n'.join(delivery) + '\n'
    dicom = []
    for k in ('host', 'port', 'called_aet', 'calling_aet', 'timeout',
              'retry_initial', 'retry_max', 'breaker_threshold',
              'breaker_cooldown', 'queue_tick'):
        if f'dicom_{k}' in opts:
            dicom.append(f"{k} = {opts[f'dicom_{k}']}")
    if dicom:
        ini += '\n[Dicom]\n' + '\n'.join(dicom) + '\n'
    with open(os.path.join(paths['work'], 'config.ini'), 'w',
              encoding='utf-8') as f:
        f.write(ini)


@pytest.fixture
def paths(tmp_path):
    work = tmp_path / 'work'
    remote = tmp_path / 'remote'
    local = tmp_path / 'local'
    for d in (work, remote, local):
        d.mkdir(parents=True)
    return {'work': str(work), 'remote': str(remote), 'local': str(local)}


@pytest.fixture
def make_rtlc(paths, monkeypatch):
    """Фабрика экземпляров CopyUtility в изолированной рабочей директории.

    Повторный вызов без параметров = «перезапуск» приложения:
    config.ini и файл состояния не перезаписываются.
    """
    old_cwd = os.getcwd()

    def factory(**opts):
        if opts or not os.path.exists(os.path.join(paths['work'],
                                                   'config.ini')):
            write_config(paths, **opts)
        monkeypatch.setattr(rtlc, 'getModulePath', lambda: paths['work'])
        return rtlc.CopyUtility()

    yield factory

    os.chdir(old_cwd)
    logger = logging.getLogger('rltc')
    for h in logger.handlers[:]:
        h.close()
        logger.removeHandler(h)


@pytest.fixture
def hook_copy_writes(monkeypatch):
    """Перехват фактической записи копии.

    Python 3.14 на Windows копирует через shutil._winapi.CopyFile2,
    старые версии — через copyfileobj/_copyfileobj_readinto.
    Патчим все точки. handler(src_name, dst_name, do_copy):
    вызывается на каждое копирование, do_copy() выполняет реальную запись.
    """
    real_copyfileobj = rtlc.shutil.copyfileobj
    real_readinto = getattr(rtlc.shutil, '_copyfileobj_readinto', None)
    real_copyfile2 = getattr(getattr(rtlc.shutil, '_winapi', None),
                             'CopyFile2', None)

    def install(handler):
        def obj_wrapper(fsrc, fdst, length=None):
            return handler(getattr(fsrc, 'name', '?'),
                           getattr(fdst, 'name', '?'),
                           lambda: real_copyfileobj(fsrc, fdst))

        monkeypatch.setattr(rtlc.shutil, 'copyfileobj', obj_wrapper)
        if real_readinto is not None:
            monkeypatch.setattr(rtlc.shutil, '_copyfileobj_readinto',
                                obj_wrapper)
        if real_copyfile2 is not None:
            def win_wrapper(src, dst, flags):
                return handler(src, dst,
                               lambda: real_copyfile2(src, dst, flags))

            monkeypatch.setattr(rtlc.shutil._winapi, 'CopyFile2', win_wrapper)

    return install
