#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @Author      : Cao Zejun
# @Time        : 2024/8/5 22:06
# @File        : util.py
# @Software    : Pycharm
# @description : 工具函数，存储一些通用的函数

import datetime
import json
import re
import shutil
from pathlib import Path

import requests
from lxml import etree
from src.utils.data_manager import headers


def jstime2realtime(jstime):
    """将js获取的时间id转化成真实时间，截止到分钟"""
    return (datetime.datetime.strptime("1970-01-01 08:00", "%Y-%m-%d %H:%M") + datetime.timedelta(
        minutes=jstime // 60)).strftime("%Y-%m-%d %H:%M")

def realtime2jstime(realtime):
    """
    将真实时间（如"2024-07-02 11:40"）转化为js获取的时间（自1970-01-01 08:00起的分钟数）
    Args:
        realtime: 字符串，格式为"YYYY-MM-DD HH:MM"
    Returns:
        int，分钟数
    """
    base_time = datetime.datetime.strptime("1970-01-01 08:00", "%Y-%m-%d %H:%M")
    target_time = datetime.datetime.strptime(realtime, "%Y-%m-%d %H:%M")
    delta = target_time - base_time
    return int(delta.total_seconds() // 60)


def time_delta(time1, time2):
    """计算时间差

    Args:
        time1: 第一个时间，格式为"YYYY-MM-DD HH:MM"
        time2: 第二个时间，格式为"YYYY-MM-DD HH:MM"

    Returns:
        返回时间差对象，可以通过：
        - result.days 获取天数
        - result.seconds 获取不满一天的秒数（0-86399）
        - result.total_seconds() 获取总的秒数
    """
    delta = datetime.datetime.strptime(time1,"%Y-%m-%d %H:%M") - datetime.datetime.strptime(time2,"%Y-%m-%d %H:%M")
    return delta


def time_now():
    """获取当前时间，格式为：2024-05-29 10:00"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def url2text(url, num=0):
    '''
    提取 rich_media_content 下的纯文本（供 LLM / 字数统计）。
    :return: 列表形式，每个元素对应一段；失败时为不可用原因或「请求错误」
    '''
    from src.utils.wechat_body import fetch_article_body

    text, _html = fetch_article_body(url, num=num)
    return text


def message_is_delete(url='', response=None):
    """检查文章是否正常运行(未被作者删除)"""
    if not response:
        response = requests.get(url=url, headers=headers).text
    tree = etree.HTML(response, parser=etree.HTMLParser(encoding='utf-8'))
    warn = tree.xpath('//div[@class="weui-msg__title warn"]/text()')
    if len(warn) > 0 and warn[0] == '该内容已被发布者删除':
        return True
    return False


# 原文永久不可读的页面提示。只收有实样的三种:作者删除、平台违规下架、
# 投诉侵权下架。这三种页面一个字都不会再变回正文,按临时失败重试等于
# 每天白打一次请求、还永远挂在面板的「正文待重试」里。
#
# 这张表要保守:命中即判「不可用」、退避拉到一周,误判会让一篇本可抓到的
# 文章一周才试一次。所以只写这几条特异的整句短语,不做「无法查看」这种
# 宽泛匹配 —— 认不出的形态落回 request_failed,顶多多试几次,方向是安全的。
_UNAVAILABLE_HINTS = (
    '已被发布者删除',
    '因违规无法查看',
    '涉嫌侵权',
)

# 第四种形态:作者把文章设成了隐私可见。这一种页面上没有任何提示文字 ——
# 服务端只回一个空壳(<div id="app"></div> + 公众号信息),「根据作者隐私设置,
# 无法查看该内容」那句是前端渲染出来的,抓下来的 HTML 里一个字都没有。
# 能认的只有入口 bundle:隐私页加载 private.<hash>.js,正常文章页加载
# appmsg.<hash>.js。按整条资源路径匹配,别拿 'private' 裸串去搜正文。
_PRIVATE_ENTRY_RE = re.compile(r'/assets/private(?:-legacy)?\.[0-9a-z]+\.js')
_PRIVATE_HINT = '根据作者隐私设置，无法查看该内容'


def message_unavailable(url='', response=None):
    """原文是否永久不可读;是则返回那句原因,否则空串。

    提示节点有 div.weui-msg__title.warn(删除)和 h3.weui-msg__title(违规、
    侵权)两种形态,所以按 class 包含匹配、取全部文本,别再只认 div.warn。
    隐私文章没有提示节点,只能认入口 bundle(见 _PRIVATE_ENTRY_RE)。

    传了 response 就只看它(空串也是「这次拿到的就是空页面」,不是「没传」),
    别在这里替调用方补一次不带凭证的裸请求。
    """
    if response is None:
        response = requests.get(url=url, headers=headers).text
    tree = etree.HTML(response, parser=etree.HTMLParser(encoding='utf-8'))
    if tree is None:
        return ''
    for raw in tree.xpath('//*[contains(@class, "weui-msg__title")]//text()'):
        hint = ' '.join(str(raw).split())
        if any(kw in hint for kw in _UNAVAILABLE_HINTS):
            return hint
    if _PRIVATE_ENTRY_RE.search(response):
        return _PRIVATE_HINT
    return ''


def check_text_ratio(text):
    """检测文本中英文和符号的占比
    Args:
        text: 输入文本字符串
    Returns: 英文字符占比和符号占比
    """
    # 统计字符数
    total_chars = len(text)
    if total_chars == 0:
        return 0, 0

    # 统计英文字符
    english_chars = sum(1 for c in text if c.isascii() and c.isalpha())

    # 统计符号 (不包括空格)
    symbols = sum(1 for c in text if not c.isalnum() and not c.isspace())

    # 计算占比
    english_ratio = english_chars / total_chars
    symbol_ratio = symbols / total_chars

    return english_ratio, symbol_ratio


def nunjucks_escape(text):
    """替换 Nunjucks 转义字符"""
    text = text.replace('{{', '{ {')
    text = text.replace('}}', '} }')  # 补充右大括号
    text = text.replace('{%', '{ %')  # 补充 Nunjucks 标签
    text = text.replace('%}', '% }')  # 补充 Nunjucks 标签
    text = text.replace('{#', '{ #')
    text = text.replace('#}', '# }')  # 补充注释标签
    text = text.replace('https:', 'https :')
    text = text.replace('http:', 'http :')

    # 新增：处理可能引起解析错误的特殊字符组合
    text = text.replace('{-', '{ -')
    text = text.replace('-}', '- }')
    text = text.replace('{{-', '{ { -')
    text = text.replace('-}}', '- } }')
    text = text.replace('{%-', '{ % -')
    text = text.replace('-%}', '- % }')

    # 处理可能的变量访问语法
    text = re.sub(r'(\w+)\.(\w+)', r'\1\. \2', text)  # 处理点号访问
    text = re.sub(r'(\w+)\[(\w+)\]', r'\1\[ \2\]', text)  # 处理方括号访问

    # 处理管道符（Nunjucks 过滤器语法）
    text = text.replace('|', '\\|')

    # 处理反引号和特殊引号
    text = text.replace('`', '\\`')
    text = text.replace('"', '\\"')
    text = text.replace('"', '\\"')

    # 处理可能的数学表达式或特殊符号
    text = text.replace('&lt;', '< ')
    text = text.replace('&gt;', '> ')
    text = text.replace('&amp;', '& ')
    text = text.replace('&quot;', '\\" ')

    # 处理HTML实体编码中的特殊字符
    text = re.sub(r'&#x([0-9A-Fa-f]+);', r'&# x\1;', text)
    text = re.sub(r'&#(\d+);', r'&# \1;', text)

    # 处理可能被误认为是 Nunjucks 语法的其他字符组合
    text = text.replace('\\n\\n\\n', '\\n \\n \\n')  # 根据你的错误可能相关

    for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp']:
        text = text.replace(ext, '')

    # 去掉html标签，防止转义失败
    text = re.sub(r'<[^>]*>', '', text)

    return text