from smb.SMBConnection import SMBConnection


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

    def copyfile(self, path, filename):
        try:
            with open(filename, 'wb') as file_obj:
                self.server.retrieveFile(self.shareName, path, file_obj)
            return True
        except Exception as exc:
            print(f'[RTLC]: {exc}')
            return False

    def scandir(self, path: str):
        scanDir = self.server.listPath(self.shareName, path)
        for entry in scanDir:
            if entry.isDirectory and (entry.filename !=
                                      '.') and (entry.filename != '..'):
                self.scandir(path + entry.filename + '/')
            elif not entry.isDirectory and (entry.create_time >
                                            self.timeStamp):
                self.remoteList.append(
                    (entry.create_time, path + entry.filename))
