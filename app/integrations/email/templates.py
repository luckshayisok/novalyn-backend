"""Transactional email templates.

Each function returns an EmailMessage with a plain-text and an HTML body. All
caller-supplied values are escaped before they reach the HTML.
"""

from html import escape

from app.integrations.email.base import EmailMessage

BRAND = "NOVALYN"
TAGLINE = "Every document. Every deadline. Under control."

_NAVY = "#0f1f3d"
_ACCENT = "#3b5bdb"


def _layout(heading: str, paragraphs: list[str], action_label: str, action_url: str, footnote: str) -> str:
    body = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:24px;color:#334155;">{p}</p>'
        for p in paragraphs
    )
    url = escape(action_url, quote=True)
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:32px 16px;background:#f5f6f8;font-family:Inter,Segoe UI,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center">
        <table role="presentation" width="520" cellpadding="0" cellspacing="0"
               style="max-width:520px;background:#ffffff;border:1px solid #e2e8f0;border-radius:8px;">
          <tr><td style="padding:28px 32px 0;">
            <div style="font-size:15px;font-weight:700;letter-spacing:0.14em;color:{_NAVY};">{BRAND}</div>
          </td></tr>
          <tr><td style="padding:24px 32px 8px;">
            <h1 style="margin:0 0 16px;font-size:20px;line-height:28px;color:{_NAVY};">{escape(heading)}</h1>
            {body}
            <p style="margin:24px 0;">
              <a href="{url}"
                 style="display:inline-block;padding:11px 20px;background:{_ACCENT};color:#ffffff;
                        text-decoration:none;border-radius:6px;font-size:14px;font-weight:600;">
                {escape(action_label)}
              </a>
            </p>
            <p style="margin:0 0 8px;font-size:13px;line-height:20px;color:#64748b;">
              If the button does not work, copy this link into your browser:
            </p>
            <p style="margin:0 0 24px;font-size:13px;line-height:20px;word-break:break-all;">
              <a href="{url}" style="color:{_ACCENT};">{url}</a>
            </p>
            <p style="margin:0 0 28px;font-size:13px;line-height:20px;color:#64748b;">{escape(footnote)}</p>
          </td></tr>
        </table>
        <p style="margin:16px 0 0;font-size:12px;color:#94a3b8;">{BRAND} &middot; {TAGLINE}</p>
      </td></tr>
    </table>
  </body>
</html>"""


def _text(greeting: str, lines: list[str], action_label: str, action_url: str, footnote: str) -> str:
    return "\n\n".join([greeting, *lines, f"{action_label}: {action_url}", footnote, f"{BRAND}\n{TAGLINE}"])


def verification_email(*, to: str, full_name: str, url: str, expires_hours: int) -> EmailMessage:
    heading = "Confirm your email address"
    line = f"Confirm this address to finish setting up your {BRAND} account."
    footnote = (
        f"This link expires in {expires_hours} hours. If you did not create an account, ignore this email."
    )
    return EmailMessage(
        to=to,
        subject=f"Confirm your email for {BRAND}",
        text=_text(f"Hi {full_name},", [line], "Confirm email", url, footnote),
        html=_layout(heading, [f"Hi {escape(full_name)},", escape(line)], "Confirm email", url, footnote),
    )


def password_reset_email(*, to: str, full_name: str, url: str, expires_minutes: int) -> EmailMessage:
    heading = "Reset your password"
    line = f"We received a request to reset the password for your {BRAND} account."
    footnote = (
        f"This link expires in {expires_minutes} minutes and can be used once. "
        "If you did not ask for it, ignore this email; your password stays the same."
    )
    return EmailMessage(
        to=to,
        subject=f"Reset your {BRAND} password",
        text=_text(f"Hi {full_name},", [line], "Reset password", url, footnote),
        html=_layout(heading, [f"Hi {escape(full_name)},", escape(line)], "Reset password", url, footnote),
    )


def invitation_email(
    *,
    to: str,
    organization_name: str,
    inviter_name: str,
    role_label: str,
    url: str,
    expires_days: int,
) -> EmailMessage:
    heading = f"Join {organization_name} on {BRAND}"
    line = (
        f"{inviter_name} invited you to join {organization_name} on {BRAND} as {role_label}. "
        f"{BRAND} keeps business documents, certificates and renewal deadlines in one place."
    )
    footnote = (
        f"This invitation expires in {expires_days} days. If you were not expecting it, ignore this email."
    )
    return EmailMessage(
        to=to,
        subject=f"{inviter_name} invited you to {organization_name} on {BRAND}",
        text=_text("Hello,", [line], "Accept invitation", url, footnote),
        html=_layout(heading, [escape(line)], "Accept invitation", url, footnote),
    )


def notification_email(
    *,
    to: str,
    full_name: str,
    subject: str,
    heading: str,
    lines: list[str],
    action_label: str,
    url: str,
    organization_name: str,
) -> EmailMessage:
    """A reminder or workflow notice: expiring documents, renewals, review outcomes."""
    footnote = (
        f"You are receiving this because you are a member of {organization_name} on {BRAND}. "
        "Owners and admins can change reminder settings in the organization settings."
    )
    return EmailMessage(
        to=to,
        subject=subject,
        text=_text(f"Hi {full_name},", lines, action_label, url, footnote),
        html=_layout(
            heading,
            [f"Hi {escape(full_name)},", *(escape(line) for line in lines)],
            action_label,
            url,
            footnote,
        ),
    )
