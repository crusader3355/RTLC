# -*- coding: utf-8 -*-
"""Сценарные тесты RTLC на синтетических данных.

Каждый тест соответствует сценарию из задания. Часть тестов на исходном
коде падает — это воспроизведение дефектов (см. REPORT.md).
"""
import ctypes
import os
import threading
import time

import pytest

from conftest import (local_contents, local_files, make_dicom_bytes,
                      write_dicom)


def run_until_copied(inst, local_dir, want=1, timeout=10.0):
    """Крутит циклы walker(), пока в назначении не появится want файлов."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst.walker()
        done = [f for f in local_files(local_dir)
                if not f.startswith('.')]
        if len(done) >= want:
            return done
        time.sleep(0.15)
    return [f for f in local_files(local_dir) if not f.startswith('.')]


# --- Сценарий 1: базовое копирование + побайтовое сравнение -------------

def test_basic_copy_byte_identical(paths, make_rtlc):
    inst = make_rtlc()
    payloads = set()
    sub = os.path.join(paths['remote'], 'nested', 'study1')
    os.makedirs(sub)
    for i, target in enumerate([
            os.path.join(paths['remote'], 'a.dcm'),
            os.path.join(paths['remote'], 'b.dcm'),
            os.path.join(sub, 'c.dcm'),
    ]):
        payloads.add(write_dicom(target, 4096, seed=i))
    run_until_copied(inst, paths['local'], want=3, timeout=10)
    assert not inst.remoteList, f'остались необработанные: {inst.remoteList}'
    copied = local_contents(paths['local'])
    assert len(copied) == 3
    assert copied == sorted(payloads), 'содержимое копий не совпало побайтово'


# --- Сценарий 2: медленная запись большого файла -------------------------

def test_slow_write_copied_only_after_complete(paths, make_rtlc):
    """Файл пишется кусками с паузами: копия должна появиться только после
    завершения записи и совпадать побайтово (проверка «стабилизации»).

    Паузы между кусками (0.15 с) меньше окна стабилизации (0.3 с):
    корректная реализация ждёт окно тишины после ПОСЛЕДНЕГО куска."""
    inst = make_rtlc()
    os.makedirs(os.path.join(paths['remote'], 'series1'))
    target = os.path.join(paths['remote'], 'series1', 'img.dcm')
    full = make_dicom_bytes(4 * 1024 * 1024, seed=7)
    started = threading.Event()
    done = threading.Event()

    def writer():
        with open(target, 'wb') as f:
            f.write(full[:len(full) // 3])
            f.flush()
            started.set()
            time.sleep(0.15)
            f.write(full[len(full) // 3:2 * len(full) // 3])
            f.flush()
            time.sleep(0.15)
            f.write(full[2 * len(full) // 3:])
            f.flush()
        done.set()

    t = threading.Thread(target=writer)
    t.start()
    assert started.wait(5)
    copied = run_until_copied(inst, paths['local'], timeout=15)
    t.join(5)
    assert done.is_set()
    assert len(copied) == 1, f'ожидали 1 файл, есть: {copied}'
    dest = os.path.join(paths['local'], copied[0])
    with open(dest, 'rb') as f:
        assert f.read() == full, 'скопирован недописанный (частичный) файл'


# --- Сценарий 3: файл со «старым» таймштампом не должен потеряться -------

def _set_creation_time_windows(path, year=2001):
    """Эмуляция robocopy/restore: creation time выставлен в прошлое."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    FILE_WRITE_ATTRIBUTES = 0x0100
    OPEN_EXISTING = 3
    h = kernel32.CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, 0, None,
                             OPEN_EXISTING, 0, None)
    assert h not in (None, 0, 0xFFFFFFFFFFFFFFFF), 'CreateFileW failed'
    try:
        # 2001-01-01 UTC в Windows FILETIME (100-нс тики с 1601-01-01)
        ticks = 116444736000000000 + int(
            (time.mktime((year, 1, 1, 0, 0, 0, 0, 1, 0))
             - time.timezone) * 10**7)
        ctime = ctypes.c_int64(ticks)
        ok = kernel32.SetFileTime(h, ctypes.byref(ctime), None, None)
        assert ok, 'SetFileTime failed'
    finally:
        kernel32.CloseHandle(h)


