import win32serviceutil  # ServiceFramework and commandline helper
import win32service  # Events
import servicemanager  # Simple setup and logging

from rtlc import CopyUtility, getLenSysArg
from smbclient import SmbClient


class CopyUtilityNetShare(CopyUtility):

    def __init__(self):
        super().__init__()
        self.shareIP = self.config.shareIP
        self.shareName = self.config.shareName
        self.server = SmbClient.create(self.config.userName,
                                       self.config.userPassword,
                                       self.config.clientMachineName,
                                       self.config.remoteMachineName)
        self.connect()

    def connect(self):
        connect, msg = SmbClient.connect(self)
        if connect:
            self.log.info(msg)
        else:
            self.log.error(msg)
            self.exit()

    def copyfile(self, path, filename):
        return SmbClient.copyfile(self, path, filename)

    def scandir(self, path):
        SmbClient.scandir(self, path)


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