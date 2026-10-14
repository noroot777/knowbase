# -*- coding: utf-8 -*-
"""订阅页那行「这一类来源什么时候自动同步」,和它背后那层共享定时配置。

**单独成文件只因为行数**(从 `tests/test_retry_multi_source.py` 拆出来,那边到了
700 行上限);摆盘手法原样搬过来:抠真代码丢给 node 求值。

这一组只钉共享层的契约(三态、包形、代次、画不画得了自己的控件)。
**"某个面板有没有按这个契约接线"节点这一层钉不住** —— 顺序错了(先画完再问
资格)这里一条都不红,定点注入验证过;那部分在 `tests/daily_probe.mjs` 和
`tests/subs_schedule_note_checks.mjs` 的真浏览器那两组。
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / 'kb' / 'static' / 'js'


class SourceScheduleNoteTests(unittest.TestCase):
    """订阅页那行「这一类来源什么时候自动同步」:抠真代码丢给 node 求值。

    **喂的是两个接口的真实返回包**,不是手写的扁平设置对象 —— 上一版就是把
    `/api/daily/settings` 的外层(`{settings, channels, default_channels}`)当设置
    读,`enabled` 永远取不到,于是开着的定时被一律说成「未启用」;而那一版的用例
    自己造了一个扁平对象喂进去,563 条回归一条都没红。

    **要问遍所有还在生效的定时**:页头那颗「定时同步」(pipeline_schedule)按要求
    暂留着,只看日报的话 B 开着时这里会说「不会自动同步」,跟页头那行当场打架。
    """

    def run_js(self, body, daily=None, pipe=None, kind='rss'):
        """抠真代码(共享定时层 + 那两个派生函数)丢给 node,`body` 是这一局的剧本。

        解包那一下必须由产品代码做:上一版在测试脚本里自己写死
        `d.settings`,于是把「读错包形」注入进去照样全绿(红 0 条)—— 而那正是
        这条用例要挡的那个 bug。None = 那个接口读不到(走 catch 那条路)。
        """
        # 共享那一层**整份带进来**(它自带模块级的缓存和代次,只缺一个 `pipeJson`)
        # —— 抠函数会把那几个 let 漏在外面,而代次正是这一组要验的东西。
        shared = (JS / 'schedules.js').read_text(encoding='utf-8')
        forms = (JS / 'source_forms.js').read_text(encoding='utf-8')
        funcs = [re.search(rf'(?:async )?function {name}\([^)]*\) \{{.*?\n\}}',
                           forms, re.S)
                 for name in ('sourceScheduleHits', 'renderSourceScheduleNote')]
        self.assertTrue(all(funcs), '那几行代码的形状变了,这条用例得跟着改')
        for name in ('loadSchedules', 'schedulesSaved', 'schedulesRead',
                     'watchSchedules', 'schedulesApply'):
            self.assertIn(f'function {name}(', shared, name)
        script = f"""
let box = {{ textContent: '' }};
function $(id) {{ return id === 'source-settings-status' ? box : null; }}
const sourceKind = {kind!r};
const RESPONSES = {{ '/api/daily/settings': {daily or 'null'},
                     '/api/pipeline/schedule': {pipe or 'null'} }};
// 接口读不到时 `pipeJson` 是会抛的,这里照样抛 —— 那条 catch 分支也要被走到。
// `hold` 非空 = 这一路的回包按住不放,等剧本放行(验乱序用)。
let hold = {{}};
function pipeJson(url) {{
  const body = RESPONSES[url];
  const answer = () => body === null
    ? Promise.reject(new Error('读不到')) : Promise.resolve(body);
  return hold[url] ? hold[url].then(answer) : answer();
}}
{shared}
{chr(10).join(f.group(0) for f in funcs)}
// 来源页那句说明只做派生展示:配置一变就重画(接线同 source_forms.js)
watchSchedules(() => renderSourceScheduleNote());
(async () => {{ {body} }})().then(() => console.log(box.textContent),
                                  e => {{ console.error(e); process.exit(1); }});
