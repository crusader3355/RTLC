#Remote To Local Copy(RTLC)
import atexit
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time

from uuid import uuid4
from time import mktime, strptime, strftime, gmtime

from config import Config


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
            self.config.extensionFile.replace(' ', '').split(','))

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
        atexit.register(self.end)
        self.log.info('Service started.')

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
                        ) and entry.name.endswith(self.extensionFile):
                            stat = entry.stat()
                            found[entry.path] = (stat.st_size,
                                                 stat.st_mtime)
                    except OSError as exc:
                        self.log.error(
                            f"Entry '{entry.path}' scan error: {exc}")
        except Exception as exc:
            self.log.error(f"Directory '{path}' scan error: {exc}")

    def copyfile(self, path, filename):
        try:
            shutil.copy2(path, filename)
            return True
        except Exception as exc:
            self.log.error(f'Copy "{path}" -> "{filename}" failed: {exc}')
            return False

    def timeout(self):
        time.sleep(self.refreshTime)

    def exit(self):
        sys.exit()

    def end(self):
        self.log.info('Service stopped.')


if __name__ == '__main__':
    rtlc = CopyUtility()
    while True:
        rtlc.walker()
        rtlc.timeout()
