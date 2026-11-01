#Remote To Local Copy(RTLC) — единая точка входа продакшен-сборки
import sys

SERVICE_COMMANDS = ('install', 'start', 'stop', 'restart', 'remove',
                    'update', 'debug', 'status')

USAGE = """Remote To Local Copy (RTLC)

  rtlc.exe                 Web UI (по умолчанию, http://127.0.0.1:8765/)
  rtlc.exe gui             то же самое
  rtlc.exe run             консольный цикл копирования (без Web UI)
  rtlc.exe service         режим службы Windows (вызывается SCM)
  rtlc.exe install|start|stop|restart|remove
                           управление службой Windows (SMB-режим,
                           из командной строки администратора)
"""


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else 'gui'

    if cmd in ('gui', '--gui'):
        import webui
        webui.main()
    elif cmd == 'run':
        from rtlc import CopyUtility
        CopyUtility().run_forever()
    elif cmd == 'service':
        # pywin32 ждёт команды начиная с argv[1]
        sys.argv = [sys.argv[0]] + args[1:]
        import service
        service.main()
    elif cmd in SERVICE_COMMANDS:
        # обратная совместимость: rtlc.exe install / start / stop ...
        import service
        service.main()
    elif cmd in ('-h', '--help', 'help', '/?'):
        print(USAGE)
    else:
        print(USAGE)
        sys.exit(2)


if __name__ == '__main__':
    main()
