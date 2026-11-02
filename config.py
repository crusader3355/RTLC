import configparser
import os


class Config:

    def __init__(self, ini: str):
        self.ini = ini
        self.config = configparser.ConfigParser()
        self.startDate: str = '2000-01-01 00:00'
        self.timeStamp: float = 0.0
        self.refreshTime: int = 60
        self.extensionFile: str = ''
        self.stabilizeTime: float = 5.0
        self.remotePath: str = './remote'
        self.localPath: str = './local'
        self.useNetShare: bool = False
        self.shareIP: str = ''
        self.sharePort: int = 445
        self.shareName: str = ''
        self.userName: str = ''
        self.userPassword: str = ''
        self.clientMachineName: str = ''
        self.remoteMachineName: str = ''
        self.logsLevel: str = 'INFO'
        self.logsSize: int = 2
        self.logsBackups: int = 5
        self.webHost: str = '127.0.0.1'
        self.webPort: int = 8765
        self.webApiKey: str = ''
        self.webTrustLoopback: bool = True
        self.toFolder: bool = True
        self.toDicom: bool = False
        self.dicomHost: str = ''
        self.dicomPort: int = 104
        self.dicomCalledAet: str = ''
        self.dicomCallingAet: str = 'RTLC'
        self.dicomTimeout: int = 30

    def getstring(self, section, option, fallback):
        string = self.config.get(section, option, fallback=fallback)
        if string:
            return string
        else:
            return fallback

    def getfloat(self, section, option, fallback):
        try:
            return self.config.getfloat(section, option, fallback=fallback)
        except ValueError:
            return fallback

    def getint(self, section, option, fallback):
        try:
            return self.config.getint(section, option, fallback=fallback)
        except ValueError:
            return fallback

    def getboolean(self, section, option, fallback):
        try:
            return self.config.getboolean(section, option, fallback=fallback)
        except ValueError:
            return fallback

    def load(self):
        self.config.read(self.ini)
        self.startDate = self.getstring('Options',
                                        'start_date',
                                        fallback=self.startDate)
        self.timeStamp = self.getfloat('Options',
                                       'last_file',
                                       fallback=self.timeStamp)
        self.refreshTime = self.getint('Options',
                                       'refresh_time',
                                       fallback=self.refreshTime)
        self.extensionFile = self.getstring('Options',
                                            'extension_file',
                                            fallback=self.extensionFile)
        self.stabilizeTime = self.getfloat('Options',
                                           'stabilize_time',
                                           fallback=self.stabilizeTime)
        self.remotePath = self.getstring('Paths',
                                         'remote',
                                         fallback=self.remotePath)
        self.localPath = self.getstring('Paths',
                                        'local',
                                        fallback=self.localPath)
        self.useNetShare = self.getboolean('NetShare',
                                           'use_network_share',
                                           fallback=self.useNetShare)
        self.shareIP = self.getstring('NetShare',
                                      'share_ip',
                                      fallback=self.shareIP)
        self.sharePort = self.getint('NetShare',
                                     'share_port',
                                     fallback=self.sharePort)
        self.shareName = self.getstring('NetShare',
                                        'share_name',
                                        fallback=self.shareName)
        self.userName = self.getstring('NetShare',
                                       'user_name',
                                       fallback=self.userName)
        self.userPassword = self.getstring('NetShare',
                                           'user_password',
                                           fallback=self.userPassword)
        self.clientMachineName = self.getstring(
            'NetShare', 'client_machine_name', fallback=self.clientMachineName)
        self.remoteMachineName = self.getstring(
            'NetShare', 'remote_machine_name', fallback=self.remoteMachineName)
        self.logsLevel = self.getstring('Logs',
                                        'level',
                                        fallback=self.logsLevel)
        self.logsSize = self.getint('Logs', 'size', fallback=self.logsSize)
        self.logsBackups = self.getint('Logs',
                                       'backups',
                                       fallback=self.logsBackups)
        self.webHost = self.getstring('Web', 'host', fallback=self.webHost)
        self.webPort = self.getint('Web', 'port', fallback=self.webPort)
        self.webApiKey = self.getstring('Web',
                                        'api_key',
                                        fallback=self.webApiKey)
        self.webTrustLoopback = self.getboolean(
            'Web', 'trust_loopback', fallback=self.webTrustLoopback)
        self.toFolder = self.getboolean('Delivery',
                                        'to_folder',
                                        fallback=self.toFolder)
        self.toDicom = self.getboolean('Delivery',
                                       'to_dicom',
                                       fallback=self.toDicom)
        self.dicomHost = self.getstring('Dicom',
                                        'host',
                                        fallback=self.dicomHost)
        self.dicomPort = self.getint('Dicom', 'port', fallback=self.dicomPort)
        self.dicomCalledAet = self.getstring('Dicom',
                                             'called_aet',
                                             fallback=self.dicomCalledAet)
        self.dicomCallingAet = self.getstring('Dicom',
                                              'calling_aet',
                                              fallback=self.dicomCallingAet)
        self.dicomTimeout = self.getint('Dicom',
                                        'timeout',
                                        fallback=self.dicomTimeout)

    def save(self) -> str:
        self.config['Options'] = {
            'start_date': self.startDate,
            'last_file': self.timeStamp,
            'refresh_time': self.refreshTime,
            'extension_file': self.extensionFile,
            'stabilize_time': self.stabilizeTime
        }
        self.config['Paths'] = {
            'remote': self.remotePath,
            'local': self.localPath
        }
        self.config['NetShare'] = {
            'use_network_share': self.useNetShare,
            'share_ip': self.shareIP,
            'share_port': self.sharePort,
            'share_name': self.shareName,
            'user_name': self.userName,
            'user_password': self.userPassword,
            'client_machine_name': self.clientMachineName,
            'remote_machine_name': self.remoteMachineName
        }
        self.config['Logs'] = {
            'level': self.logsLevel,
            'size': self.logsSize,
            'backups': self.logsBackups
        }
        self.config['Web'] = {
            'host': self.webHost,
            'port': self.webPort,
            'api_key': self.webApiKey,
            'trust_loopback': self.webTrustLoopback
        }
        self.config['Delivery'] = {
            'to_folder': self.toFolder,
            'to_dicom': self.toDicom
        }
        self.config['Dicom'] = {
            'host': self.dicomHost,
            'port': self.dicomPort,
            'called_aet': self.dicomCalledAet,
            'calling_aet': self.dicomCallingAet,
            'timeout': self.dicomTimeout
        }
        try:
            tmp = f'{self.ini}.tmp'
            with open(tmp, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
            os.replace(tmp, self.ini)
            return (f'Building configuration file completed successfully.')
        except Exception as exc:
            return (f'Error building configuration file "{exc}": {self.ini}')
