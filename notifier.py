"""SMTP email notifications."""

import smtplib
from email.mime.text import MIMEText
from config import Config

SUBJECT_PREFIX = '[FakkuDL]'


def _send(config: Config, subject: str, body: str) -> None:
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = f'{SUBJECT_PREFIX} {subject}'
    msg['From'] = config.smtp_from
    msg['To'] = config.smtp_to
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.starttls()
            server.login(config.smtp_user, config.smtp_password)
            server.sendmail(config.smtp_from, [config.smtp_to], msg.as_string())
    except Exception as e:
        # Notification failure must never crash the main process
        print(f'[notifier] Failed to send email: {e}')


def send_success(config: Config, reports: list[dict], elapsed: str) -> None:
    downloaded = [r for r in reports if not r.get('skipped')]
    skipped    = [r for r in reports if r.get('skipped')]

    subject = f'Run complete: {len(downloaded)} downloaded'
    if skipped:
        subject += f', {len(skipped)} skipped'

    lines = [
        f'Run complete. {len(downloaded)} book(s) downloaded, {len(skipped)} skipped.',
        f'Elapsed time: {elapsed}',
    ]

    if downloaded:
        lines += ['', '=' * 60, 'DOWNLOADED', '=' * 60]
        for r in downloaded:
            lines.append('')
            lines.append(f'  {r["display_name"]}')
            lines.append(f'  URL:    {r["url"]}')
            lines.append(f'  Pages:  {r["pages"]}')

            routing = r['routing']
            if routing == 'multi_collection':
                lines.append('  Type:   *** MULTIPLE COLLECTIONS — placed in TO FIX MANUALLY/')
                lines.append('          Assign to the correct series manually.')
            elif routing == 'missing_volumes':
                missing = r.get('missing_vol_nums', [])
                vols = ', '.join(f'vol.{k}' for k in missing)
                lines.append(f'  Type:   *** MISSING PRECEDING VOLUMES ({vols}) — placed in TO FIX MANUALLY/')
                lines.append(f'  Series: {r["series_name"]} vol.{r["volume_number"]}')
                lines.append('          Find/download the missing volumes, then move this file manually.')
            elif routing == 'cover':
                lines.append('  Type:   Cover (≤4 pages) → placed in Covers/')
            elif routing == 'series':
                sdir = r['series_dir']
                created = r.get('series_dir_created')
                created_note = ' (new folder)' if created else ' (existing folder)'
                lines.append(f'  Type:   Series — {r["series_name"]} vol.{r["volume_number"]}')
                lines.append(f'  Folder: {sdir}{created_note}')
            else:
                lines.append(f'  Type:   One-shot → placed in {r["series_dir"]}')

            lines.append(f'  File:   {r["cbz_filename"]}')

            move = r.get('oneshot_move')
            if move:
                lines.append(f'  Moved vol.1 from one-shots:')
                lines.append(f'    Before: {move["from"]}')
                lines.append(f'    After:  {move["to"]}')

    if skipped:
        lines += ['', '=' * 60, 'SKIPPED', '=' * 60]
        for r in skipped:
            lines.append(f'  {r["url"]}  [{r.get("skip_reason", "unknown")}]')

    _send(config, subject, '\n'.join(lines))


def send_error(config: Config, url: str, page: int | None, error: str, trace: str) -> None:
    location = f'page {page}' if page else 'metadata stage'
    body = '\n'.join([
        'Run halted due to an unrecoverable error.',
        '',
        f'URL:      {url}',
        f'Location: {location}',
        f'Error:    {error}',
        '',
        'Traceback:',
        trace,
    ])
    _send(config, 'ERROR: Run halted', body)


def send_warning(config: Config, subject: str, body: str) -> None:
    _send(config, f'Warning: {subject}', body)
