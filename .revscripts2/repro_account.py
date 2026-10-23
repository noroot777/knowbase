import json, os, sys
from pathlib import Path

TMP = Path(r'C:\Users\fanda\AppData\Local\Temp\kbrev2')
os.environ.update(
    USERPROFILE=str(TMP / 'fakehome'), HOME=str(TMP / 'fakehome'),
    APPDATA=str(TMP / 'fakeappdata'),
    KB_WECHAT_DATABASE_ROOT=str(TMP / 'root'),
    RION_WECHAT_READER_CONFIG=str(TMP / 'cfg/config.json'),
    KB_WECHAT_RUNTIME=str(TMP / 'runtime'),
)
os.environ.pop('KB_WECHAT_KEYS_FILE', None)
sys.path.insert(0, r'D:\ai_workspace\knowbase\.worktrees\WJ1Uxb6zVWbJ')

import kb
print("kb from:", kb.__file__)
from kb import wechat_windows as win

REAL = [r'C:\Users\fanda\.local\share\knowbase\wechat\windows-reader\venv\Scripts\python.exe',
        r'C:\Users\fanda\.local\share\knowbase\wechat\windows-reader\reader_launcher.py']
win.prepare_reader = lambda progress: REAL      # 只跳过下载/建 venv,读取器本身是真的

print('roots =', list(win.account_roots()))
events = []
win.connect(lambda state, message, **extra: events.append({'state': state, 'message': message, **extra}),
            '', acquire=False)
print(json.dumps(events, ensure_ascii=False, indent=1))
