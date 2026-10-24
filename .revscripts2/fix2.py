from pathlib import Path
p = Path('.revscripts2/repro_work_finally.py')
t = p.read_text(encoding='utf-8').replace(
    "    setup._work(fd, '', False)",
    "    try:\n        setup._work(fd, '', False)\n    except Exception as error:\n        print('收尾抛出 :', type(error).__name__, str(error)[:28])")
p.write_text(t, encoding='utf-8')
