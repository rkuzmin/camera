# Private infrastructure — do not expose in this public repo

This repo (`rkuzmin/camera`) is **public on GitHub**. Roman's real production
domain, admin/contact email, and VPS IP address for the cloud backend must
never appear in code, comments, docs, or examples committed here — not even
partially or as a "current value" note. Don't reconstruct or guess them from
context either.

When docs or examples need a domain, IP, or contact email, use placeholders:
`your-domain.example`, `admin@your-domain.example`, `root@your-server-ip`.
`server/deploy/deploy.sh` requires `CAMERA_HOST` to be set explicitly (no
hardcoded default) for the same reason — don't reintroduce a real host as a
fallback default.

On 2026-07-08 these values (plus the commit author email, which used the same
personal domain) were scrubbed from this repo's git history via
`git filter-repo` (text + mailmap rewrite) and force-pushed. Several already-
merged feature branches on the remote still carry the old, unscrubbed history
and were intentionally left alone pending Roman's decision to delete them.
Also, GitHub retains old commits/diffs on already-merged Pull Request pages
independently of branch history, so the old values may still be visible there
even though `main` and its history are clean — full removal needs a GitHub
Support request if that matters.
