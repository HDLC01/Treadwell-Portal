# Security policy

Treadwell runs two connected web apps, and this policy covers both. The same file is in both
repositories.

- **Proposal Tool**, at `proposals.wetreadwell.com`. Treadwell staff use it to price jobs and
  write proposals. Only Treadwell Google accounts can sign in.
- **Customer Portal**, at `portal.wetreadwell.com`. Our customers use it to read, discuss and
  approve their proposals.

## Supported versions

Both apps are hosted services, not software anyone installs. Only the version running at those
two addresses is supported, and every fix goes live there. There are no older releases to patch.

## Reporting a vulnerability

Please report it privately. Do not open a public issue, pull request or discussion.

- Email **security@wetreadwell.com**, or
- Use **Report a vulnerability** on the Security tab of the
  [Customer Portal repository](https://github.com/HDLC01/Treadwell-Portal/security) on GitHub.

Please include:

- which app and page it affects
- what you found and what someone could do with it
- the steps to reproduce it, and screenshots or a short video if you have them
- how we can reach you

If you saw someone else's information while testing, tell us what you saw. Do not keep a copy.

## What happens after you report

- We confirm we received it within 3 business days (US Central time).
- Within 10 business days we tell you whether we agree it is a problem and what we plan to do.
- We update you at least every two weeks until it is fixed.
- We aim to fix serious problems, such as anything that exposes customer data or lets someone
  into a staff account, within 30 days, and sooner when we can.
- When it is fixed we tell you, and we credit you by name in the fix notes if you want.

We do not run a paid bug bounty.

## What this covers

In scope:

- `proposals.wetreadwell.com` and `portal.wetreadwell.com`
- the code in these two repositories

Not covered:

- `wetreadwell.com`, our marketing website, which is hosted and managed separately. You can
  still email security@wetreadwell.com about it and we will pass it on.
- The outside services these apps use, such as Google, Supabase, Dropbox and our email provider.
  Please report those to the service directly.
- Tricking our staff or customers (phishing, phone calls, fake emails), or physical access.
- Flooding the apps with traffic, or sending mass email or sign-in codes through them.
- Scanner results or missing "best practice" settings with no real way to misuse them.

## Rules for testing

- Only use accounts and proposals that belong to you. The staff tool is for Treadwell staff only,
  so do not try to get into it.
- Touch as little data as you need to show the problem. Stop and report as soon as you see
  something that isn't yours. Never change or delete anyone else's data.
- Do not run heavy automated scans. Do not do anything that slows the apps down or sends email
  or text messages to real people. The portal emails real customers.
- Give us a fair chance to fix the problem before you tell anyone else about it.

If you follow these rules in good faith, we will treat your report as help. We will not take
legal action against you over it.

## For people working on this code

- Never commit passwords, API keys, tokens, `.env` files, server details or customer files.
- If a secret is committed by mistake, tell security@wetreadwell.com straight away. Deleting the
  commit is not enough, because the secret stays in the history, so it must be replaced.
- Every change goes to the staging site first and reaches the live sites only after it has been
  checked there.
