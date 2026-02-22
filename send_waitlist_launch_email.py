#!/usr/bin/env python3
"""
Send launch announcement emails to all Rutgers University waitlist users.

Usage:
    python send_waitlist_launch_email.py                # dry-run (default)
    python send_waitlist_launch_email.py --send          # actually send emails
    python send_waitlist_launch_email.py --send --delay 2  # 2s delay between emails
"""

import os
import sys
import time
import argparse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "dormdish1@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

APP_LINK = "https://apps.apple.com/us/app/dormdish-ai/id6754625089"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.png")


# ── Email HTML ────────────────────────────────────────────────────────────────

def build_email_html(first_name: str) -> str:
    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 20px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
          <!-- Header -->
          <tr>
            <td style="background-color:#16a34a;padding:32px 40px;text-align:center;">
              <img src="cid:dormdish_logo" alt="DormDish" width="60" height="60" style="display:block;margin:0 auto 12px;border-radius:12px;">
              <h1 style="margin:0;color:#ffffff;font-size:28px;font-weight:700;">DormDish</h1>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:40px;">
              <h2 style="margin:0 0 16px;color:#18181b;font-size:22px;font-weight:600;">
                Hey {first_name}, the wait is over!
              </h2>
              <p style="margin:0 0 16px;color:#3f3f46;font-size:16px;line-height:1.6;">
                Great news &mdash; there is no more waitlist! DormDish is now live and ready for you at Rutgers.
              </p>
              <p style="margin:0 0 24px;color:#3f3f46;font-size:16px;line-height:1.6;">
                Download the app today and create your personalized meal plan for <strong>free</strong>. We&rsquo;ll match you with meals from your campus dining halls based on your goals and preferences.
              </p>
              <!-- CTA Button -->
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto 32px;">
                <tr>
                  <td align="center" style="background-color:#16a34a;border-radius:8px;">
                    <a href="{APP_LINK}" target="_blank" style="display:inline-block;padding:14px 32px;color:#ffffff;font-size:16px;font-weight:600;text-decoration:none;">
                      Download DormDish
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0;color:#71717a;font-size:14px;line-height:1.5;">
                Thanks for being an early supporter. We can&rsquo;t wait for you to try it!
              </p>
              <p style="margin:16px 0 0;color:#71717a;font-size:14px;">
                &mdash; The DormDish Team
              </p>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding:20px 40px;background-color:#fafafa;text-align:center;border-top:1px solid #e4e4e7;">
              <p style="margin:0;color:#a1a1aa;font-size:12px;">
                You received this email because you signed up for the DormDish waitlist.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ── Fetch waitlist users ──────────────────────────────────────────────────────

def fetch_rutgers_waitlist_users():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        supabase.table("waitlist_users")
        .select("first_name, email")
        .ilike("university", "%rutgers%")
        .execute()
    )
    return resp.data or []


# ── Send one email ────────────────────────────────────────────────────────────

def send_email(to_email: str, first_name: str) -> bool:
    msg = MIMEMultipart("related")
    msg["From"] = SMTP_EMAIL
    msg["To"] = to_email
    msg["Subject"] = "The waitlist is over — DormDish is live!"

    # HTML + plain text alternative part
    alt = MIMEMultipart("alternative")
    plain = (
        f"Hey {first_name},\n\n"
        "Great news — there is no more waitlist! DormDish is now live and ready "
        "for you at Rutgers.\n\n"
        "Download the app today and create your personalized meal plan for free:\n"
        f"{APP_LINK}\n\n"
        "Thanks for being an early supporter!\n"
        "— The DormDish Team"
    )
    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(build_email_html(first_name), "html"))
    msg.attach(alt)

    # Attach logo inline
    with open(LOGO_PATH, "rb") as f:
        logo = MIMEImage(f.read(), _subtype="png")
        logo.add_header("Content-ID", "<dormdish_logo>")
        logo.add_header("Content-Disposition", "inline", filename="AppIcon.png")
        msg.attach(logo)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Email Rutgers waitlist users about launch")
    parser.add_argument("--send", action="store_true", help="Actually send emails (default is dry-run)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between emails (default: 1)")
    parser.add_argument("--test", type=str, metavar="EMAIL", help="Send a single test email to this address")
    args = parser.parse_args()

    if not SMTP_PASSWORD:
        print("ERROR: SMTP_PASSWORD must be set in .env")
        sys.exit(1)

    if args.test:
        print(f"Sending test email to {args.test}...")
        try:
            send_email(args.test, "TestUser")
            print("Test email sent!")
        except Exception as e:
            print(f"Failed: {e}")
        return

    users = fetch_rutgers_waitlist_users()
    print(f"Found {len(users)} Rutgers waitlist user(s)\n")

    if not users:
        return

    if not args.send:
        print("DRY RUN — no emails will be sent. Use --send to send for real.\n")

    sent = 0
    failed = 0
    for user in users:
        email = user["email"]
        name = user.get("first_name") or "there"

        if args.send:
            try:
                send_email(email, name)
                sent += 1
                print(f"  [SENT]   {email}")
                time.sleep(args.delay)
            except Exception as e:
                failed += 1
                print(f"  [FAILED] {email} — {e}")
        else:
            print(f"  [DRY-RUN] Would email: {email} ({name})")

    print(f"\nDone. Sent: {sent}, Failed: {failed}, Total: {len(users)}")


if __name__ == "__main__":
    main()
