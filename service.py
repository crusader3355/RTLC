import os

import win32serviceutil  # ServiceFramework and commandline helper
import win32service  # Events
import servicemanager  # Simple setup and logging

from rtlc import CopyUtility, getLenSysArg
from smb_client import SmbClient


class CopyUtilityNetShare(CopyUtility):

    def __init__(self):
        super().__init__()
        if self.toFolder and not os.path.exists(self.localPath):
            self.log.error(f'Local folder: "{self.localPath}" not found.')
            self.exit()
        self.smb = SmbClient(self.config.shareIP, self.config.shareName,
                             self.config.userName, self.config.userPassword,
                             self.config.sharePort)
        self.smb.log = self.log
        if not self.connect():
            self.exit()

    def connect(self):
        ok, msg = self.smb.connect()
        (self.log.info if ok else self.log.error)(msg)
        return ok

    def reconnect(self):
        self.smb.reconnect()

    def _retrieve(self, path, tmp):
        self.smb.retrieve(path, tmp)

    def _localSource(self, path):
        # Источник на SMB-шаре: материализуем во временный локальный файл
        # для C-STORE; удаляется сразу после отправки.
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix='rtlc-net-', suffix='.dcm')
        os.close(fd)
        try:
            self._retrieve(path, tmp)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

        def cleanup():
            try:
                os.remove(tmp)
            except OSError:
                pass

        return tmp, cleanup

    def scandir(self, path, found):
        self.smb.scandir(path, found, self._acceptName)

    def _acceptName(self, name):
        return self.matchName(name)

    def sourceExists(self, path):
        return self.smb.exists(path)


class MyService:

    def __init__(self):
        self.rtlc = None

    def stop(self):
        if self.rtlc is not None:
            self.rtlc.stop_event.set()

    def run(self):
        servicemanager.LogInfoMsg('Service started.')

        #rtlc = CopyUtility()

        self.rtlc = CopyUtilityNetShare()
        self.rtlc.run_forever()


class MyServiceFramework(win32serviceutil.ServiceFramework):

    _svc_name_ = 'RTLC'
    _svc_display_name_ = 'Remote To Local Copy'
    # SCM стартует exe с этим аргументом — лаунчер переводит процесс
    # в режим службы (иначе запуск без аргументов открыл бы Web UI)
    _exe_args_ = 'service'

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        service_impl = getattr(self, 'service_impl', None)
        if service_impl is not None:
            service_impl.stop()
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        self.service_impl = MyService()
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        self.service_impl.run()


def main():
    if getLenSysArg() == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MyServiceFramework)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(MyServiceFramework)


if __name__ == '__main__':
    main()