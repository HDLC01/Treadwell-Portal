"""The OTP rate limiter is a security control (blocks email-bombing + the
'resend to reset the attempt cap' brute-force). Pin its window + cooldown."""
import ratelimit


def test_ip_window():
    ip = "test-ip-1"
    assert all(ratelimit.allow_ip(ip, 3, 60) for _ in range(3))
    assert ratelimit.allow_ip(ip, 3, 60) is False


def test_otp_cooldown_blocks_immediate_resend():
    ok, wait = ratelimit.allow_otp("cooldown@x.com", 5, 900, 45)
    assert ok is True and wait == 0
    ok2, wait2 = ratelimit.allow_otp("cooldown@x.com", 5, 900, 45)
    assert ok2 is False and wait2 > 0  # within cooldown


def test_otp_per_email_window():
    e = "window@x.com"
    assert all(ratelimit.allow_otp(e, 3, 900, 0)[0] for _ in range(3))  # cooldown=0 isolates the window cap
    assert ratelimit.allow_otp(e, 3, 900, 0)[0] is False
