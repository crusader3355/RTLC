#Remote To Local Copy(RTLC) — SMB2/3 клиент (smbprotocol, замена pysmb/SMB1)
import os
import shutil

import smbclient as smb


class SmbClient():
    """Подключение к SMB-шаре: рекурсивное сканирование и чтение файлов."""

    def __init__(self, host, share, username, password, port=445):
        self.host = host
        self.share = share.strip('/\\')
        # NTLM из-под SYSTEM (служба LocalSystem): незквалифицированное
        # локальное имя даёт LOGON_DENIED — нужен квалификатор '.\'.
        if '\\' not in username and '@' not in username:
            username = '.\\' + username
        self.username = username
        self.password = password
        self.port = port
        self.log = None  # назначает владелец (CopyUtilityNetShare)

    def _unc(self, rel):
        rel = (rel or '').strip('/\\').replace('/', '\\')
        base = f'\\\\{self.host}\\{self.share}'
        return f'{base}\\{rel}' if rel else base

    def connect(self):
        try:
            smb.register_session(self.host,
                                 username=self.username,
                                 password=self.password,
                                 port=self.port)
            return True, f'Authenticated to //{self.host}/{self.share} (SMB2/3).'
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'

    def reconnect(self):
        try:
            smb.delete_session(self.host)
        except Exception:
            pass
        ok, msg = self.connect()
        if self.log:
            (self.log.info if ok else self.log.error)(f'SMB reconnect: {msg}')
        return ok

    def retrieve(self, path, tmp):
        with smb.open_file(self._unc(path), mode='rb') as src, \
                open(tmp, 'wb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())

    def scandir(self, rel, found, accept):
        try:
            with smb.scandir(self._unc(rel)) as it:
                entries = list(it)
        except Exception as exc:
            if self.log:
                self.log.error(f"Share '{rel}' scan error: {exc}")
            self.reconnect()
            return
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in ('.', '..'):
                        self.scandir(f'{rel.rstrip("/")}/{entry.name}', found,
                                     accept)
                    continue
                if not entry.is_file() or not accept(entry.name):
                    continue
                stat = entry.stat()
                full = (f'{rel.rstrip("/")}/{entry.name}'
                        if rel != '/' else f'/{entry.name}')
                found[full] = (stat.st_size, stat.st_mtime)
            except Exception as exc:
                if self.log:
                    self.log.error(
                        f"Share entry '{entry.name}' scan error: {exc}")
