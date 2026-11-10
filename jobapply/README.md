# JobApply

A Windows desktop app on browser-use that fills job applications in a loop and **never submits them**. For each job it
opens a new tab, writes a resume and cover letter for that job, fills the application, and stops before the final
Submit/Send. Then it moves on to the next job. You review each tab and submit it yourself.

## Install (Windows)

Download `JobApply-Setup-<version>.exe` from the repository's **Actions → jobapply-windows** run (artifact), or from a
`jobapply-v*` release. Run it. It installs for your user only, with no admin rights, and adds a desktop shortcut. You
need Chrome or Edge installed (Edge comes with Windows).

## First run

1. **Setup → Your files**
   - **Profile**: a `.md` file with everything about you: contact details, work authorization, experience,
     education, skills, projects, and standard answers. "New from template" creates a starter file and opens it.
   - **Job criteria**: a `.md` file with the titles, locations, remote preference, salary floor, keywords and
     companies to avoid. An optional `- Cover letter tone: ...` line sets the letter's tone.
   - **Resume template**: the built-in one, or your own HTML (see "Resume template" below).
2. **Setup → AI model**: a Gemini API key, an OpenAI API key, or **Sign in with ChatGPT** (see below).
3. **Setup → Jobs**: which boards to search, how many jobs per day (default 20), and any job URLs you want handled
   first.
4. **Run → Start**. A browser window opens with its own profile. The first time, log in to LinkedIn, Indeed and Gmail
   in that window. The logins are kept for later runs.

## What happens in the loop

- **Job order**: pasted URLs come first. After that it searches the selected boards in turn: LinkedIn (Easy Apply
  preferred), Indeed, Greenhouse, Lever, Ashby and Wellfound. It never opens the same job twice, even through a
  different link (the URL is normalized).
- **Criteria check**: a posting that clearly fails your criteria is marked `skipped` and its tab is closed.
- **Documents**: for each job, one model call returns JSON that fills the resume template. The browser prints it to
  PDF together with a cover letter, in `documents\<date>_<company>_<title>\`. A resume naming an employer, school or
  number that isn't in your profile is sent back to the model to fix.
- **Unknown questions** (salary, visa, custom questions): the field is left empty, the question goes to the
  **Questions** tab, and the loop continues. When you answer, the answer is saved under `## Saved answers` in your
  profile, so later applications use it.
- **Email applications**: when a posting asks you to email your CV, it opens a Gmail compose tab, writes the email,
  attaches the resume and cover letter PDFs, and leaves the draft unsent.
- **Daily cap**: filled, email-drafted and needs-input jobs count toward it. Skipped and failed jobs do not.
- **Pause / Stop**: Pause finishes the current job, then waits. Stop cancels the current job. The open tabs stay
  either way.

## The submit guard

A guard runs on its own connection to the browser and covers every tab and iframe before the page's own scripts run.
While the agent works, it blocks:

- clicks, taps and Enter/Space on any button whose label says *submit* or *send* (but not "send code" or "verify");
- Ctrl+Enter (Gmail's send shortcut);
- `form.submit()` and `requestSubmit()`.

A blocked click shows a note: "left for you". When a job is done, the guard unlocks that job's tabs so you can submit
there. Every other tab stays locked.

The guard is a safety net, not a sandbox. A site whose final button reads something else (for example just "Apply")
is not caught. The agent's instructions forbid pressing those buttons too.

## Files (in `%APPDATA%\JobApply`, see "Open data folder")

| File | What |
|---|---|
| `applications.csv` | One row per job: `date, time, company, title, url, board, status, tab_id, resume, cover_letter, unanswered_questions, notes`. Status is `filled`, `email_drafted`, `needs_input`, `skipped` or `failed`. Opens in Excel. |
| `questions.json` | Questions you have answered and ones still open. |
| `documents\` | Per job: the resume and cover letter as PDF and HTML, the letter as text, and the model's JSON. |
| `browser-profile\` | The JobApply browser's logins. |
| `settings.json` | Your settings, including API keys in plain text. It is readable only by your Windows account. |
| `jobapply.log` | Log for troubleshooting. |

## Resume template

The template is HTML with Mustache tags, filled from the JSON in `jobapply/documents.py` (`ResumeData`):
`{{name}} {{headline}} {{email}} {{phone}} {{location}} {{summary}}`, `{{#links}}{{label}} {{url}}{{/links}}`,
`{{#skills}}{{name}}: {{joined}}{{/skills}}`, `{{#experience}}{{title}} {{company}} {{location}} {{start}} {{end}}
{{#bullets}}{{.}}{{/bullets}}{{/experience}}`, `{{#projects}}...{{/projects}}`, `{{#education}}{{degree}} {{school}}
{{#details}}{{.}}{{/details}}{{/education}}`, `{{#certifications}}{{.}}{{/certifications}}`, `{{languages_joined}}`.

`{{#list.0}}...{{/list.0}}` shows a block only when the list isn't empty. "Copy built-in to edit" gives you a starting
point.

## Sign in with ChatGPT

This is OAuth 2.0 with PKCE and a localhost redirect (`http://localhost:1455/auth/callback` by default). Model calls
go to an OpenAI-compatible Responses endpoint, with your token refreshed automatically.

- **Client ID**: enter the one OpenAI issues when you register the app for Sign in with ChatGPT.
- **Endpoints, scope, API base URL and port**: these are settings, so they can follow OpenAI's current documentation
  without a new build.

## Develop

```bash
uv sync --all-extras --group jobapply
uv run python -m jobapply                  # the window (pywebview)
uv run pytest tests/ci/test_jobapply*.py   # unit + end-to-end on headless Chrome with scripted models
```

To build the installer, run `.github/workflows/jobapply-windows.yml`, which uses PyInstaller
(`packaging/windows/jobapply.spec`) and then Inno Setup (`packaging/windows/installer.iss`). Push a `jobapply-v0.1.0`
tag to attach the installer to a release.

## Limits

- **Job boards**: LinkedIn and Indeed limit automation and may show checks or ask you to log in again. Keep the daily
  cap modest.
- **Accounts**: it never types passwords or creates accounts. A site that needs a login is marked `needs_input` with a
  question for you.
- **Closing the app**: closing JobApply closes its browser. Review your tabs before you close it.
