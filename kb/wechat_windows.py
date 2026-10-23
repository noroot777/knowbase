"""Explicitly authorized Windows 4.x onboarding; never called by collection."""

import hashlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import time

from kb import wechat_runtime as runtime

READER_REV = '578dddde6db3f2466ac2b41ea67d5f993dc436f7'
KEY_REV = '79f1b5b92e12c66aa281b4a60a3c478b5f547dfa'
READER_FILES = {
    'rion_wechat_reader.py': 'ed15ca2814c4cefb4bb829ce915ad855655dbfc04d42cb4008db9a3b6e10511f',
    'windows_privacy.py': '37ebb36dc9978c9ec8585a2fa12d06e6de62f737ba0b31cf42958ba78f262f06',
}
KEY_SHA = '9de22ccc5070bfc5325ece1bdf4da8ca537eea594b821fc2acd46a62c0e41958'
READER_NOTICES = {
    'LICENSE': '0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0',
    'WINDOWS-CHANGES.md': 'aedd322cfcf2f9eb5557ced589b08937e130350ba43ba8a75eb23709f208a0fe',
}
REQUIREMENTS = (
    'sqlcipher3==0.6.2 --hash=sha256:8bd60ffb7bfa65bd0e51da3d5c308553d7149f0091d4ea9f754c33d5ebbf0a66\n'
    'zstandard==0.25.0 --hash=sha256:ffef5a74088f1e09947aecf91011136665152e0b4b359c42be3373897fb39b01\n'
    'pywin32==311 --hash=sha256:b8c095edad5c211ff31c05223658e71bf7116daa0ecf3ad85f3201ea3190d067\n'
)


def reader_paths():
    source = runtime.runtime_home() / 'windows-reader'
    return source, source / 'venv/Scripts/python.exe', source / 'reader_launcher.py'


def account_roots():
    bases = [Path.home() / 'Documents/xwechat_files']
    appdata = os.getenv('APPDATA')
    if appdata:
        for ini in sorted((Path(appdata) / 'Tencent/xwechat/config').glob('*.ini'))[:32]:
            for encoding in ('utf-8-sig', 'gb18030'):
                try:
                    with ini.open(encoding=encoding) as stream:
                        value = stream.read(2048).strip().strip('"')
                    candidate = Path(value)
                    if value and candidate.is_absolute() and not str(candidate).startswith('\\\\'):
                        bases.extend((candidate, candidate / 'xwechat_files'))
                    break
                except UnicodeError:
                    continue
                except OSError:
                    break
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            documents, _ = winreg.QueryValueEx(key, 'Personal')
            bases.append(Path(os.path.expandvars(documents)) / 'xwechat_files')
    except (ImportError, OSError):
        pass
    explicit = os.getenv('KB_WECHAT_DATABASE_ROOT')
    candidates = [Path(explicit).expanduser()] if explicit else []
    for base in dict.fromkeys(bases):
        if not str(base).startswith('\\\\') and base.is_dir() and not base.is_symlink():
            candidates.extend(sorted(base.glob('*/db_storage'))[:32])
    roots = {}
    for candidate in candidates:
        if not str(candidate).startswith('\\\\') and candidate.is_dir() and not candidate.is_symlink():
            resolved = candidate.resolve()
            roots[hashlib.sha256(str(resolved).encode()).hexdigest()[:16]] = resolved
    return roots


def prepare_reader(progress):
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise runtime.SetupError('Windows 自动接入目前仅支持 x64；其他架构可配置兼容 Reader 后复用。')
    source, python, launcher = reader_paths()
    runtime.private_dir(source)
    progress('preparing', '正在准备 Windows x64 Reader、SQLCipher 与只读依赖…')
    for filename, digest in READER_FILES.items():
        runtime.download('https://raw.githubusercontent.com/iversonzhang50-gif/'
                         f'wechat-intelligence-hub-windows/{READER_REV}/projects/rion-wechat-reader/{filename}',
                         source / filename, digest)
    for filename, digest in READER_NOTICES.items():
        runtime.download('https://raw.githubusercontent.com/iversonzhang50-gif/'
                         f'wechat-intelligence-hub-windows/{READER_REV}/{filename}', source / filename, digest)
    uv = shutil.which('uv')
    if not uv:
        candidates = [Path.home() / '.local/bin/uv.exe']
        if os.getenv('APPDATA'):
            candidates.extend(sorted((Path(os.environ['APPDATA']) / 'Python').glob('Python*/Scripts/uv.exe')))
        uv = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    if not python.is_file():
        if not uv:
            raise runtime.SetupError('需要 uv 准备 Windows 隔离读取环境，请安装 uv 后重新检查。')
        runtime.checked([uv, 'venv', '--python', '3.12', source / 'venv'], timeout=300)
    code, _ = runtime.run([python, '-c', 'from sqlcipher3 import dbapi2; import zstandard; '
                           'import win32security; assert dbapi2.connect(":memory:")'
                           '.execute("PRAGMA cipher_version").fetchone()'])
    if code:
        if not uv:
            raise runtime.SetupError('Windows Reader 依赖未就绪，请安装 uv 后重新检查。')
        requirements = source / 'requirements.txt'
        requirements.write_text(REQUIREMENTS, encoding='utf-8')
        runtime.checked([uv, 'pip', 'install', '--python', python, '--require-hashes',
                         '--only-binary', ':all:', '-r', requirements], timeout=600)
    for filename, target in (('wechat_access_adapter.py', 'wechat_access_adapter.py'),
                             ('wechat_windows_reader.py', launcher.name)):
        shutil.copyfile(Path(__file__).with_name(filename), source / target)
    command = [str(python), str(launcher)]
    result = runtime.json_call([*command, 'self-test', '--require-sqlcipher'])
    if result.get('passed') is not True:
        raise runtime.SetupError('Windows Reader 自检未通过，已停止；请检查读取依赖与运行环境。')
    return command