@pytest.mark.skipif(os.name != 'nt',
                    reason='управление creation time — только Windows')
def test_old_timestamp_file_not_lost(paths, make_rtlc):
    """Файл, восстановленный со старым creation time (robocopy/архив),
    появляется ПОСЛЕ того, как watermark уже ушёл вперёд."""
    inst = make_rtlc()
    write_dicom(os.path.join(paths['remote'], 'first.dcm'), 4096, seed=1)
    run_until_copied(inst, paths['local'], want=1, timeout=10)
    assert len(local_files(paths['local'])) == 1

    restored = os.path.join(paths['remote'], 'restored.dcm')
    data = write_dicom(restored, 4096, seed=2)
    _set_creation_time_windows(restored, year=2001)

    copied = run_until_copied(inst, paths['local'], want=2, timeout=5)
    contents = local_contents(paths['local'])
    assert data in contents, ('файл со старым таймштампом потерян '
                              f'(в назначении: {copied})')


# --- Сценарий 4: перезапуск посреди копирования --------------------------

def test_restart_mid_copy_no_duplicates_no_leftovers(paths, make_rtlc,
                                                     hook_copy_writes):
    """Падение процесса посреди пачки: после рестарта — ни дублей,
    ни недокопированных temp-файлов, бэклог догнан."""
    inst = make_rtlc()
    d1 = write_dicom(os.path.join(paths['remote'], 'a.dcm'), 8192, seed=1)
    d2 = write_dicom(os.path.join(paths['remote'], 'b.dcm'), 8192, seed=2)

    state = {'calls': 0}

    def crashy(src_name, dst_name, do_copy):
        state['calls'] += 1
        if state['calls'] == 2:
            with open(dst_name, 'wb') as f:  # недокопированный файл + падение
                f.write(b'PARTIAL')
            raise KeyboardInterrupt('simulated crash')
        return do_copy()

    hook_copy_writes(crashy)
    with pytest.raises(KeyboardInterrupt):
        for _ in range(50):  # крутим циклы, пока не начнётся копирование
            inst.walker()
            time.sleep(0.1)

    inst2 = make_rtlc()  # «перезапуск» приложения
    run_until_copied(inst2, paths['local'], want=2, timeout=10)

    files = local_files(paths['local'])
    assert len(files) == 2, f'дубли или мусор в назначении: {files}'
    assert local_contents(paths['local']) == sorted([d1, d2])


# --- Сценарий 5: повторное появление файла с тем же именем ---------------

def test_resend_same_name_new_content_not_lost(paths, make_rtlc):
    inst = make_rtlc()
    f = os.path.join(paths['remote'], 'study.dcm')
    write_dicom(f, 4096, seed=1)
    run_until_copied(inst, paths['local'], want=1, timeout=10)
    assert len(local_files(paths['local'])) == 1

    time.sleep(0.05)
    v2 = write_dicom(f, 4096, seed=2)  # перезапись тем же именем
    run_until_copied(inst, paths['local'], want=2, timeout=5)
    contents = local_contents(paths['local'])
    assert v2 in contents, ('обновлённая версия файла потеряна '
                            '(молчаливый пропуск перезаписи)')


# --- Сценарий 6: всплеск 500+ файлов -------------------------------------

def test_burst_500_files_nothing_lost(paths, make_rtlc):
    inst = make_rtlc()
    payloads = set()
    for i in range(520):
        sub = os.path.join(paths['remote'], f'mod{i % 7}')
        os.makedirs(sub, exist_ok=True)
        payloads.add(write_dicom(os.path.join(sub, f'img_{i:04d}.dcm'),
                                 2048, seed=i))
    run_until_copied(inst, paths['local'], want=520, timeout=120)
    assert not inst.remoteList
    files = local_files(paths['local'])
    assert len(files) == 520, f'потеряно {520 - len(files)} файлов'
    assert local_contents(paths['local']) == sorted(payloads)


