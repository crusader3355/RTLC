# Remote To Local Copy(RTLC)
    1. Install to Windows(x64):
        a. Скачать 'dist/rtlc.exe'.
        b. Закинуть бинарь в рабочую директорию (Например: 'C:/rtlc/').
        c. Открыть командную строку от имени администратора и запустить бинарь с параметром install (Пример: 'C:/rtlc/rtlc.exe install')
        e. При первом запуске утилиты (Через 'Службы' или комнадную строку (Пример: 'C:/rtlc/rtlc.exe start')) она сгенерирует конфигурационный файл в директорию с бинарем (Пример: 'C:/rtlc/config.ini'). 
    2. Install to RedOS:
        a. Скачать файлы: 'rtlc.py, config.py, install_rtlc.sh'.
        b. Закинуть файлы в домашнюю директорию (Например: '/home/lins/').
        c. Выдать права: 'chmod 755 /home/lins/install_rtlc.sh', запустить его и следовать инструкции установочного скрипта.
    Config.ini:
        [Options]
        1. start_date - дата и время создания файлов от которых начинать сканирование (игнорируется при заданном параметре last_file).
        2. last_file - timestamp от которого начинать сканирование файлов, подставляется автоматически.
        3. refresh_time - интервал между сканированием файлов (в секундах)
        4. extenstion_file - фильтр по расширениям файлов (Пример: '.dcm, .png'), если не нужен фильтр оставляем пустым
        [Paths]
        1. remote - путь до удалённой папки
        2. local - путь до локальной папки
        [Logs]
        1. level - уровень логирования
        2. size - максимальный размер файла логов (в мегабайтах)
        3. backups - количество бекапов логов (создаются в случае достижения максимального размера основного файла логирования)
