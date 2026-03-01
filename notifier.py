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


def send_success(config: Config, books_downloaded: list[str], elapsed: str) -> None:
    body_lines = [
        f'Run complete. {len(books_downloaded)} book(s) downloaded.',
        f'Elapsed time: {elapsed}',
        '',
        'Books:',
    ] + [f'  - {b}' for b in books_downloaded]
    _send(config, f'Run complete: {len(books_downloaded)} book(s)', '\n'.join(body_lines))


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
