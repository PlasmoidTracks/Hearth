# Vendored frontend assets

Hearth ships **no CDN dependencies** — everything the UI needs is served from this
`/static/` tree, so the app renders with zero third-party requests and a strict
Content-Security-Policy (`script-src 'self'`) can be enforced. See DEC-018.

## tailwind.js  (REQUIRED)

This is Tailwind CSS's runtime (JIT-in-browser) build. It is **not committed** to the repo
(it's ~400 KB of third-party JS). Fetch it once into this directory:

```bash
# from the project root
mkdir -p hearth/static/vendor
curl -fsSL https://cdn.tailwindcss.com -o hearth/static/vendor/tailwind.js
# or, with wget:
wget -O hearth/static/vendor/tailwind.js https://cdn.tailwindcss.com
```

Verify it's real JS (not an HTML error page):
```bash
head -c 80 hearth/static/vendor/tailwind.js   # should show JS, not "<!DOCTYPE html>"
```

> Note: `cdn.tailwindcss.com` serves Tailwind's *development* build (compiles CSS in the
> browser). It's convenient and needs no build step, which matches our project decision.
> For a leaner production bundle you could instead run Tailwind's CLI to emit a static
> `.css` and drop the `<script>` — that adds a Node/build step and is tracked as a follow-up.

## HTMX  (not used)

An earlier plan mentioned HTMX; the frontend ended up using plain `fetch()` in
`app.js`/`chat.js`, so **HTMX is not required** and is intentionally not vendored.

## Pinning / integrity
For stronger supply-chain assurance, record the file's hash after fetching:
```bash
sha256sum hearth/static/vendor/tailwind.js
```
and note it here so future fetches can be verified against a known-good value.
