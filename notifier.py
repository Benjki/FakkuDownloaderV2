"""SMTP email notifications."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import Config

SUBJECT_PREFIX = '[FakkuDL]'

_ATTENTION_ROUTINGS = ('multi_collection', 'missing_volumes', 'file_conflict')


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


def _sort_key(r: dict) -> str:
    """Sort key: numbers first, then A-Z (case-insensitive)."""
    name = r.get('display_name', r.get('original_filename', ''))
    return name.lower()


# ---------------------------------------------------------------------------
# Downloaded section helpers
# ---------------------------------------------------------------------------

def _dl_attention_item(r: dict) -> str:
    """Render a single downloaded book that needs attention."""
    routing = r['routing']
    author = r.get('author') or 'unknown'
    pages = r.get('pages', '?')

    lines = [
        f'<div style="font-size:13px;font-weight:700;color:#1f2937;">'
        f'{r["display_name"]} '
        f'<span style="font-weight:400;color:#6b7280;font-size:12px;">'
        f'&middot; {author} &middot; {pages} pg</span></div>'
    ]

    if routing == 'missing_volumes':
        missing = r.get('missing_vol_nums', [])
        vols = ', '.join(f'vol.{k}' for k in missing)
        lines.append(
            f'<div style="font-size:12px;color:#dc2626;margin-top:3px;">'
            f'MISSING VOLUMES ({vols}) &mdash; {r["series_name"]} vol.{r["volume_number"]}</div>'
        )
    elif routing == 'multi_collection':
        lines.append(
            '<div style="font-size:12px;color:#dc2626;margin-top:3px;">'
            'MULTIPLE COLLECTIONS &mdash; assign to correct series manually</div>'
        )
    elif routing == 'file_conflict':
        conflict = r.get('conflicting_path', 'unknown')
        lines.append(
            '<div style="font-size:12px;color:#dc2626;margin-top:3px;">'
            'FILE CONFLICT &mdash; CBZ already exists at destination</div>'
        )
        lines.append(
            f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">'
            f'Conflict: {conflict}</div>'
        )

    lines.append(
        f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">'
        f'TO FIX MANUALLY/ &middot; {r["cbz_filename"]}</div>'
    )

    return '\n'.join(lines)


def _dl_table_row(r: dict, odd: bool) -> str:
    """Render a single OK downloaded book as a table row."""
    bg = 'background:#f9fafb;' if odd else ''
    routing = r.get('routing', 'oneshot')

    # Title cell — may include warnings
    title_parts = [r['display_name']]
    if not r.get('author'):
        title_parts.append(
            '<span style="color:#ef4444;font-size:11px;margin-left:4px;">'
            '&#9888; no author</span>'
        )
    retries = r.get('page_retries', 0)
    if retries:
        label = 'retry' if retries == 1 else 'retries'
        title_parts.append(
            f'<span style="color:#f97316;font-size:11px;margin-left:4px;">'
            f'&#9888; {retries} {label}</span>'
        )
    title_html = ''.join(title_parts)

    # Author cell
    if r.get('author'):
        author_html = f'<td style="padding:8px 12px;color:#6b7280;">{r["author"]}</td>'
    else:
        author_html = '<td style="padding:8px 12px;color:#ef4444;font-style:italic;">NOT FOUND</td>'

    # Pages cell
    pages_html = f'<td style="padding:8px 8px;text-align:center;color:#6b7280;">{r.get("pages", "?")}</td>'

    # Destination cell
    if routing == 'series':
        new_tag = ' <span style="color:#22c55e;font-weight:600;font-size:10px;">(new)</span>' if r.get('series_dir_created') else ''
        dest = f'{r["series_dir"]}/{new_tag} &middot; vol.{r["volume_number"]}'
    elif routing == 'cover':
        dest = f'{r["series_dir"]}/'
    else:  # oneshot
        dest = f'{r["series_dir"]}/'
    dest_html = f'<td style="padding:8px 12px;color:#6b7280;">{dest}</td>'

    row = (
        f'<tr style="{bg}border-bottom:1px solid #e5e7eb;">'
        f'<td style="padding:8px 12px;font-weight:600;">{title_html}</td>'
        f'{author_html}{pages_html}{dest_html}'
        f'</tr>'
    )

    # Oneshot move sub-row
    move = r.get('oneshot_move')
    if move:
        row += (
            f'<tr style="background:#f0fdf4;border-bottom:1px solid #e5e7eb;">'
            f'<td colspan="4" style="padding:4px 12px 8px;font-size:11px;color:#6b7280;">'
            f'<b style="color:#374151;">{r["display_name"]}:</b> '
            f'&#8618; Moved vol.1 from OneShots:<br>'
            f'&nbsp;&nbsp;From: {move["from"]}<br>'
            f'&nbsp;&nbsp;To: {move["to"]}'
            f'</td></tr>'
        )

    return row


def _group_header(label: str, count: int, border_color: str, bg_color: str, text_color: str) -> str:
    return (
        f'<div style="background:{bg_color};border-left:4px solid {border_color};'
        f'padding:3px 12px;margin-bottom:8px;border-radius:0 4px 4px 0;">'
        f'<span style="font-size:12px;font-weight:700;color:{text_color};'
        f'text-transform:uppercase;letter-spacing:0.5px;">{label} ({count})</span>'
        f'</div>'
    )


def _build_downloaded_html(downloaded: list[dict]) -> str:
    """Build the full Downloaded section with grouped layout."""
    if not downloaded:
        return ''

    attention = sorted(
        [r for r in downloaded if r['routing'] in _ATTENTION_ROUTINGS],
        key=_sort_key,
    )
    series = sorted(
        [r for r in downloaded if r['routing'] == 'series' and r['routing'] not in _ATTENTION_ROUTINGS],
        key=_sort_key,
    )
    oneshots = sorted(
        [r for r in downloaded if r['routing'] == 'oneshot'],
        key=_sort_key,
    )
    covers = sorted(
        [r for r in downloaded if r['routing'] == 'cover'],
        key=_sort_key,
    )

    parts = [
        '<div style="font-family:sans-serif;font-size:15px;font-weight:700;color:#374151;'
        f'margin:0 0 16px;border-bottom:2px solid #1f2937;padding-bottom:6px;">'
        f'Downloaded ({len(downloaded)})</div>'
    ]

    # Attention box
    if attention:
        items_html = []
        for i, r in enumerate(attention):
            border = 'border-bottom:1px solid #fecaca;' if i < len(attention) - 1 else ''
            items_html.append(
                f'<div style="padding:8px 0;{border}">'
                f'{_dl_attention_item(r)}</div>'
            )
        parts.append(
            '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;'
            'padding:14px 16px;margin-bottom:20px;">'
            '<div style="font-size:13px;font-weight:700;color:#dc2626;margin-bottom:10px;'
            f'text-transform:uppercase;letter-spacing:0.5px;">&#9888; Needs Attention ({len(attention)})</div>'
            + '\n'.join(items_html)
            + '</div>'
        )

    # Series group
    if series:
        parts.append(_group_header('Series', len(series), '#22c55e', '#f0fdf4', '#16a34a'))
        rows = ''.join(_dl_table_row(r, i % 2 == 1) for i, r in enumerate(series))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px;color:#1f2937;border-collapse:collapse;margin-bottom:20px;">'
            f'{rows}</table>'
        )

    # One-shots group
    if oneshots:
        parts.append(_group_header('One-shots', len(oneshots), '#3b82f6', '#eff6ff', '#2563eb'))
        rows = ''.join(_dl_table_row(r, i % 2 == 1) for i, r in enumerate(oneshots))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px;color:#1f2937;border-collapse:collapse;margin-bottom:20px;">'
            f'{rows}</table>'
        )

    # Covers group
    if covers:
        parts.append(_group_header('Covers', len(covers), '#9ca3af', '#f3f4f6', '#6b7280'))
        rows = ''.join(_dl_table_row(r, i % 2 == 1) for i, r in enumerate(covers))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px;color:#1f2937;border-collapse:collapse;margin-bottom:20px;">'
            f'{rows}</table>'
        )

    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# ToPlace section helpers
# ---------------------------------------------------------------------------

def _tp_attention_item(r: dict) -> str:
    """Render a single toplace item that needs attention (error or routing issue)."""
    if r.get('error'):
        lines = [
            f'<div style="font-size:13px;font-weight:700;color:#1f2937;">{r["original_filename"]}</div>',
            f'<div style="font-size:12px;color:#dc2626;margin-top:3px;">ERROR: {r["error"]}</div>',
            f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">Original: {r["original_filename"]}</div>',
        ]
        return '\n'.join(lines)

    routing = r.get('routing', 'oneshot')
    author = r.get('author') or 'unknown'
    pages = r.get('pages', '?')

    lines = [
        f'<div style="font-size:13px;font-weight:700;color:#1f2937;">'
        f'{r["display_name"]} '
        f'<span style="font-weight:400;color:#6b7280;font-size:12px;">'
        f'&middot; {author} &middot; {pages} pg</span></div>'
    ]

    if routing == 'missing_volumes':
        missing = r.get('missing_vol_nums', [])
        vols = ', '.join(f'vol.{k}' for k in missing)
        lines.append(
            f'<div style="font-size:12px;color:#dc2626;margin-top:3px;">'
            f'MISSING VOLUMES ({vols}) &mdash; {r["series_name"]} vol.{r["volume_number"]}</div>'
        )
    elif routing == 'file_conflict':
        lines.append(
            '<div style="font-size:12px;color:#dc2626;margin-top:3px;">'
            'FILE CONFLICT &mdash; CBZ already exists at destination</div>'
        )

    lines.append(
        f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">'
        f'TO FIX MANUALLY/ &middot; {r["cbz_filename"]}</div>'
    )
    lines.append(
        f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">'
        f'Original: {r["original_filename"]}</div>'
    )

    return '\n'.join(lines)


def _tp_table_row(r: dict, odd: bool) -> str:
    """Render a single OK toplace book as a table row + original filename sub-row."""
    bg = 'background:#f9fafb;' if odd else ''
    routing = r.get('routing', 'oneshot')

    # Title cell
    title_parts = [r['display_name']]
    if not r.get('author'):
        title_parts.append(
            '<span style="color:#ef4444;font-size:11px;margin-left:4px;">'
            '&#9888; no author</span>'
        )
    title_html = ''.join(title_parts)

    # Author cell
    if r.get('author'):
        author_html = f'<td style="padding:8px 12px;color:#6b7280;">{r["author"]}</td>'
    else:
        author_html = '<td style="padding:8px 12px;color:#ef4444;font-style:italic;">NOT FOUND</td>'

    # Pages cell
    pages_html = f'<td style="padding:8px 8px;text-align:center;color:#6b7280;">{r.get("pages", "?")}</td>'

    # Destination cell
    if routing == 'series':
        dest = f'{r["series_dir"]}/ &middot; vol.{r["volume_number"]}'
    else:  # oneshot
        dest = f'{r["series_dir"]}/'
    dest_html = f'<td style="padding:8px 12px;color:#6b7280;">{dest}</td>'

    row = (
        f'<tr style="{bg}border-bottom:1px solid #e5e7eb;">'
        f'<td style="padding:8px 12px;font-weight:600;">{title_html}</td>'
        f'{author_html}{pages_html}{dest_html}'
        f'</tr>'
    )

    # Original filename sub-row
    row += (
        f'<tr>'
        f'<td colspan="4" style="padding:2px 12px 8px;font-size:11px;color:#9ca3af;">'
        f'Original: {r["original_filename"]} &rarr; {r["cbz_filename"]}</td>'
        f'</tr>'
    )

    # Oneshot move sub-row
    move = r.get('oneshot_move')
    if move:
        row += (
            f'<tr style="background:#f0fdf4;border-bottom:1px solid #e5e7eb;">'
            f'<td colspan="4" style="padding:4px 12px 8px;font-size:11px;color:#6b7280;">'
            f'<b style="color:#374151;">{r["display_name"]}:</b> '
            f'&#8618; Moved vol.1 from OneShots:<br>'
            f'&nbsp;&nbsp;From: {move["from"]}<br>'
            f'&nbsp;&nbsp;To: {move["to"]}'
            f'</td></tr>'
        )

    return row


def _build_toplace_html(toplace_reports: list[dict]) -> str:
    """Build the full ToPlace section with grouped layout."""
    if not toplace_reports:
        return ''

    errors = sorted(
        [r for r in toplace_reports if r.get('error')],
        key=lambda r: r.get('original_filename', '').lower(),
    )
    placed = [r for r in toplace_reports if not r.get('error')]
    attention = sorted(
        [r for r in placed if r.get('routing') in _ATTENTION_ROUTINGS],
        key=_sort_key,
    )
    ok_placed = [r for r in placed if r.get('routing') not in _ATTENTION_ROUTINGS]
    series = sorted(
        [r for r in ok_placed if r.get('routing') == 'series'],
        key=_sort_key,
    )
    oneshots = sorted(
        [r for r in ok_placed if r.get('routing') in ('oneshot', None)],
        key=_sort_key,
    )

    all_attention = errors + attention
    ok_count = len(ok_placed)

    parts = [
        '<div style="margin-top:28px;padding-top:20px;border-top:2px solid #e5e7eb;">',
        '<div style="font-family:sans-serif;font-size:15px;font-weight:700;color:#374151;'
        f'margin:0 0 16px;border-bottom:2px solid #1f2937;padding-bottom:6px;">'
        f'ToPlace Processing ({len(toplace_reports)})</div>',
    ]

    # Badges
    badge_parts = [_badge(f'{ok_count} placed', '#22c55e')]
    if attention:
        badge_parts.append(_badge(f'{len(attention)} needs attention', '#ef4444'))
    if errors:
        label = 'error' if len(errors) == 1 else 'errors'
        badge_parts.append(_badge(f'{len(errors)} {label}', '#ef4444'))
    parts.append(f'<div style="margin-bottom:12px;">{"".join(badge_parts)}</div>')

    # Attention box (errors + routing issues)
    if all_attention:
        items_html = []
        for i, r in enumerate(all_attention):
            border = 'border-bottom:1px solid #fecaca;' if i < len(all_attention) - 1 else ''
            items_html.append(
                f'<div style="padding:8px 0;{border}">'
                f'{_tp_attention_item(r)}</div>'
            )
        parts.append(
            '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;'
            'padding:14px 16px;margin-bottom:20px;">'
            '<div style="font-size:13px;font-weight:700;color:#dc2626;margin-bottom:10px;'
            f'text-transform:uppercase;letter-spacing:0.5px;">&#9888; Needs Attention ({len(all_attention)})</div>'
            + '\n'.join(items_html)
            + '</div>'
        )

    # Series group
    if series:
        parts.append(_group_header('Series', len(series), '#22c55e', '#f0fdf4', '#16a34a'))
        rows = ''.join(_tp_table_row(r, i % 2 == 1) for i, r in enumerate(series))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px;color:#1f2937;border-collapse:collapse;margin-bottom:16px;">'
            f'{rows}</table>'
        )

    # One-shots group
    if oneshots:
        parts.append(_group_header('One-shots', len(oneshots), '#3b82f6', '#eff6ff', '#2563eb'))
        rows = ''.join(_tp_table_row(r, i % 2 == 1) for i, r in enumerate(oneshots))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px;color:#1f2937;border-collapse:collapse;">'
            f'{rows}</table>'
        )

    parts.append('</div>')
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Full HTML builder for success emails
# ---------------------------------------------------------------------------

def _build_success_html(
    downloaded: list[dict],
    not_owned: list[dict],
    other_skipped: list[dict],
    elapsed: str,
    toplace_reports: list[dict] | None = None,
) -> str:
    # Summary badge counts
    needs_attention = sum(
        1 for r in downloaded
        if r['routing'] in _ATTENTION_ROUTINGS
    )
    total_retries = sum(r.get('page_retries', 0) for r in downloaded)

    banner_parts = [_badge(f'{len(downloaded)} downloaded', '#22c55e')]
    if needs_attention:
        banner_parts.append(_badge(f'{needs_attention} needs attention', '#ef4444'))
    if total_retries:
        banner_parts.append(_badge(f'{total_retries} page {"retry" if total_retries == 1 else "retries"}', '#f97316'))
    if not_owned:
        banner_parts.append(_badge(f'{len(not_owned)} not owned', '#f97316'))
    if other_skipped:
        banner_parts.append(_badge(f'{len(other_skipped)} skipped', '#6b7280'))
    banner_html = ''.join(banner_parts)

    # Downloaded section
    downloaded_html = _build_downloaded_html(downloaded)

    # Not owned
    not_owned_html = ''
    if not_owned:
        items = ''.join(
            f'<div style="font-size:12px;color:#92400e;padding-left:8px;margin-bottom:4px;">'
            f'&#9888; {r["url"]} &mdash; not owned at download time, check your subscription.</div>'
            for r in not_owned
        )
        not_owned_html = (
            '<div style="font-family:sans-serif;font-size:12px;font-weight:700;color:#92400e;'
            'margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #e5e7eb;">'
            'Not Owned (will retry next run)</div>'
            f'{items}'
        )

    # Other skipped
    skipped_html = ''
    if other_skipped:
        items = ''.join(
            f'<div style="font-size:12px;color:#6b7280;padding-left:8px;">'
            f'{r["url"]} &nbsp;[{r.get("skip_reason", "unknown")}]</div>'
            for r in other_skipped
        )
        skipped_html = (
            '<div style="font-family:sans-serif;font-size:12px;font-weight:700;color:#6b7280;'
            'margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #e5e7eb;">'
            'Skipped</div>'
            f'{items}'
        )

    # ToPlace section
    toplace_html = _build_toplace_html(toplace_reports) if toplace_reports else ''

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f3f4f6;padding:24px 0;">
  <tr><td>
    <table width="700" align="center" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1);
                  max-width:700px;width:100%;">
      <!-- header bar -->
      <tr>
        <td style="background:#1f2937;padding:16px 20px;border-radius:8px 8px 0 0;">
          <span style="font-family:sans-serif;font-size:18px;font-weight:700;
                       color:#ffffff;">FakkuDownloader</span>
          <span style="font-family:sans-serif;font-size:13px;color:#9ca3af;
                       margin-left:10px;">run complete &middot; {elapsed}</span>
        </td>
      </tr>
      <!-- summary banner -->
      <tr>
        <td style="padding:14px 20px;background:#f9fafb;
                   border-bottom:1px solid #e5e7eb;">
          {banner_html}
        </td>
      </tr>
      <!-- body -->
      <tr>
        <td style="padding:20px 24px;font-family:sans-serif;">
          {downloaded_html}
          {not_owned_html}
          {skipped_html}
          {toplace_html}
        </td>
      </tr>
      <!-- footer -->
      <tr>
        <td style="padding:12px 20px;background:#f9fafb;border-radius:0 0 8px 8px;
                   border-top:1px solid #e5e7eb;">
          <span style="font-family:sans-serif;font-size:11px;color:#9ca3af;">
            Generated by FakkuDownloaderV2
          </span>
          <span style="float:right;font-family:sans-serif;font-size:11px;color:#9ca3af;">
            <span style="display:inline-block;width:8px;height:8px;background:#22c55e;border-radius:2px;margin-right:2px;vertical-align:middle;"></span>Series &nbsp;
            <span style="display:inline-block;width:8px;height:8px;background:#3b82f6;border-radius:2px;margin-right:2px;vertical-align:middle;"></span>One-shot &nbsp;
            <span style="display:inline-block;width:8px;height:8px;background:#9ca3af;border-radius:2px;margin-right:2px;vertical-align:middle;"></span>Cover &nbsp;
            <span style="display:inline-block;width:8px;height:8px;background:#ef4444;border-radius:2px;margin-right:2px;vertical-align:middle;"></span>Attention
          </span>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Error email HTML builder
# ---------------------------------------------------------------------------

def _build_error_html(
    url: str,
    location: str,
    error: str,
    trace: str,
    completed: list[dict],
) -> str:
    completed_html = ''
    if completed:
        completed_html = _build_downloaded_html(completed)

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 0;">
  <tr><td>
    <table width="700" align="center" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;
                  box-shadow:0 1px 3px rgba(0,0,0,.1);max-width:700px;width:100%;">
      <tr>
        <td style="background:#991b1b;padding:16px 20px;border-radius:8px 8px 0 0;">
          <span style="font-family:sans-serif;font-size:18px;font-weight:700;
                       color:#ffffff;">FakkuDownloader &mdash; ERROR</span>
        </td>
      </tr>
      <tr>
        <td style="padding:20px 24px;font-family:sans-serif;font-size:13px;color:#1f2937;">
          <p style="margin:0 0 8px;"><b>URL:</b> {url}</p>
          <p style="margin:0 0 8px;"><b>Location:</b> {location}</p>
          <p style="margin:0 0 16px;color:#ef4444;font-weight:600;">
            <b>Error:</b> {error}
          </p>
          <p style="margin:0 0 4px;font-weight:600;">Traceback:</p>
          <pre style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;
                      padding:12px;font-size:12px;overflow-x:auto;
                      white-space:pre-wrap;word-break:break-all;">{trace}</pre>
          {completed_html}
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

def send_success(config: Config, reports: list[dict], elapsed: str, dry_run: bool = False, toplace_reports: list[dict] | None = None) -> None:
    downloaded = [r for r in reports if not r.get('skipped')]
    skipped    = [r for r in reports if r.get('skipped')]

    not_owned     = [r for r in skipped if r.get('skip_reason') == 'not owned']
    other_skipped = [r for r in skipped if r.get('skip_reason') != 'not owned']

    not_owned_count   = len(not_owned)
    other_skipped_count = len(other_skipped)

    tp_placed_count = len([r for r in (toplace_reports or []) if not r.get('error')]) if toplace_reports else 0

    subject = f'{"[DRY RUN] " if dry_run else ""}Run complete: {len(downloaded)} downloaded'
    if not_owned_count:
        subject += f', {not_owned_count} not owned'
    if other_skipped_count:
        subject += f', {other_skipped_count} skipped'
    if tp_placed_count:
        subject += f', {tp_placed_count} placed'

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

            retries = r.get('page_retries', 0)
            if retries:
                lines.append(f'  Retries: *** {retries} page timeout(s)')

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

    if toplace_reports:
        lines += ['', '=' * 60, 'TOPLACE PROCESSING', '=' * 60]
        for r in toplace_reports:
            lines.append('')
            if r.get('error'):
                lines.append(f'  {r["original_filename"]}')
                lines.append(f'  *** ERROR: {r["error"]}')
                continue
            lines.append(f'  {r["display_name"]}')
            lines.append(f'  Author:   {r.get("author") or "unknown"}')
            lines.append(f'  Original: {r["original_filename"]}')

            routing = r.get('routing', 'oneshot')
            if routing == 'missing_volumes':
                missing = r.get('missing_vol_nums', [])
                vols = ', '.join(f'vol.{k}' for k in missing)
                lines.append(f'  Type:   *** MISSING PRECEDING VOLUMES ({vols}) — placed in TO FIX MANUALLY/')
                lines.append(f'  Series: {r["series_name"]} vol.{r["volume_number"]}')
            elif routing == 'file_conflict':
                lines.append('  Type:   *** FILE CONFLICT — placed in TO FIX MANUALLY/')
            elif routing == 'series':
                lines.append(f'  Type:   Series — {r["series_name"]} vol.{r["volume_number"]}')
                lines.append(f'  Folder: {r["series_dir"]}')
            else:
                lines.append(f'  Type:   One-shot → placed in {r["series_dir"]}')

            lines.append(f'  File:   {r["cbz_filename"]}')
            lines.append(f'  Dest:   {r["cbz_path"]}')

            move = r.get('oneshot_move')
            if move:
                lines.append(f'  Moved vol.1 from one-shots:')
                lines.append(f'    Before: {move["from"]}')
                lines.append(f'    After:  {move["to"]}')

    html = _build_success_html(downloaded, not_owned, other_skipped, elapsed, toplace_reports=toplace_reports)
    _send(config, subject, '\n'.join(lines), html=html)


def send_error(
    config: Config,
    url: str,
    page: int | None,
    error: str,
    trace: str,
    reports: list[dict] | None = None,
) -> None:
    location = f'page {page}' if page else 'metadata stage'
    completed = [r for r in (reports or []) if not r.get('skipped')]

    body_lines = [
        'Run halted due to an unrecoverable error.',
        '',
        f'URL:      {url}',
        f'Location: {location}',
        f'Error:    {error}',
        '',
        'Traceback:',
        trace,
    ]
    if completed:
        body_lines += ['', '=' * 60, f'COMPLETED BEFORE ERROR ({len(completed)} book(s))', '=' * 60]
        for r in completed:
            body_lines.append(f'  {r["display_name"]}  →  {r.get("cbz_path", "")}')

    html = _build_error_html(url, location, error, trace, completed)
    _send(config, 'ERROR: Run halted', '\n'.join(body_lines), html=html)


def send_warning(config: Config, subject: str, body: str) -> None:
    _send(config, f'Warning: {subject}', body)
