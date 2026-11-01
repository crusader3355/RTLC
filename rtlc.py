#Remote To Local Copy(RTLC)
import atexit
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

        if not self.config.useNetShare:
            notPath = False
            if not os.path.exists(self.remotePath):
                self.log.error(
                    f'Remote folder: "{self.remotePath}" not found.')
                notPath = True
            if not os.path.exists(self.localPath):
                self.log.error(f'Local folder: "{self.localPath}" not found.')
                notPath = True
            if notPath:
                self.exit()
            if self.pathContains(self.remotePath, self.localPath):
                self.log.error(
                    f'Local folder "{self.localPath}" is the remote folder or inside it: '
                    'the service would copy its own output forever. Refusing to start.'
                )
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
            done = self.state['files'].get(path)
            if done and done.get('size') == size and done.get(
                    'mtime') == mtime:
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
            f'Found {len(backlog)} files to copy ({len(ready)} stable).')
        copied_ok = set()
        for path, size, mtime in ready:
            if self.stop_event.is_set():
                self.log.info('Stop requested, copy loop interrupted.')
                break
            newName = f'{uuid4()}{os.path.splitext(path)[1]}'
            if self.copyfile(path, f'{self.localPath}/{newName}'):
                resent = self.state['files'].get(path) is not None
                self.state['files'][path] = {
                    'size': size,
                    'mtime': mtime,
                    'dest': newName
                }
                self.saveState()
                copied_ok.add(path)
                self.log.info(
                    f'File "{newName} << {path}" copied to local folder'
                    f'{" (re-sent, content changed)" if resent else ""}.')
            else:
                self.log.error(f"File '{path}' not copied to local folder.")
        self.remoteList = [p for (p, _, _) in backlog if p not in copied_ok]

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

    def exit(self):
        sys.exit()

    def end(self):
        self.log.info('Service stopped.')


if __name__ == '__main__':
    rtlc = CopyUtility()
    while not rtlc.stop_event.is_set():
        try:
            rtlc.walker()
        except Exception:
            rtlc.log.exception('Unhandled error in walker(), continuing.')
        rtlc.timeout()
