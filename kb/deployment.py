"""Runtime capabilities; a Linux container cannot access the Mac WeChat process."""

import os
from pathlib import Path


def in_container() -> bool:
    return (os.environ.get('KB_CONTAINER') == '1'
            or Path('/.dockerenv').exists() or Path('/run/.containerenv').exists())


def wechat_container_status() -> dict:
    return {
        'state': 'host_required', 'running': False, 'ready': False, 'can_connect': False,
        'message': '当前运行在 Docker 中，不能访问 Mac 绿泡泡进程获取 key。请在 Mac 本机完成绿泡泡接入和文章采集，再将导出的文章目录挂载到容器；知识库索引和分析仍在容器中运行。',
        'deployment': 'docker',
    }
