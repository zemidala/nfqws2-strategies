# Relay

Routers running nfqws2-ui send strategies here, so nobody needs a GitHub account to share one. The relay keeps
each submission as a file in the `inbox` branch and starts the intake Action, which checks it and writes the base.
The router's installation number stays here: the base only sees a salted hash.

## Deploy (Cloudflare, free plan)

1. GitHub → Settings → Developer settings → Fine-grained tokens → new token for `zemidala/nfqws2-strategies` only,
   permission **Contents: Read and write** (needed for files and for `repository_dispatch`), expiry 1 year.
2. In this folder:
   ```
   npx wrangler login
   npx wrangler deploy
   npx wrangler secret put GH_TOKEN     # the token from step 1
   npx wrangler secret put SALT         # any long random string; never change it, or everyone's counters reset
   ```
3. The address is `https://nfqws2-strategies.<account>.workers.dev` — it goes into nfqws2-ui (`COMMUNITY_RELAY`).

The `inbox` branch must exist (an orphan branch with this README is enough).
