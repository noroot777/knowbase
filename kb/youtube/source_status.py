from kb.youtube import diagnosis, login


def fields(source, summary, pending=False, videos=()):
    identity = str(source['id'])
    partial = [item['reason'] for item in summary.get('partial', ())
               if str(item.get('source_id', '')) == identity]
    notices = [item['reason'] for item in summary.get('notices', ())
               if str(item.get('source_id', '')) == identity]
    if pending:
        notices.extend('视频处理未完成：' + row['last_error'] for row in videos
                       if not row.get('excluded') and row['state'] not in ('ready', 'excluded', 'filtered')
                       and row.get('last_error'))
    browser = summary.get('browser', 'chrome')
    for code, identities in summary.get('walls', {}).items():
        if identity in {str(value) for value in identities}:
            notices.append(login.describe(code, browser))
    for code, identities in summary.get('cautions', {}).items():
        failed = {str(value) for value in summary.get('walls', {}).get(code, ())}
        if identity in {str(value) for value in identities} and identity not in failed:
            if message := login.cautioned(code, browser):
                notices.append(message)
    if partial:
        note = '频道没列完：' + '；'.join(dict.fromkeys(partial))
    else:
        note = ''
    if pending and not partial:
        notices.append('本轮已停止，视频仍有未完成处理' if summary.get('stopped') else
                       '本机读写未完成，留待下轮重算' if identity in summary.get('local_ids', ()) else
                       '视频仍有未完成处理或待重试错误')
    return diagnosis.excerpt(note, 1200), diagnosis.excerpt('；'.join(dict.fromkeys(notices)), 1200)