# --- Сценарий 7: недоступность назначения и восстановление ---------------

def test_destination_outage_then_recovery(paths, make_rtlc):
    inst = make_rtlc()
    data = write_dicom(os.path.join(paths['remote'], 'a.dcm'), 4096, seed=1)
    hidden = paths['local'] + '_hidden'
    os.rename(paths['local'], hidden)  # назначение «отвалилось»

    inst.walker()  # не должно упасть и не должно потерять файл
    assert inst.remoteList, 'файл выброшен из очереди при недоступном назначении'

    os.rename(hidden, paths['local'])  # назначение вернулось
    run_until_copied(inst, paths['local'], want=1, timeout=5)
    assert local_contents(paths['local']) == [data]


# --- Сценарий 8: кириллица, пробелы, длинные пути, подпапки, .DCM --------

def test_cyrillic_spaces_nested_uppercase_extension(paths, make_rtlc):
    inst = make_rtlc()  # фильтр '.dcm'
    targets = []
    p1 = os.path.join(paths['remote'], 'Исследование №1', 'снимок груди.dcm')
    os.makedirs(os.path.dirname(p1))
    targets.append(write_dicom(p1, 4096, seed=1))
    p2 = os.path.join(paths['remote'], 'STUDY', 'IMAGE.DCM')  # верхний регистр
    os.makedirs(os.path.dirname(p2))
    targets.append(write_dicom(p2, 4096, seed=2))

    run_until_copied(inst, paths['local'], want=2, timeout=5)
    contents = local_contents(paths['local'])
    for data in targets:
        assert data in contents, ('файл не скопирован (кириллица/пробелы/'
                                  'верхний регистр расширения)')


def test_long_path(paths, make_rtlc):
    inst = make_rtlc()
    deep = paths['remote']
    try:
        while len(deep) < 300:
            deep = os.path.join(deep, 'd' * 40)
            os.mkdir(deep)
    except OSError as exc:
        pytest.skip(f'ОС не дала создать путь >260 символов: {exc}')
    data = write_dicom(os.path.join(deep, 'img.dcm'), 2048, seed=7)
    run_until_copied(inst, paths['local'], want=1, timeout=5)
    assert data in local_contents(paths['local'])


def test_no_extension_with_empty_filter(paths, make_rtlc):
    inst = make_rtlc(extension_file='')
    data = write_dicom(os.path.join(paths['remote'], 'IMG001'), 2048, seed=3)
    run_until_copied(inst, paths['local'], want=1, timeout=5)
    assert data in local_contents(paths['local'])


# --- Маркер '.[empty]': файлы без расширения в фильтре ---------------------

def test_empty_marker_matches_extensionless(paths, make_rtlc):
    inst = make_rtlc(extension_file='.dcm,.[empty]')
    dcm = write_dicom(os.path.join(paths['remote'], 'a.dcm'), 2048, seed=1)
    noext = write_dicom(os.path.join(paths['remote'], 'IMG001'), 2048, seed=2)
    write_dicom(os.path.join(paths['remote'], 'b.bin'), 2048, seed=3)

    run_until_copied(inst, paths['local'], want=2, timeout=5)
    contents = local_contents(paths['local'])
    assert dcm in contents, '.dcm не скопирован при фильтре .dcm,.[empty]'
    assert noext in contents, ('файл без расширения не скопирован '
                               'при фильтре .dcm,.[empty]')
    assert len(contents) == 2, 'файл с чужим расширением прошёл фильтр'


def test_empty_marker_alone(paths, make_rtlc):
    inst = make_rtlc(extension_file='.[empty]')
    noext = write_dicom(os.path.join(paths['remote'], 'IMG001'), 2048, seed=1)
    write_dicom(os.path.join(paths['remote'], 'a.dcm'), 2048, seed=2)

    run_until_copied(inst, paths['local'], want=1, timeout=5)
    contents = local_contents(paths['local'])
    assert noext in contents
    assert len(contents) == 1, 'при фильтре .[empty] скопировался файл с расширением'


