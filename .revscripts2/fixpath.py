from pathlib import Path
p = Path('.revscripts2/repro_account_main.py')
t = p.read_text(encoding='utf-8').replace('knowbase\.worktrees\WJ1Uxb6zVWbJ', 'knowbase')
p.write_text(t, encoding='utf-8')
print([l for l in t.splitlines() if 'sys.path.insert' in l])
