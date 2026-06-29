"""Members-area auth-flow HTML pages — extracted from lead_server.py
2026-06-28 as refactor slice #6c-part1. The three pages a member sees
BEFORE they're authenticated (login, verify-code, profile-onboarding).

What's here:
  - login_html():         email + Google login + "Join for $99" CTA
  - code_html():          6-digit code verification
  - onboarding_html():    first-time profile completion (BYOK consent)

Each is a pure function: takes a message / row / etc., returns the
final HTML string. Zero state, zero DB access. All chrome (esc, _page)
comes from html_chrome. SITE injected via configure() so the "Join"
link in login_html points at the right place.

Members landing page (members_html) is NOT here — that one's tangled
with vault state, GitHub team status, agent status, etc. and is best
carved as #6c-part2 once those subsystems are clearer.
"""
from __future__ import annotations
from html_chrome import esc, _page


_SITE = ""  # populated by configure()


def configure(*, site):
    """Wire in the SITE URL — referenced by login_html for the
    "Not a member yet? Join for $99/month" link."""
    global _SITE
    _SITE = site or ""


def login_html(message=""):
    note = ("<p class=\"form-note\" style=\"color:#dc2626\">"
            + esc(message) + "</p>") if message else ""
    google_btn = (
        "<a href=\"/members/login/google/start\" "
        "class=\"btn btn-lg\" "
        "style=\"display:inline-flex;align-items:center;gap:.6rem;"
        "background:#fff;color:#1f1f1f;border:1px solid #dadce0;"
        "padding:.7rem 1.4rem;border-radius:8px;text-decoration:none;"
        "font-weight:500\">"
        "<svg width=\"18\" height=\"18\" viewBox=\"0 0 18 18\" xmlns=\"http://www.w3.org/2000/svg\">"
        "<path d=\"M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615z\" fill=\"#4285F4\"/>"
        "<path d=\"M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z\" fill=\"#34A853\"/>"
        "<path d=\"M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z\" fill=\"#FBBC05\"/>"
        "<path d=\"M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z\" fill=\"#EA4335\"/>"
        "</svg>Continue with Google</a>")
    divider = (
        "<div style=\"display:flex;align-items:center;gap:.8rem;"
        "margin:1.6rem auto;max-width:340px;color:#9ca3af;font-size:.85rem\">"
        "<hr style=\"flex:1;border:none;border-top:1px solid #e5e7eb\">"
        "<span>or sign in with email</span>"
        "<hr style=\"flex:1;border:none;border-top:1px solid #e5e7eb\"></div>")
    body = ("<section class=\"section\"><div class=\"container narrow center\">"
            "<span class=\"eyebrow\">Members</span><h1>The Automation Founders</h1>"
            "<p class=\"lead\">Sign in to your members area.</p>"
            "<div style=\"margin:1rem 0 0\">" + google_btn + "</div>"
            + divider +
            "<form method=\"POST\" action=\"/members/login\" class=\"signup center\" style=\"justify-content:center\">"
            "<input type=\"email\" name=\"email\" required placeholder=\"you@company.com\" aria-label=\"Email\">"
            "<button class=\"btn btn-primary btn-lg\" type=\"submit\">Send code</button></form>"
            "<p class=\"muted small\" style=\"margin-top:.6rem\">We'll email you a 6-digit code. Works for any registered email.</p>"
            + note +
            "<p class=\"muted small\">Not a member yet? <a href=\"" + _SITE +
            "/community.html\">Join for $99/month</a>.</p></div></section>")
    return _page("Members · The Automation Founders", body)


def code_html(email, message="", ok=False):
    color = "#16a34a" if ok else "#dc2626"
    note = ("<p class=\"form-note\" style=\"color:" + color + "\">"
            + esc(message) + "</p>") if message else ""
    body = ("<section class=\"section\"><div class=\"container narrow center\">"
            "<span class=\"eyebrow\">Members</span><h1>Check your email</h1>"
            "<p class=\"lead\">Enter the 6-digit code we sent to <strong>" + esc(email) + "</strong>.</p>"
            "<form method=\"POST\" action=\"/members/verify\" class=\"signup center\" style=\"justify-content:center\">"
            "<input type=\"hidden\" name=\"email\" value=\"" + esc(email) + "\">"
            "<input type=\"text\" name=\"code\" inputmode=\"numeric\" pattern=\"[0-9]*\" maxlength=\"6\" "
            "required placeholder=\"123456\" aria-label=\"6-digit code\" autocomplete=\"one-time-code\">"
            "<button class=\"btn btn-primary btn-lg\" type=\"submit\">Verify</button></form>" + note +
            "<p class=\"muted small\"><a href=\"/members\">Use a different email</a></p></div></section>")
    return _page("Members · The Automation Founders", body)


def onboarding_html(email, row=None, message=""):
    row = row or {}
    note = ("<p class=\"form-note\" style=\"color:#dc2626\">"
            + esc(message) + "</p>") if message else ""

    def val(k):
        return esc(row.get(k) or "")

    body = ("<section class=\"section\"><div class=\"container narrow\">"
            "<span class=\"eyebrow\">Welcome — one quick step</span>"
            "<h1>Complete your profile</h1>"
            "<p class=\"lead\">Confirm your details so we can set up your app access. Takes 20 seconds.</p>"
            "<form method=\"POST\" action=\"/members/profile\" class=\"contact-form card\">"
            "<label>First name<input type=\"text\" name=\"first_name\" required value=\"" + val("first_name") + "\"></label>"
            "<label>Last name<input type=\"text\" name=\"last_name\" value=\"" + val("last_name") + "\"></label>"
            "<label>Phone<input type=\"text\" name=\"phone\" value=\"" + val("phone") + "\"></label>"
            "<label>Country<input type=\"text\" name=\"country\" value=\"" + val("country") + "\"></label>"
            "<label style=\"display:flex;gap:.6rem;align-items:flex-start;font-weight:500\">"
            "<input type=\"checkbox\" name=\"byok\" value=\"1\" required style=\"margin-top:.3rem\">"
            "<span class=\"muted\">I understand <strong>AdsGPT and Callified are usage-based</strong> — I'll connect my "
            "own API keys to run them, and all app access lasts only while my membership is active.</span></label>"
            "<button class=\"btn btn-primary btn-lg\" type=\"submit\">Enter the members area</button>" + note +
            "</form></div></section>")
    return _page("Welcome · The Automation Founders", body)