"""
        done = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip()

    def line(self, daily=None, pipe=None, kind='rss'):
        """读一次两条定时之后,那行说明是什么。"""
        return self.run_js('await loadSchedules();', daily, pipe, kind)

    # `/api/daily/settings` 的真实包形:设置在 settings 里,外层那个 channels 是
    # 可选来源目录(一堆 {id,label}),不是待同步名单 —— 拿它当名单也会判错。
    DAILY_ON = ("{settings: {enabled: true, sync: true, time: '09:00', channels: ['rss']},"
                " channels: [{id: 'rss', label: 'RSS'}, {id: 'social', label: '大V观点'}],"
                " default_channels: ['wechat_local', 'rss']}")
    # 页头那条:读得到、而且确实关着。**跟"读不到"是两件事**,所以要分开摆。
    PIPE_OFF = "{enabled: false, time: '07:30', channel: 'rss', channels: ['rss']}"

    def test_an_enabled_daily_schedule_is_not_reported_as_off(self):
        self.assertIn('每天 09:00 跟着日报那一轮同步', self.line(daily=self.DAILY_ON))
        self.assertNotIn('未启用', self.line(daily=self.DAILY_ON))

    def test_the_retained_head_schedule_counts_too(self):
        # A 关着、B 开着且范围含 RSS:页头写着「定时同步 · 每天 07:30」,这里不许
        # 同时说「目前不会自动同步」。
        off = "{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}"
        b_on = "{enabled: true, time: '07:30', channel: 'rss', channels: ['rss']}"
        got = self.line(daily=off, pipe=b_on)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', got)
        self.assertNotIn('不会自动同步', got)
        # 反过来钉一边:B 开着但范围里没有这一类,照旧如实说不会
        b_other = "{enabled: true, time: '07:30', channel: 'wechat_local', channels: ['wechat_local']}"
        self.assertIn('不会自动同步', self.line(daily=off, pipe=b_other))

    def test_each_reason_for_no_automatic_sync_is_named(self):
        for daily, expect in (
                ("{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}",
                 '日报定时未启用'),
                ("{settings: {enabled: true, sync: false, time: '09:00', channels: ['rss']}}",
                 "没开「生成前先同步」"),
                ("{settings: {enabled: true, sync: true, time: '09:00', channels: ['social']}}",
                 '不在日报的同步范围里')):
            # **两条定时都读得到**才轮得到念原因(见下一条用例):少一条就是不确定
            got = self.line(daily=daily, pipe=self.PIPE_OFF)
            self.assertIn(expect, got)
            self.assertNotIn('每天 09:00 跟着日报', got)

    def test_one_unreadable_schedule_is_not_read_as_off(self):
        """读不到 ≠ 没开。**少一条就不许断言「不会自动同步」。**

        两态读法(读到一个就按已知的那部分说话)的后果:只中断页头那条的 GET,
        这行字就说「日报定时未启用,这一类来源目前不会自动同步」,而页头上明明
        写着「定时同步 · 每天 07:30」—— 用户看到的是两句互相打脸的话,而真相是
        这一类来源每天 07:30 真的会被同步。反过来(日报那条读不到)同形。
        """
        a_off = "{settings: {enabled: false, sync: true, time: '09:00', channels: ['rss']}}"
        for args, who in ((dict(daily=a_off), '页头的定时同步'),
                          (dict(pipe=self.PIPE_OFF), '日报')):
            got = self.line(**args)
            self.assertIn(f'读不到{who}的设置', got)
            self.assertIn('没法确定', got)
            # 不许替没读到的那条下结论(断言用确定句的字样:不确定那句本身就带着
            # 「会不会自动同步」,拿它当否定词永远红)
            self.assertNotIn('目前不会自动同步', got)
            self.assertNotIn('未启用', got)
        # 两条都读不到时同理,而且两条都点名
        both = self.line()
        self.assertIn('读不到日报、页头的定时同步的设置', both)
        self.assertNotIn('目前不会自动同步', both)
        # 反过来钉一边:两条都读到了、都没带上它,照旧给出确定的那句话和原因
        settled = self.line(daily=a_off, pipe=self.PIPE_OFF)
        self.assertIn('日报定时未启用，这一类来源目前不会自动同步', settled)
        self.assertNotIn('没法确定', settled)

    def test_a_hit_is_stated_even_when_the_other_schedule_is_unreadable(self):
        # 已知有一条带上它就够了:另一条读不到只可能再多一个同步时刻,不会把这个
        # 时刻取消掉。这里退回"不知道"等于把一句真话换成一句含糊话。
        got = self.line(daily=self.DAILY_ON)
        self.assertIn('每天 09:00 跟着日报那一轮同步', got)
        self.assertNotIn('没法确定', got)

    PIPE_ON = "{enabled: true, time: '07:30', channel: 'rss', channels: ['rss']}"
    DAILY_OFF = ("{settings: {enabled: false, sync: true, time: '09:00',"
                 " channels: ['rss']}}")

    def test_saving_a_schedule_updates_the_note_on_the_same_page(self):
        """保存成功之后,同一页那句说明必须跟着变。**不许等刷新。**

        第 6 轮真实点击实测:在 RSS 的订阅管理里打开页头「定时同步」、勾上启用、
        保存 —— 页头立刻变成「定时同步 · 每天 07:30」,右侧却还写着「日报定时未
        启用,这一类来源目前不会自动同步」;反过来关掉再保存,右侧继续承诺每天
        同步。留在页面上等常规轮询也不会好(轮询不重读这份配置),只有整页刷新
        才对得上。根因是那句说明自己缓存了一份配置,而保存只更新页头。
        """
        # ① 开启:两条都读到过(都关着),保存页头那条 → 说明当场改口
        opened = self.run_js("""
            await loadSchedules();
            if (!/不会自动同步/.test(box.textContent))
              throw new Error('摆局没成:' + box.textContent);
            schedulesSaved('pipe', { enabled: true, time: '07:30', channels: ['rss'] });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', opened)
        self.assertNotIn('目前不会自动同步', opened)
        # ② 关停:**反过来也要改口**,不许继续承诺每天同步
        closed = self.run_js("""
            await loadSchedules();
            if (!/每天 07:30/.test(box.textContent))
              throw new Error('摆局没成:' + box.textContent);
            schedulesSaved('pipe', { enabled: false, time: '07:30', channels: ['rss'] });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_ON)
        self.assertIn('日报定时未启用', closed)
        self.assertNotIn('每天 07:30 跟着', closed)
        # ③ 日报那条同理(它的 POST 回的是扁的设置对象,GET 才套着一层)
        daily_on = self.run_js("""
            await loadSchedules();
            schedulesSaved('daily', { enabled: true, sync: true, time: '06:05',
                                      channels: ['rss'] });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('每天 06:05 跟着日报那一轮同步', daily_on)
        # ④ 改范围也算变化:范围里去掉这一类之后不许还说会同步
        narrowed = self.run_js("""
            await loadSchedules();
            schedulesSaved('pipe', { enabled: true, time: '07:30',
                                     channels: ['wechat_local'] });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_ON)
        self.assertIn('日报定时未启用', narrowed)
        self.assertNotIn('07:30', narrowed)

    def test_an_in_flight_read_cannot_roll_back_a_saved_schedule(self):
        """在途的旧回包不许把刚保存的配置倒退回去。

        打开来源页会现读一次两条定时;这一趟还没回来时用户就把页头那条保存了 ——
        旧回包落地时带的是保存**之前**的事实,照它写回去等于把刚保存的设置悄悄
        撤销,而页头上写的是新的。代次按条算(见 `schedules.js`)。
        """
        out = self.run_js("""
            // 两路回包都按住:读取发出去了,但还没落地
            let open1, open2;
            hold['/api/pipeline/schedule'] = new Promise(r => { open1 = r; });
            hold['/api/daily/settings'] = new Promise(r => { open2 = r; });
            const reading = loadSchedules();
            // 这中间用户保存了页头那条
            schedulesSaved('pipe', { enabled: true, time: '07:30', channels: ['rss'] });
            if (!/每天 07:30/.test(box.textContent))
              throw new Error('摆局没成:' + box.textContent);
            open1(); open2();
            await reading;
        """, daily=self.DAILY_ON, pipe=self.PIPE_OFF)
        # 页头那条:保存的那一份说了算,旧回包(关着)丢掉
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', out)
        # **另一条照旧收下**:代次按条算,一条被保存不许把另一条的回包连带作废 ——
        # 作废了的话日报那条会一直停在「读不到」
        self.assertIn('每天 09:00 跟着日报那一轮同步', out)

    def test_a_panel_reread_cannot_roll_back_a_saved_schedule(self):
        """面板自己那次重读也要占代次,不许绕过去直接当"最新"。

        两个面板各自还会读一次这份配置(`loadSched`、`loadDailySettings`:切标签、
        重开弹窗都会读)。把它们读到的直接当最新写进共享层,就又开了一条倒退的
        路:重读发出去、用户紧接着保存、旧回包落地 —— 共享层被写回保存**之前**
        的事实,而按钮上写的是新的。所以读取一律先 `schedulesRead` 占代次。
        """
        out = self.run_js("""
            await loadSchedules();
            // 面板重开,读取发出去(代次占在这一刻)
            const accept = schedulesRead('pipe');
            // 回包还没落地,用户把页头那条开了并保存成功
            schedulesSaved('pipe', { enabled: true, time: '07:30', channels: ['rss'] });
            // 现在旧回包才落地,带的是保存之前的事实
            accept({ pipe: { enabled: false, time: '07:30', channels: ['rss'] } });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', out)
        self.assertNotIn('目前不会自动同步', out)
        # 反过来钉一边:期间没人保存,这个回包照旧收下(一律丢掉的话面板重读
        # 就永远更新不了这句说明)
        fresh = self.run_js("""
            await loadSchedules();
            const accept = schedulesRead('pipe');
            accept({ pipe: { enabled: true, time: '07:30', channels: ['rss'] } });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', fresh)

    def test_a_read_gets_back_the_confirmed_fact_not_its_own_answer(self):
        """被顶掉的那趟读取**照共享层确认的那一份画**,不许照自己的回包画。

        第 1 轮钉的是"资格先判再画";第 2 轮双页面实测说明那还不够:另一份页面
        在两次读取之间保存成功,被顶掉的那趟照自己的回包画,就把已经显示出来的
        新事实退回旧事实(开关变回未勾选、提示写「定时生成未启用」),而共享事实
        拒绝退回 —— 同一页上来源说明还承诺每天同步,两处当场打架。所以
        `accept` 回的是**当前已确认的事实**,不是这趟的回包。
        """
        # ① 期间别处保存过:拿回来的是保存那一份,不是自己读到的旧值
        out = self.run_js("""
            await loadSchedules();
            const mine = schedulesRead('pipe');
            schedulesSaved('pipe', { enabled: true, time: '07:30', channels: ['rss'] });
            const got = mine({ pipe: { enabled: false, time: '09:09', channels: ['rss'] } });
            box.textContent = JSON.stringify(got.pipe) + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('"enabled":true', out)
        self.assertNotIn('09:09', out)            # 自己那份旧回包一个字都不许露头
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', out)
        # ② 被更晚的一次读取顶掉:同理,拿回来的是那一趟写进去的事实
        later = self.run_js("""
            await loadSchedules();
            const mine = schedulesRead('pipe');
            schedulesRead('pipe')({ pipe: { enabled: true, time: '07:30', channels: ['rss'] } });
            const got = mine({ pipe: { enabled: false, time: '09:09', channels: ['rss'] } });
            box.textContent = JSON.stringify(got.pipe) + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('"enabled":true', later)
        self.assertNotIn('09:09', later)
        # ③ 钉边:没人顶掉它时,自己这份照旧写得进去、也拿得回来(不然面板重读
        #    就永远更新不了)
        fresh = self.run_js("""
            await loadSchedules();
            const got = schedulesRead('pipe')(
              { pipe: { enabled: true, time: '06:06', channels: ['rss'] } });
            box.textContent = JSON.stringify(got.pipe) + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertIn('06:06', fresh)
        self.assertIn('每天 06:06 跟着页头的定时同步那一轮同步', fresh)
        # ④ 最新那次读取失败 = 当前"不知道":回 null,调用方走它自己的失败那条路
        unknown = self.run_js("""
            await loadSchedules();
            const got = schedulesRead('pipe')({ pipe: null });
            box.textContent = (got.pipe === null ? 'null' : 'value') + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_ON)
        self.assertTrue(unknown.startswith('null | '), unknown)
        self.assertIn('读不到页头的定时同步的设置', unknown)

    def test_a_stale_failure_cannot_overwrite_a_newer_fact(self):
        """**读失败也要过这道门。** 被顶掉的那趟读取失败了,不许用它的失败把别处
        刚读到/刚保存的好值盖成"不知道" —— 那会把一句「读取失败」挂在一份正常的
        配置上,而面板上那几句说明跟着退回「没法确定」。
        """
        out = self.run_js("""
            await loadSchedules();
            const mine = schedulesRead('pipe');
            schedulesSaved('pipe', { enabled: true, time: '07:30', channels: ['rss'] });
            const got = mine({ pipe: null });          // 这趟读失败,但它已经过期
            box.textContent = (got.pipe ? 'kept' : 'lost') + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertTrue(out.startswith('kept | '), out)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', out)
        # 钉边:当令的那趟读失败,照旧要如实变成"不知道"
        reported = self.run_js("""
            await loadSchedules();
            schedulesRead('pipe')({ pipe: null });
        """, daily=self.DAILY_OFF, pipe=self.PIPE_ON)
        self.assertIn('读不到页头的定时同步的设置', reported)

    def test_one_read_settles_only_once(self):
        """一趟读取只结账一次:异常分支里再调一次 `accept` 不许把刚写进去的
        好值盖成"不知道"(两处都调是现实写法 —— 成功那条路画完,紧接着的渲染
        要是抛了就会落到 catch 里)。"""
        out = self.run_js("""
            await loadSchedules();
            const mine = schedulesRead('pipe');
            mine({ pipe: { enabled: true, time: '07:30', channels: ['rss'] } });
            const second = mine({ pipe: null });
            box.textContent = (second.pipe ? 'kept' : 'lost') + ' | ' + box.textContent;
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertTrue(out.startswith('kept | '), out)
        self.assertIn('每天 07:30 跟着页头的定时同步那一轮同步', out)

    def test_reading_again_on_an_open_page_keeps_the_newest_answer(self):
        # 已经打开的页面再读一次(切来源、重开设置都会读):读到的仍是当前事实,
        # 而且重复读不许把说明抖回「不知道」那一档。
        out = self.run_js("""
            await loadSchedules();
            await loadSchedules();
        """, daily=self.DAILY_ON, pipe=self.PIPE_OFF)
        self.assertIn('每天 09:00 跟着日报那一轮同步', out)
        self.assertNotIn('没法确定', out)

    def test_a_form_is_not_ready_until_every_dependency_is_read(self):
        """**配置可用 ≠ 这张表单的依赖都可用。**

        两张定时表单除了定时本身还要一份「可勾选的来源目录」,而目录和设置是两趟
        请求、各自会失败。只看配置就进"已加载"的后果(第 4 轮真实鼠标实测):弹窗
        写着「已保存:每天 07:30 自动同步」、字段开放、重读藏着,而来源一个都没有 ——
        关掉定时去保存只换来一句「请至少选择一个同步来源」,一个都选不了也没法重读。

        这一层钉的是判据本身(`scheduleSettle`),**钉不住某张表单有没有按它接线**:
        那部分在 `tests/subs_schedule_note_checks.mjs` 的真浏览器那一组。
        """
        out = self.run_js("""
            const fact = { enabled: true, time: '07:30', channels: ['rss'] };
            const deps = ready => [{ ready, what: '来源目录' }];
            const verdict = (o) => {
              const v = scheduleSettle(o);
              return v.state + (v.missing ? ':' + v.missing : '');
            };
            box.textContent = [
              verdict({ fact, deps: deps(3) }),                        // 齐了
              verdict({ fact, deps: deps(0) }),                        // 目录没读到
              verdict({ fact: null, deps: deps(3) }),                  // 配置不知道
              verdict({ fact, deps: deps(0), dirty: true }),           // 草稿优先
              verdict({ fact, deps: deps(0), saving: true }),          // 保存自己收尾
              verdict({ fact }),                                       // 没声明依赖
            ].join(' | ');
        """, daily=self.DAILY_OFF, pipe=self.PIPE_OFF)
        self.assertEqual('ready | unknown:来源目录 | unknown | draft | saving | ready', out)


    def test_manual_sync_is_always_true(self):
        for args in ({'daily': self.DAILY_ON}, {'pipe': self.PIPE_OFF}, {}):
            self.assertIn('手动同步随时可用', self.line(**args))


if __name__ == '__main__':
    unittest.main()
