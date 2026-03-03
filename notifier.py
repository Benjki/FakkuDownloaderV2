"""SMTP email notifications."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import Config

SUBJECT_PREFIX = '[FakkuDL]'

# Routing state -> left-border colour for book cards
_ROUTING_COLOUR = {
    'series':           '#22c55e',  # green
    'oneshot':          '#3b82f6',  # blue
    'cover':            '#9ca3af',  # grey
    'multi_collection': '#ef4444',  # red
    'missing_volumes':  '#ef4444',  # red
    'file_conflict':    '#ef4444',  # red
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _badge(text: str, bg: str) -> str:
    style = (
        f'display:inline-block;padding:3px 10px;border-radius:12px;'
        f'background:{bg};color:#fff;font-size:12px;font-weight:600;'
        f'margin-right:6px;font-family:sans-serif;'
    )
    return f'<span style="{style}">{text}</span>'


def _book_card(r: dict) -> str:
    routing = r['routing']
    border = _ROUTING_COLOUR.get(routing, '#9ca3af')
    card_style = (
        f'border-left:4px solid {border};padding:10px 14px;margin:8px 0;'
        f'background:#f9fafb;border-radius:0 6px 6px 0;font-family:sans-serif;'
        f'font-size:13px;color:#1f2937;'
    )

    rows = [f'<div style="{card_style}">']
    rows.append(
        f'<div style="font-weight:700;font-size:14px;margin-bottom:4px;">'
        f'{r["display_name"]}</div>'
    )
    author_val = r.get("author") or '<span style="color:#ef4444;">NOT FOUND</span>'
    rows.append(f'<div><b>Author:</b> {author_val}</div>')
    rows.append(f'<div><b>Pages:</b> {r["pages"]}</div>')

    # Routing detail
    if routing == 'multi_collection':
        rows.append(
            '<div style="color:#ef4444;font-weight:600;">&#9888; MULTIPLE COLLECTIONS — '
            'placed in TO FIX MANUALLY/. Assign to the correct series manually.</div>'
        )
    elif routing == 'missing_volumes':
        missing = r.get('missing_vol_nums', [])
        vols = ', '.join(f'vol.{k}' for k in missing)
        rows.append(
            f'<div style="color:#ef4444;font-weight:600;">&#9888; MISSING PRECEDING VOLUMES '
            f'({vols}) — placed in TO FIX MANUALLY/</div>'
        )
        rows.append(
            f'<div><b>Series:</b> {r["series_name"]} vol.{r["volume_number"]}</div>'
        )
        rows.append(
            '<div style="color:#6b7280;">Find/download the missing volumes, '
            'then move this file manually.</div>'
        )
    elif routing == 'file_conflict':
        conflict = r.get('conflicting_path', 'unknown')
        rows.append(
            '<div style="color:#ef4444;font-weight:600;">&#9888; FILE CONFLICT — '
            'placed in TO FIX MANUALLY/</div>'
        )
        rows.append(f'<div><b>Conflict with:</b> {conflict}</div>')
        rows.append(
            '<div style="color:#6b7280;">A CBZ already exists at the destination. '
            'Resolve the duplicate manually.</div>'
        )
    elif routing == 'cover':
        rows.append(f'<div><b>Type:</b> Cover (≤4 pages) → placed in {r["series_dir"]}/</div>')
    elif routing == 'series':
        created_note = ' (new folder)' if r.get('series_dir_created') else ' (existing folder)'
        rows.append(
            f'<div><b>Type:</b> Series — {r["series_name"]} vol.{r["volume_number"]}</div>'
        )
        rows.append(f'<div><b>Folder:</b> {r["series_dir"]}{created_note}</div>')
    else:  # oneshot
        rows.append(
            f'<div><b>Type:</b> One-shot → placed in {r["series_dir"]}</div>'
        )

    rows.append(f'<div><b>File:</b> {r["cbz_filename"]}</div>')

    if not r.get('author'):
        rows.append(
            '<div style="color:#ef4444;">&#9888; No author found — '
            'filename has no [Author] tag</div>'
        )

    move = r.get('oneshot_move')
    if move:
        rows.append(
            f'<div style="color:#6b7280;margin-top:4px;">'
            f'Moved vol.1 from one-shots:<br>'
            f'&nbsp;&nbsp;Before: {move["from"]}<br>'
            f'&nbsp;&nbsp;After:&nbsp; {move["to"]}</div>'
        )

    rows.append('</div>')
    return '\n'.join(rows)


# ---------------------------------------------------------------------------
# Full HTML builder for success emails
# ---------------------------------------------------------------------------

def _build_success_html(
    downloaded: list[dict],
    not_owned: list[dict],
    other_skipped: list[dict],
    elapsed: str,
) -> str:
    # Summary badge counts
    needs_attention = sum(
        1 for r in downloaded
        if r['routing'] in ('multi_collection', 'missing_volumes', 'file_conflict')
    )

    banner_parts = [_badge(f'{len(downloaded)} downloaded', '#22c55e')]
    if needs_attention:
        banner_parts.append(_badge(f'{needs_attention} needs attention', '#ef4444'))
    if not_owned:
        banner_parts.append(_badge(f'{len(not_owned)} not owned', '#f97316'))
    if other_skipped:
        banner_parts.append(_badge(f'{len(other_skipped)} skipped', '#6b7280'))

    banner_html = ''.join(banner_parts)

    section_head_style = (
        'font-family:sans-serif;font-size:15px;font-weight:700;'
        'color:#374151;margin:20px 0 6px;border-bottom:1px solid #e5e7eb;'
        'padding-bottom:4px;'
    )

    # Downloaded cards
    downloaded_html = ''
    if downloaded:
        cards = '\n'.join(_book_card(r) for r in downloaded)
        downloaded_html = (
            f'<div style="{section_head_style}">Downloaded</div>'
            f'{cards}'
        )

    # Not owned
    not_owned_html = ''
    if not_owned:
        items = ''.join(
            f'<li style="margin:4px 0;">&#9888;&nbsp;<b>{r["url"]}</b> — '
            f'not owned at download time, check your subscription.</li>'
            for r in not_owned
        )
        not_owned_html = (
            f'<div style="{section_head_style}">Not Owned (will retry next run)</div>'
            f'<ul style="font-family:sans-serif;font-size:13px;color:#92400e;'
            f'padding-left:18px;margin:0;">{items}</ul>'
        )

    # Other skipped
    skipped_html = ''
    if other_skipped:
        items = ''.join(
            f'<li style="margin:4px 0;">{r["url"]} '
            f'[{r.get("skip_reason", "unknown")}]</li>'
            for r in other_skipped
        )
        skipped_html = (
            f'<div style="{section_head_style}">Skipped</div>'
            f'<ul style="font-family:sans-serif;font-size:13px;color:#6b7280;'
            f'padding-left:18px;margin:0;">{items}</ul>'
        )

    total_skipped = len(not_owned) + len(other_skipped)
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f3f4f6;padding:24px 0;">
  <tr><td>
    <table width="600" align="center" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1);
                  max-width:600px;width:100%;">
      <!-- header bar -->
      <tr>
        <td style="background:#1f2937;padding:16px 20px;border-radius:8px 8px 0 0;">
          <span style="font-family:sans-serif;font-size:18px;font-weight:700;
                       color:#ffffff;">FakkuDownloader</span>
          <span style="font-family:sans-serif;font-size:13px;color:#9ca3af;
                       margin-left:10px;">run complete</span>
        </td>
      </tr>
      <!-- summary banner -->
      <tr>
        <td style="padding:14px 20px;background:#f9fafb;
                   border-bottom:1px solid #e5e7eb;">
          {banner_html}
          <span style="font-family:sans-serif;font-size:12px;color:#6b7280;
                       margin-left:8px;">Elapsed: {elapsed}</span>
        </td>
      </tr>
      <!-- body -->
      <tr>
        <td style="padding:16px 20px;">
          <p style="font-family:sans-serif;font-size:13px;color:#374151;margin:0 0 12px;">
            {len(downloaded)} book(s) downloaded, {total_skipped} skipped.
          </p>
          {downloaded_html}
          {not_owned_html}
          {skipped_html}
        </td>
      </tr>
      <!-- footer -->
      <tr>
        <td style="padding:12px 20px;background:#f9fafb;border-radius:0 0 8px 8px;
                   border-top:1px solid #e5e7eb;">
          <span style="font-family:sans-serif;font-size:11px;color:#9ca3af;">
            Generated by FakkuDownloaderV2
          </span>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# SMTP transport
# ---------------------------------------------------------------------------

def _send(config: Config, subject: str, body: str, html: str | None = None) -> None:
    if html:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))
    else:
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_success(config: Config, reports: list[dict], elapsed: str, dry_run: bool = False) -> None:
    downloaded = [r for r in reports if not r.get('skipped')]
    skipped    = [r for r in reports if r.get('skipped')]

    not_owned     = [r for r in skipped if r.get('skip_reason') == 'not owned']
    other_skipped = [r for r in skipped if r.get('skip_reason') != 'not owned']

    not_owned_count   = len(not_owned)
    other_skipped_count = len(other_skipped)

    subject = f'{"[DRY RUN] " if dry_run else ""}Run complete: {len(downloaded)} downloaded'
    if not_owned_count:
        subject += f', {not_owned_count} not owned'
    if other_skipped_count:
        subject += f', {other_skipped_count} skipped'

    # Plain-text fallback (unchanged from original)
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
            elif routing == 'file_conflict':
                lines.append('  Type:   *** FILE CONFLICT — placed in TO FIX MANUALLY/')
                lines.append(f'  Conflict with: {r.get("conflicting_path", "unknown")}')
                lines.append('          A CBZ already exists at the destination. Resolve the duplicate manually.')
            elif routing == 'cover':
                lines.append(f'  Type:   Cover (≤4 pages) → placed in {r["series_dir"]}/')
            elif routing == 'series':
                sdir = r['series_dir']
                created_note = ' (new folder)' if r.get('series_dir_created') else ' (existing folder)'
                lines.append(f'  Type:   Series — {r["series_name"]} vol.{r["volume_number"]}')
                lines.append(f'  Folder: {sdir}{created_note}')
            else:
                lines.append(f'  Type:   One-shot → placed in {r["series_dir"]}')

            lines.append(f'  File:   {r["cbz_filename"]}')

            if not r.get('author'):
                lines.append('  Author: *** NOT FOUND — filename has no [Author] tag')

            move = r.get('oneshot_move')
            if move:
                lines.append(f'  Moved vol.1 from one-shots:')
                lines.append(f'    Before: {move["from"]}')
                lines.append(f'    After:  {move["to"]}')

    if not_owned:
        lines += ['', '=' * 60, 'NOT OWNED (will retry next run)', '=' * 60]
        for r in not_owned:
            lines.append(f'  *** {r["url"]}')
            lines.append('      This book was not owned at download time. Check your subscription.')

    if other_skipped:
        lines += ['', '=' * 60, 'SKIPPED', '=' * 60]
        for r in other_skipped:
            lines.append(f'  {r["url"]}  [{r.get("skip_reason", "unknown")}]')

    html = _build_success_html(downloaded, not_owned, other_skipped, elapsed)
    _send(config, subject, '\n'.join(lines), html=html)


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
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
  <tr><td>
    <table width="600" align="center" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1);max-width:600px;width:100%;">
      <tr>
        <td style="background:#991b1b;padding:16px 20px;border-radius:8px 8px 0 0;">
          <span style="font-family:sans-serif;font-size:18px;font-weight:700;
                       color:#ffffff;">FakkuDownloader — ERROR</span>
        </td>
      </tr>
      <tr>
        <td style="padding:20px;font-family:sans-serif;font-size:13px;color:#1f2937;">
          <p style="margin:0 0 8px;"><b>URL:</b> {url}</p>
          <p style="margin:0 0 8px;"><b>Location:</b> {location}</p>
          <p style="margin:0 0 16px;color:#ef4444;font-weight:600;">
            <b>Error:</b> {error}
          </p>
          <p style="margin:0 0 4px;font-weight:600;">Traceback:</p>
          <pre style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;
                      padding:12px;font-size:12px;overflow-x:auto;
                      white-space:pre-wrap;word-break:break-all;">{trace}</pre>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    _send(config, 'ERROR: Run halted', body, html=html)


def send_warning(config: Config, subject: str, body: str) -> None:
    _send(config, f'Warning: {subject}', body)
