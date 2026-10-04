# Dashboard

A password-protected page on Vercel showing every application, email and LinkedIn request jobpilot has made.
The laptop publishes a fresh snapshot every 30 minutes while `jobpilot run` is running (and on `jobpilot dashboard`).

One-time setup, from this folder:

    npm i -g vercel
    vercel login                                   # your own Vercel account
    vercel link                                    # create a project, e.g. "jobpilot-dashboard"
    vercel env add DASHBOARD_PASSWORD production   # pick a password; you type it on the site
    cd .. && jobpilot dashboard                    # first publish

Only `public/` is served as files. `data.json` is reachable only through `/api/data` with the password.
