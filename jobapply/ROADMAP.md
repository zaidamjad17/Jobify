# JobApply roadmap

Goal: an MVP that **finishes applications** reliably, then one people can buy.

Status: ⬜ idea · 🟨 approved · 🟦 building · ✅ shipped (version)

---

## 1. Make it actually finish applications (now)

Real run on 2026-11-10: 14 jobs, 1 complete. The rest stopped on questions the profile did not answer, logins,
or forms the agent could not open. Everything here attacks that.

| # | Idea | Why it matters | Status |
|---|---|---|---|
| 1.1 | **Resume import + setup interview.** Drop in a resume (PDF/DOCX); JobApply writes `profile.md` from it, then asks the ~15 questions every form asks (salary range, notice period, visa, languages, "a project you're proud of", "where in 2 years"…) once, in the app. | Most of the run's stuck questions never happen. New users are set up in 5 minutes instead of writing markdown. | ⬜ |
| 1.2 | **Human-help handoff with sound.** When the agent hits a captcha, "verify you're human", a login wall or a 2FA code, it doesn't skip the job: it pauses it, brings that tab to the front, plays a sound and shows a Windows notification ("JobApply needs you: captcha on Teradata"), and offers an **I did it — continue** button. The agent then picks up where it stopped. Meanwhile it keeps working on other jobs in other tabs. | Captchas and logins are today's silent failures. A 10-second human click saves the whole application. | ⬜ |
| 1.3 | **Pre-flight check before searching.** On Start, open each selected board and Gmail, detect "not logged in" / captcha / "unusual traffic", and ask for help (with the 1.2 notification) before the loop begins. | No more "nothing new" from a board that was really blocked. Problems surface once, up front. | ⬜ |
| 1.4 | **Answer bank.** Every answer the user gives goes into a searchable list in the app (edit, delete, reuse). Reworded questions ("why this track?" ≈ "why AI engineering?") match the same answer. | Fewer questions per job; answers improve over time instead of piling up in `profile.md`. | ⬜ |
| 1.5 | **Match score before applying.** Score each found job 0–100 against the profile and criteria, with reasons ("needs 5+ yrs", "US only"). Skip below a threshold *before* writing documents or opening the form. Show the score in History. | 7 of 14 jobs were skipped only after work was spent on them. Cheaper, faster, and the score itself is a selling point. | ⬜ |
| 1.6 | **Final check before hand-off.** After filling, a second pass reads every field on the page, lists required fields still empty, and saves a screenshot of the finished form. History shows "12/12 fields ✓" or "2 required fields empty" plus the screenshot. | You know which tabs are really ready without opening each one. Catches agent mistakes. | ⬜ |
| 1.7 | **Review mode.** One button, "Review next", opens the next ready tab with its summary (what was filled, documents attached, score). You submit, click **Submitted ✓**, and it moves to the next one. Keyboard shortcuts. | The human step is the product's bottleneck; make it a 30-second loop. | ⬜ |
| 1.8 | **ATS playbooks.** Tested step-by-step helpers for the big application systems: LinkedIn Easy Apply, Greenhouse, Lever, Ashby, Workday, SmartRecruiters. The agent uses the playbook instead of exploring each page. | Higher success, fewer AI steps (cost), fewer weird mistakes. Most jobs run on these 6. | ⬜ |
| 1.9 | **Survive restarts.** Keep the job queue, pending refills and tab-to-job mapping on disk. After an app or PC restart, reopen the jobs that need input and continue. | A desktop app gets closed; today an unfinished run is lost. | ⬜ |
| 1.10 | **Human-like pacing per board.** Daily and hourly limits per site (e.g. LinkedIn), random pauses, and no parallel tabs on the same board. | Protects the user's LinkedIn/Indeed account from being flagged, which would kill the product's main channel. | ⬜ |

## 2. Quality of life

| # | Idea | Why | Status |
|---|---|---|---|
| 2.1 | **Criteria editor in the app** (titles, locations, remote, seniority, salary floor, blocklist companies) instead of a markdown file. | Easier setup; structured filters make 1.5 exact. | ⬜ |
| 2.2 | **Company blocklist / "never this company"** button in History. | Users have companies they don't want. | ⬜ |
| 2.3 | **Duplicate detection across boards** (same company + title on LinkedIn and Greenhouse = one job). | No double applications. | ⬜ |
| 2.4 | **Cost meter.** AI cost per job and per day, with a daily budget cap that pauses the loop. | Trust; nobody wants a surprise bill. | ⬜ |
| 2.5 | **Reply tracking.** Read Gmail (with permission) for replies from companies applied to: interview / rejection / assessment, and update the job's status. | Turns JobApply into the user's whole job-search tracker. Strong retention. | ⬜ |
| 2.6 | **Several resume variants** (e.g. "Backend", "AI/ML"): the match picks the closest as the base for tailoring. | Better documents for people targeting two kinds of roles. | ⬜ |
| 2.7 | **Scheduled runs** ("every weekday at 9:00, up to 20 jobs") with the 1.2 notification when it needs you. | Set and forget. | ⬜ |
| 2.8 | **One-click support bundle**: zip of logs and settings (keys removed) to send when something breaks. | Needed once there are users. | ⬜ |

## 3. Make it sellable (after section 1 works)

| # | Idea | Why | Status |
|---|---|---|---|
| 3.1 | **Free trial + license key.** E.g. 15 free applications, then a key bought through Lemon Squeezy or Stripe; the app checks it online, with an offline grace period. | Revenue. | ⬜ |
| 3.2 | **Code-signed installer.** Removes "Windows protected your PC". Needs a code-signing certificate (~$100–300/year, bought by the owner). | That warning loses buyers at the first click. | ⬜ |
| 3.3 | **Landing page + demo video** (apply loop, human-help handoff, review mode). | Sales. | ⬜ |
| 3.4 | **Bundled AI option.** A hosted key in the paid plan, so non-technical users never see an API key; Sign in with ChatGPT stays for Plus/Pro users. | Removes the hardest setup step for normal users. | ⬜ |
| 3.5 | **Privacy page.** What stays on the PC (profile, logins, history) and what goes to the AI provider. | Job seekers share sensitive data; trust is a feature. | ⬜ |
| 3.6 | **Crash reports (opt-in)** so failures on users' PCs reach us. | We can't fix what we can't see. | ⬜ |

## Shipped

- ✅ 0.2.2: Finish applications once their questions are answered; History tracker (Submitted ✓, fill again, show tab).
- ✅ 0.2.1: Typing in Questions while running; deeper search instead of stopping after one round.
- ✅ 0.2.0: Sign in with ChatGPT (no client ID); in-app updates.
- ✅ 0.1.x: The loop, submit guard, tailored resume and cover letter, questions, Gmail drafts, daily cap, Windows installer.
