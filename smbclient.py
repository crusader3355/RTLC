import os

from smb.SMBConnection import SMBConnection

from rtlc import TEMP_PREFIX


class SmbClient():

    def __init__(self, shareIP, userName, userPassword, shareName,
                 clientMachineName, remoteMachineName):
        self.shareIP = shareIP
        self.userName = userName
        self.userPassword = userPassword
        self.shareName = shareName
        self.clienthost = clientMachineName
        self.remotehost = remoteMachineName

    @staticmethod
    def create(userName, userPassword, clientMachineName, remoteMachineName):
        return SMBConnection(userName,
                             userPassword,
                             clientMachineName,
                             remoteMachineName,
                             use_ntlm_v2=True)

    def connect(self):
        try:
            if self.server.connect(self.shareIP, 139):
                return True, 'Authentication successfully.'
            else:
                return False, 'Failed authentication.'
        except Exception as exc:
            return False, f'{exc}'

    def retrieve(self, path, tmp):
        with open(tmp, 'wb') as file_obj:
            self.server.retrieveFile(self.shareName, path, file_obj)
            file_obj.flush()
            os.fsync(file_obj.fileno())

    def scandir(self, path: str, found: dict):
        try:
            scanDir = self.server.listPath(self.shareName, path)
        except Exception as exc:
            self.log.error(f"Share '{path}' scan error: {exc}")
            self.reconnect()
            return
        for entry in scanDir:
            try:
                if entry.isDirectory:
                    if entry.filename not in ('.', '..'):
                        self.scandir(path + entry.filename + '/', found)
                else:
                    full = path + entry.filename
                    if entry.filename.startswith(TEMP_PREFIX):
                        continue
                    if not full.lower().endswith(self.extensionFile):
                        continue
                    found[full] = (entry.file_size, entry.last_write_time)
            except Exception as exc:
                self.log.error(
                    f"Share entry '{entry.filename}' scan error: {exc}")
