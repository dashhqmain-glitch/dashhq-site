# Dash HQ — Website

Static, dependency-free frontend for the Dash HQ community site. No build step — open `index.html` in a browser or serve the folder.

## Structure
```
index.html      Landing page (markup only)
styles.css      Landing styles
app.js          Landing behaviour (scroll reveals, cube, cursor ghost,
                testimonial marquee, blog/research modals, FAQ, subscribe,
                legal modals, mobile nav, progress bar)
portal.html     Citizens Portal (Discord verification + membership card)
portal.css      Portal styles
portal.js       Portal behaviour (verification states, particles)
assets/         Logo + team photos (PNG)
```

## Run locally
```
npx serve .        # or: python3 -m http.server
```
Then open http://localhost:3000 (or the printed port).

## Integration notes (for backend work)
- **Newsletter** (`app.js`, subscribe form): posts directly to Substack
  `dashhq1.substack.com`. Swap `SUBSTACK_PUB` if the publication changes.
- **Discord verification** (`portal.js`): currently a front-end demo
  (`verify('member'|'notmember')`). Wire `verify()` to a real Discord
  OAuth + guild-membership check. The membership card data
  (name, handle, ID, tier) is hard-coded placeholder in `portal.html`.
- **Apply / Contact**: buttons point to `#join`. Hook to the real
  application form / Discord invite.
- **SEO/social**: `index.html` `<head>` has meta + Open Graph + JSON-LD.
  Upload a real `og-image.png` to the site root and confirm the domain
  (`https://dashhq.site`).
- Contact email in legal modals: `dashhqmain@gmail.com`.

## Fonts
Sora (display) · DM Sans (body) · JetBrains Mono (mono) — loaded from Google Fonts.