def test_plain_filter_rejects_extensionless(paths, make_rtlc):
    inst = make_rtlc(extension_file='.dcm')  # без маркера — регрессия
    dcm = write_dicom(os.path.join(paths['remote'], 'a.dcm'), 2048, seed=1)
    write_dicom(os.path.join(paths['remote'], 'IMG001'), 2048, seed=2)

    run_until_copied(inst, paths['local'], want=1, timeout=5)
    contents = local_contents(paths['local'])
    assert dcm in contents
    assert len(contents) == 1, 'файл без расширения прошёл фильтр .dcm'


# --- Сценарий 9: битый/нулевой файл — очередь живёт ----------------------

def test_zero_size_and_locked_file_queue_survives(paths, make_rtlc):
    inst = make_rtlc()
    empty = os.path.join(paths['remote'], 'empty.dcm')
    open(empty, 'wb').close()
    good = write_dicom(os.path.join(paths['remote'], 'good.dcm'), 4096, seed=3)
    bad = os.path.join(paths['remote'], 'bad.dcm')
    with open(bad, 'wb') as f:
        f.write(b'x' * 100)

    # антивирусный лок: открываем без разделения доступа на чтение
    handle = None
    if os.name == 'nt':
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(str(bad), 0x80000000, 0, None, 3,
                                      0x80, None)
        assert handle not in (None, 0, 0xFFFFFFFFFFFFFFFF)
    try:
        # пока bad.dcm залочен, остальные должны скопироваться
        run_until_copied(inst, paths['local'], want=2, timeout=10)
    finally:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)

    contents = local_contents(paths['local'])
    assert b'' in contents, 'нулевой файл не скопирован'
    assert good in contents, 'хороший файл не скопирован из-за соседнего битого'

    copied = run_until_copied(inst, paths['local'], want=3, timeout=10)
    assert len(copied) == 3, 'залоченный файл не докопировался после снятия лока'
    log = open(os.path.join(paths['work'], 'Logs', 'rtlc.log'),
               encoding='utf-8').read()
    assert 'bad.dcm' in log, 'ошибка копирования не залогирована'


# --- Доп.: атомарность записи в назначение -------------------------------

def test_no_partial_file_visible_in_destination(paths, make_rtlc,
                                                hook_copy_writes):
    """Во время копирования PACS не должен видеть частичный файл:
    в назначении допустимы только temp-имена или финальные целые файлы."""
    inst = make_rtlc()
    data = make_dicom_bytes(8 * 1024 * 1024, seed=9)
    with open(os.path.join(paths['remote'], 'big.dcm'), 'wb') as f:
        f.write(data)

    seen = []

    def spy(src_name, dst_name, do_copy):
        seen.append(dst_name)
        return do_copy()

    hook_copy_writes(spy)
    copied = run_until_copied(inst, paths['local'], want=1, timeout=15)

    assert seen, 'хук не сработал — путь записи не перехвачен'
    for dst_name in seen:
        base = os.path.basename(str(dst_name))
        assert base.startswith(('.rtlc-part-', 'rtlc-part-')), (
            f'запись идёт сразу в финальное имя "{base}" — '
            'PACS увидит частичный файл')
    assert len(copied) == 1
    with open(os.path.join(paths['local'], copied[0]), 'rb') as f:
        assert f.read() == data


# --- Доп.: защита от петель ----------------------------------------------

def test_local_inside_remote_rejected(paths, make_rtlc):
    """Назначение внутри источника = бесконечный цикл копирования.
    Приложение обязано отказаться стартовать с такой конфигурацией."""
    inner = os.path.join(paths['remote'], 'out')
    os.makedirs(inner)
    with pytest.raises(SystemExit):
        make_rtlc(local=inner)


def test_local_equals_remote_rejected(paths, make_rtlc):
    with pytest.raises(SystemExit):
        make_rtlc(local=paths['remote'])
