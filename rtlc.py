#Remote To Local Copy(RTLC)
import atexit
import collections
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time

from uuid import uuid4
from time import mktime, strptime, strftime, gmtime

from config import Config

TEMP_PREFIX = '.rtlc-part-'
COPY_BUFFER_SIZE = 1024 * 1024


def getModulePath() -> str:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(__file__)


def getLenSysArg() -> int:
    return len(sys.argv)


class CopyUtility():

    def __init__(self):
        os.chdir(getModulePath())

        FORMAT = '[%(asctime)s]: %(levelname)s - %(message)s'
        FOLDER_LOGS = 'Logs'
        NAME_LOG = 'rtlc.log'
        NAME_QUEUE = 'rtlc.queue'
        NAME_STATE = 'rtlc.state.json'

        if not os.path.exists(FOLDER_LOGS):
            try:
                os.mkdir(FOLDER_LOGS)
            except Exception as exc:
                print(
                    f'[RTLC ERROR]: Failed to create log directory: "{os.path.abspath(FOLDER_LOGS)}"'
                )
                print(f'{exc}')
                self.exit()

        self.config = Config('config.ini')
        self.config.load()

        SIZE_LOG = self.config.logsSize * 1024 * 1024

        mainHandler = logging.handlers.RotatingFileHandler(
            f'{FOLDER_LOGS}/{NAME_LOG}',
            maxBytes=SIZE_LOG,
            backupCount=self.config.logsBackups,
            encoding='utf-8')
        mainHandler.setFormatter(logging.Formatter(FORMAT))

        self.log = logging.getLogger('rltc')
        self.log.addHandler(mainHandler)

        self.config.logsLevel = self.config.logsLevel.upper()
        if self.config.logsLevel not in [
                'NOTSET', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        ]:
            self.log.error(
                f'Uncorrect logging level: "{self.config.logsLevel}". Available values: "NOTSET, DEBUG, INFO, WARNING, ERROR, CRITICAL".'
            )
            self.exit()

        self.log.setLevel(self.config.logsLevel)
        self.log.info('Starting service...')

        if not os.path.exists(self.config.ini):
            self.log.warning(
                f'File "{self.config.ini}" not found, modify the generated config file ("{os.path.abspath(self.config.ini)}").'
            )
            result = self.config.save()
            print(result)
            self.exit()

        self.remotePath = self.config.remotePath
        self.localPath = self.config.localPath

        # Стоки доставки: папка и/или DICOM-узел. Источник только читается:
        # движок не пишет и не удаляет файлы в remotePath ни при каких
        # обстоятельствах.
        self.toFolder = self.config.toFolder
        self.toDicom = self.config.toDicom
        self.dicomHost = self.config.dicomHost
        self.dicomPort = self.config.dicomPort
        self.dicomCalledAet = self.config.dicomCalledAet
        self.dicomCallingAet = self.config.dicomCallingAet
        self.dicomTimeout = self.config.dicomTimeout

        if not self.toFolder and not self.toDicom:
            self.log.error(
                'Both delivery sinks are disabled ([Delivery] to_folder, '
                'to_dicom) — nothing to do.')
            self.exit()

        if not self.config.useNetShare:
            notPath = False
            if not os.path.exists(self.remotePath):
                self.log.error(
                    f'Remote folder: "{self.remotePath}" not found.')
                notPath = True
            if self.toFolder and not os.path.exists(self.localPath):
                self.log.error(f'Local folder: "{self.localPath}" not found.')
                notPath = True
            if notPath:
                self.exit()
            if self.toFolder and self.pathContains(self.remotePath,
                                                   self.localPath):
                self.log.error(
                    f'Local folder "{self.localPath}" is the remote folder or inside it: '
                    'the service would copy its own output forever. Refusing to start.'
                )
                self.exit()

        if self.toDicom:
            if not self.dicomHost or not self.dicomCalledAet:
                self.log.error(
                    'DICOM delivery is enabled but [Dicom] host/called_aet '
                    'are not set.')
                self.exit()
            try:
                import dicom_sender  # noqa: F401
            except ImportError as exc:
                self.log.error(
                    f'DICOM delivery is enabled but pynetdicom/pydicom are '
                    f'not available: {exc}')
                self.exit()

        self.timeStamp = self.config.timeStamp

        if not self.timeStamp:
            try:
                self.timeStamp = mktime(
                    strptime(self.config.startDate, '%Y-%m-%d %H:%M'))
            except ValueError:
                self.log.error(
                    f'Uncorrect start date: "{self.config.startDate}", correct format: "2000-01-01 00:00"'
                )
                self.exit()

        self.refreshTime = self.config.refreshTime
        self.stabilizeTime = self.config.stabilizeTime
        self.extensionFile = tuple(
            ext.lower()
            for ext in self.config.extensionFile.replace(' ', '').split(','))

        self.log.info(f'    Logs level: {self.config.logsLevel}')
        self.log.info(f'    Remote folder: "{self.remotePath}"')
        self.log.info(f'    Local folder: "{self.localPath}"')
        self.log.info(f'    Refresh time: {self.refreshTime}')
        self.log.info(f'    Stabilize time: {self.stabilizeTime}')
        self.log.info(f'    Delivery: folder={self.toFolder}'
                      f'{f" ({self.localPath})" if self.toFolder else ""}, '
                      f'dicom={self.toDicom}'
                      f'{f" ({self.dicomCalledAet}@{self.dicomHost}:{self.dicomPort})" if self.toDicom else ""}')
        self.log.info(f'    Extension file: {self.extensionFile}')
        self.log.info(f'    Start date: {self.config.startDate}')
        self.log.info(
            f'    Last file: {strftime("%Y-%m-%d %H:%M:%S", gmtime(self.timeStamp))} (timeStamp: {self.timeStamp})'
        )

        self.nameState = NAME_STATE
        self.loadState()

        if os.path.exists(NAME_QUEUE):
            self.log.info(
                f'Legacy queue file "{NAME_QUEUE}" is no longer used, '
                'unprocessed files are detected via the state ledger.')
            try:
                os.remove(NAME_QUEUE)
            except Exception as exc:
                self.log.error(f'REMOVE {exc}')

        self.remoteList = []
        self.pending = {}
        self.stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self.stats = {
            'cycles': 0,
            'copied': 0,
            'failed': 0,
            'dicom_sent': 0,
            'dicom_failed': 0,
            'last_cycle_at': None,
            'last_error': None,
            'started_at': time.time()
        }
        self.recent = collections.deque(maxlen=50)
        if self.toFolder:
            self.cleanupStaleParts()
        atexit.register(self.end)
        self.log.info('Service started.')

    @staticmethod
    def pathContains(parent, child) -> bool:
        try:
            parent = os.path.realpath(parent)
            child = os.path.realpath(child)
            return parent == child or os.path.commonpath(
                (parent, child)) == parent
        except ValueError:
            return False

    def loadState(self):
        self.state = {'files': {}}
        if os.path.exists(self.nameState):
            try:
                with open(self.nameState, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(
                        data.get('files'), dict):
                    self.state = data
                # Миграция формата до 2.0: {dest, copied_at} -> folder-сток
                for meta in self.state['files'].values():
                    if isinstance(meta, dict) and 'folder' not in meta and 'dest' in meta:
                        meta['folder'] = {
                            'dest': meta.pop('dest'),
                            'done_at': meta.pop('copied_at', None)
                        }
                self.log.info(
                    f'State ledger loaded: {len(self.state["files"])} copied files on record.'
                )
            except Exception as exc:
                self.log.error(
                    f'State file "{self.nameState}" is corrupted ({exc}), starting with an empty ledger: '
                    'previously copied files may be copied again (no data loss).'
                )

    def saveState(self):
        tmp = f'{self.nameState}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f)
            os.replace(tmp, self.nameState)
        except Exception as exc:
            self.log.error(f'State file "{self.nameState}" save error: {exc}')

    def walker(self):
        self.log.info(f'Refresh')
        now = time.time()
        found = {}
        self.scandir(self.remotePath, found)

        backlog = []
        for path, (size, mtime) in sorted(found.items()):
            if self._deliveryDone(self.state['files'].get(path), size,
                                  mtime):
                continue
            backlog.append((path, size, mtime))

        # Стабилизация источника: копируем только файлы, чьи размер и
        # mtime не изменились между сканированиями и которые «отлежались»
        # не меньше stabilizeTime с момента первого обнаружения.
        ready = []
        for path, size, mtime in backlog:
            prev = self.pending.get(path)
            if prev is None or prev[0] != size or prev[1] != mtime:
                if prev is None:
                    self.log.debug(f'Pending (first seen): "{path}"')
                else:
                    self.log.debug(f'Pending (still changing): "{path}"')
                self.pending[path] = (size, mtime, now)
                continue
            if now - prev[2] >= self.stabilizeTime:
                ready.append((path, size, mtime))
        for path in list(self.pending):
            if path not in found:
                del self.pending[path]

        self.log.info(
            f'Found {len(backlog)} files to deliver ({len(ready)} stable).')
        delivered = set()
        for path, size, mtime in ready:
            if self.stop_event.is_set():
                self.log.info('Stop requested, delivery loop interrupted.')
                break
            if self.deliver(path, size, mtime):
                delivered.add(path)
        self.remoteList = [p for (p, _, _) in backlog if p not in delivered]
        self.stats['cycles'] += 1
        self.stats['last_cycle_at'] = time.time()
        self.log.info(
            f'Cycle done: {len(found)} found, {len(backlog)} to deliver, '
            f'{len(delivered)} delivered, {len(self.remoteList)} in backlog.')

        if found:
            self.timeStamp = max(mtime for _, mtime in found.values())
            self.config.timeStamp = self.timeStamp
            self.config.save()

    def scandir(self, path: str, found: dict):
        try:
            with os.scandir(path) as scanDir:
                for entry in scanDir:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            self.scandir(entry.path, found)
                        elif entry.is_file(
                                follow_symlinks=False
                        ) and not entry.name.startswith(
                                TEMP_PREFIX) and entry.name.lower().endswith(
                                self.extensionFile):
                            stat = entry.stat()
                            found[entry.path] = (stat.st_size,
                                                 stat.st_mtime)
                    except OSError as exc:
                        self.log.error(
                            f"Entry '{entry.path}' scan error: {exc}")
        except Exception as exc:
            self.log.error(f"Directory '{path}' scan error: {exc}")

    def _deliveryDone(self, entry, size, mtime) -> bool:
        """Файл завершён, когда запись в журнале соответствует текущим
        size/mtime и все включённые стоки его приняли (permanent-ошибка
        DICOM тоже завершает файл — ретрай бессмысленен)."""
        if not entry or entry.get('size') != size or entry.get(
                'mtime') != mtime:
            return False
        if self.toFolder and not entry.get('folder'):
            return False
        if self.toDicom:
            d = entry.get('dicom') or {}
            if not d.get('sent') and not d.get('permanent'):
                return False
        return True

    def deliver(self, path, size, mtime) -> bool:
        """Доставка файла во все включённые стоки; в журнал пишется
        прогресс по каждому стоку отдельно (ретраится только упавший)."""
        entry = self.state['files'].get(path)
        resent = bool(entry) and (entry.get('size') != size
                                  or entry.get('mtime') != mtime)
        entry = dict(entry or {})
        if resent or entry.get('size') != size or entry.get(
                'mtime') != mtime:
            entry = {'size': size, 'mtime': mtime}
        changed = False

        dicom_src = None
        if self.toFolder and not entry.get('folder'):
            newName = f'{uuid4()}{os.path.splitext(path)[1]}'
            started = time.time()
            if self.copyfile(path, os.path.join(self.localPath, newName)):
                done_at = time.time()
                entry['folder'] = {'dest': newName, 'done_at': done_at}
                changed = True
                self.stats['copied'] += 1
                self.recent.appendleft({
                    'src': path,
                    'dest': newName,
                    'size': size,
                    'duration': round(done_at - started, 3),
                    'copied_at': done_at,
                    'resent': resent
                })
                self.log.info(
                    f'File "{newName} << {path}" copied to local folder: '
                    f'{size} bytes in {done_at - started:.2f}s'
                    f'{" (re-sent, content changed)" if resent else ""}.')
            else:
                self.stats['failed'] += 1
                self.log.error(f"File '{path}' not copied to local folder.")
        if self.toFolder and entry.get('folder'):
            candidate = os.path.join(self.localPath, entry['folder']['dest'])
            if os.path.exists(candidate):
                dicom_src = candidate

        if self.toDicom:
            d = dict(entry.get('dicom') or {})
            if not d.get('sent') and not d.get('permanent'):
                d['attempts'] = d.get('attempts', 0) + 1
                if dicom_src is not None:
                    ok, err, permanent = self.sendViaDicom(dicom_src,
                                                           already_local=True)
                else:
                    ok, err, permanent = self.sendViaDicom(path)
                if ok:
                    d['sent'] = True
                    d['sent_at'] = time.time()
                    d.pop('last_error', None)
                    d.pop('permanent', None)
                    self.stats['dicom_sent'] += 1
                    self.log.info(
                        f'File "{path}" sent to DICOM node '
                        f'{self.dicomCalledAet}@{self.dicomHost}:{self.dicomPort}'
                        f'{f" ({err})" if err else ""}.')
                else:
                    d['last_error'] = err
                    if permanent:
                        d['permanent'] = True
                    self.stats['dicom_failed'] += 1
                    self.log.error(
                        f"File '{path}' not sent to DICOM node "
                        f'{self.dicomCalledAet}@{self.dicomHost}:{self.dicomPort}: '
                        f'{err}{" (permanent, will not retry)" if permanent else ""}')
                entry['dicom'] = d
                changed = True

        if changed:
            self.state['files'][path] = entry
            self.saveState()
        return self._deliveryDone(entry, size, mtime)

    def sendViaDicom(self, path, already_local=False) -> tuple:
        """C-STORE одного файла. Для SMB-источника файл предварительно
        материализуется во временный локальный файл (удаляется сразу)."""
        try:
            import dicom_sender
        except ImportError as exc:
            return False, f'pynetdicom/pydicom not available: {exc}', True
        if already_local:
            return dicom_sender.send_file(path, self.dicomHost,
                                          self.dicomPort, self.dicomCalledAet,
                                          self.dicomCallingAet,
                                          self.dicomTimeout)
        try:
            local, cleanup = self._localSource(path)
        except Exception as exc:
            return False, f'materialize failed: {type(exc).__name__}: {exc}', False
        try:
            return dicom_sender.send_file(local, self.dicomHost,
                                          self.dicomPort, self.dicomCalledAet,
                                          self.dicomCallingAet,
                                          self.dicomTimeout)
        finally:
            cleanup()

    def _localSource(self, path):
        # Локальный источник читается напрямую; SMB-режим переопределяет.
        return path, lambda: None

    def copyfile(self, path, filename):
        # Атомарная запись: копия во временное имя в том же томе, затем
        # os.replace — PACS никогда не видит частично скопированный файл.
        tmp = os.path.join(self.localPath, f'{TEMP_PREFIX}{uuid4()}')
        try:
            self._retrieve(path, tmp)
            os.replace(tmp, filename)
            return True
        except Exception as exc:
            self.log.error(f'Copy "{path}" -> "{filename}" failed: {exc}')
            return False
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _retrieve(self, path, tmp):
        with open(path, 'rb') as fsrc, open(tmp, 'wb') as fdst:
            shutil.copyfileobj(fsrc, fdst, COPY_BUFFER_SIZE)
            fdst.flush()
            os.fsync(fdst.fileno())
        shutil.copystat(path, tmp)

    def cleanupStaleParts(self):
        try:
            for name in os.listdir(self.localPath):
                if name.startswith(TEMP_PREFIX):
                    os.remove(os.path.join(self.localPath, name))
                    self.log.info(
                        f'Removed stale partial copy: "{name}"')
        except Exception as exc:
            self.log.error(
                f'Stale partial cleanup in "{self.localPath}" failed: {exc}')

    def timeout(self):
        self.stop_event.wait(self.refreshTime)

    def walk_cycle(self):
        # Один цикл сканирования/копирования; блокировка не пускает
        # параллельный "сканировать сейчас" из веб-интерфейса.
        with self._cycle_lock:
            self.walker()

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.walk_cycle()
            except Exception as exc:
                self.stats['last_error'] = f'{type(exc).__name__}: {exc}'
                self.log.exception('Unhandled error in walker(), continuing.')
            self.timeout()

    def status(self) -> dict:
        return {
            'remotePath': self.remotePath,
            'localPath': self.localPath,
            'refreshTime': self.refreshTime,
            'stabilizeTime': self.stabilizeTime,
            'extensionFile': [e for e in self.extensionFile if e],
            'useNetShare': self.config.useNetShare,
            'delivery': {
                'toFolder': self.toFolder,
                'toDicom': self.toDicom,
                'dicomNode': f'{self.dicomCalledAet}@{self.dicomHost}:{self.dicomPort}'
                if self.toDicom else None
            },
            'backlog': len(self.remoteList),
            'stabilizing': len(self.pending),
            'ledgerSize': len(self.state['files']),
            'stats': dict(self.stats),
            'recent': list(self.recent)
        }

    def exit(self):
        sys.exit()

    def end(self):
        self.log.info('Service stopped.')


if __name__ == '__main__':
    rtlc = CopyUtility()
    rtlc.run_forever()
