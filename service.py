import os

import win32serviceutil  # ServiceFramework and commandline helper
import win32service  # Events
import servicemanager  # Simple setup and logging

from rtlc import CopyUtility, getLenSysArg
from smbclient import SmbClient


class CopyUtilityNetShare(CopyUtility):

    def __init__(self):
        super().__init__()
        if not os.path.exists(self.localPath):
            self.log.error(f'Local folder: "{self.localPath}" not found.')
            self.exit()
        self.shareIP = self.config.shareIP
        self.shareName = self.config.shareName
        self.server = self.createConnection()
        if not self.connect():
            self.exit()

    def createConnection(self):
        return SmbClient.create(self.config.userName,
                                self.config.userPassword,
                                self.config.clientMachineName,
                                self.config.remoteMachineName)

    def connect(self):
        connect, msg = SmbClient.connect(self)
        if connect:
            self.log.info(msg)
            return True
        self.log.error(msg)
        return False

    def reconnect(self):
        self.log.info('Reconnecting to the share...')
        try:
            self.server = self.createConnection()
        except Exception as exc:
            self.log.error(f'Reconnect failed: {exc}')
            return
        self.connect()

    def _retrieve(self, path, tmp):
        SmbClient.retrieve(self, path, tmp)

    def scandir(self, path, found):
        SmbClient.scandir(self, path, found)


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

        while not self.rtlc.stop_event.is_set():
            try:
                self.rtlc.walker()
            except Exception:
                self.rtlc.log.exception('Unhandled error in walker(), continuing.')
            self.rtlc.timeout()


class MyServiceFramework(win32serviceutil.ServiceFramework):

    _svc_name_ = 'RTLC'
    _svc_display_name_ = 'Remote To Local Copy'

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


if __name__ == '__main__':
    if getLenSysArg() == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MyServiceFramework)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(MyServiceFramework)