def prepare_provider():
    source, _, _ = reader_paths()
    provider = source / 'wcdb_key_tool_windows.py'
    runtime.download(f'https://raw.githubusercontent.com/TANGandXUE/wcdb-key-tool/{KEY_REV}/'
                     'wcdb_key_tool_windows.py', provider, KEY_SHA)
    runtime.download(f'https://raw.githubusercontent.com/TANGandXUE/wcdb-key-tool/{KEY_REV}/LICENSE',
                     source / 'LICENSE.key-tool',
                     '098659c726e64afd1c503227c1f13866a3ae55a533744185d953828f37c54d8e')
    return provider


def connect(progress, account_id, acquire=False):
    from kb import wechat_setup as setup

    roots = account_roots()
    config = setup._config()
    if not config.exists():
        if not roots:
            return progress('needs_database_location', '未发现 Windows 4.x 绿泡泡数据库。请登录本人账号并同步公众号推送；自定义目录可设置 KB_WECHAT_DATABASE_ROOT 后重新检查。')
        if not account_id and len(roots) > 1:
            return progress('account_selection_required', '发现多个本机账号，请选择要接入的本人账号。',
                            accounts=[{'id': key, 'label': path.parent.name} for key, path in roots.items()])
        if account_id and account_id not in roots:
            raise runtime.SetupError('所选账号目录已变化，请重新检查。')
    command = prepare_reader(progress)
    if config.exists():
        if setup._reader_ready():
            return setup._ready(progress)
        return progress('existing_configuration_requires_review', setup.MESSAGES['existing_configuration_requires_review'])
    root = roots[account_id] if account_id else next(iter(roots.values()))
    selected = next(key for key, path in roots.items() if path == root)
    keys = config.parent / 'keys.json'
    source, python, _ = reader_paths()
    bundle = source / f'access-{selected}/all_keys.json'
    supplied = os.getenv('KB_WECHAT_KEYS_FILE')
    material = Path(supplied).expanduser() if supplied else bundle
    if not keys.exists() and material.is_file():
        progress('verifying', '正在验证已有本机授权材料，不会重复获取…')
        runtime.json_call([*command, '--config', str(config), 'import-access', '--source', str(material),
                           '--database-root', str(root), '--keys-file', str(keys)])
    plan_args = [*command, '--config', str(config), 'access-plan', '--database-root', str(root),
                 '--keys-file', str(keys)]
    progress('verifying', '正在验证 Windows 数据库与已有访问材料，密钥不会显示在页面中…')
    plan = runtime.json_call(plan_args)
    if plan.get('state') == 'ready_to_configure':
        runtime.json_call([*command, '--config', str(config), 'setup', '--database-root', str(root),
                           '--keys-file', str(keys)])
        return setup._ready(progress)
    if keys.exists() or material.is_file() or supplied or plan.get('state') != 'needs_access':
        state = plan.get('state')
        return progress(state if state in setup.MESSAGES else 'blocked',
                        setup.MESSAGES.get(state, '已有材料或数据库未通过验证，已保留；请核对账号、目录与绿泡泡版本，不会自动重复获取。'))
    provider = prepare_provider()
    if not acquire:
        return progress('authorization_required', 'Windows 接入工具已准备好。确认仅处理本人账号后，读取本机绿泡泡进程中的数据库访问材料；不会注入、重启或修改绿泡泡。',
                        confirmation_token=secrets.token_urlsafe(32), expires_at=time.time() + 900,
                        account_id=selected, provider_revision=KEY_REV, platform='windows')
    runtime.private_dir(bundle.parent)
    progress('acquiring', '正在只读检查本机绿泡泡进程，最多 3 分钟；不注入、不重启、不修改客户端。', platform='windows')
    code, _ = runtime.run([python, provider, 'extract', '--db-dir', root, '--output', bundle], timeout=180)
    if code or not bundle.is_file():
        raise runtime.SetupError('Windows 获取未完成，未发布配置。请确认本人绿泡泡 4.x 已登录、进程权限一致及版本兼容；本次不会自动重试或提权。')
    return connect(progress, selected, acquire=False)
