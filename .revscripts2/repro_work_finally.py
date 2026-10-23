import json, os, sys, tempfile, threading, time
from pathlib import Path
from unittest.mock import patch

tmp = tempfile.mkdtemp()
os.environ['KB_WECHAT_RUNTIME'] = tmp
sys.path.insert(0, r'D:\ai_workspace\knowbase\.worktrees\WJ1Uxb6zVWbJ')
from kb import wechat_setup as setup

print('--- A. 作业成功收尾那一刻状态文件读不出来 ---')
fd = setup._lock()
real_read = setup._read
calls = {'n': 0}

def read_once_then_fail(*a, **k):
    calls['n'] += 1
    if calls['n'] > 1:          # 第一次给 _connect 用,收尾那次失败
        raise PermissionError('transient')
    return real_read(*a, **k)

def fake_connect(progress, account_id, acquire):
    progress('ready', '绿泡泡数据库已验证可读。', ready=True)

with patch.object(setup, '_connect', fake_connect), \
     patch.object(Path, 'read_text', side_effect=PermissionError('transient')), \
     patch.object(setup.time, 'sleep'):
    try:
        setup._work(fd, '', False)
    except Exception as error:
        print('收尾抛出 :', type(error).__name__, str(error)[:28])

print('落盘内容 :', setup._state_file().read_text(encoding='utf-8'))
print('snapshot :', json.dumps({k: v for k, v in setup.snapshot().items() if k in ('state', 'running', 'ready')}, ensure_ascii=False))

print()
print('--- B. 状态文件坏掉之后,还点得动「检查并接入」吗 ---')
setup._state_file().write_text('{', encoding='utf-8')
s = setup.snapshot()
print('snapshot :', json.dumps({k: s.get(k) for k in ('state', 'running', 'can_connect', 'retryable')}, ensure_ascii=False))
print('前端判据 : wechat-check.disabled =', bool(s.get('running')) or s.get('can_connect') is False)
with patch.object(setup, '_connect', fake_connect):
    out = setup.start('connect')
    time.sleep(.8)
print('假如点得动,start() 之后 :', setup._state_file().read_text(encoding='utf-8'))
