<p align="center"><img src="ui/logo.png" alt="Jobify" width="120"></p>

<h1 align="center">Jobify</h1>


A Windows desktop app on browser-use that fills job applications in a loop and **never submits them**. For each job it
opens a new tab, writes a resume and cover letter for that job, fills the application, and stops before the final
Submit/Send. Then it moves on to the next job. You review each tab and submit it yourself.

## Install (Windows)

Download `Jobify-Setup-<version>.exe` from the repository's **Actions → jobapply-windows** run (artifact), or from a
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
4. **Setup → Log in to sites**. This opens the Jobify browser normally. Sign in to Google (Gmail), LinkedIn and
   Indeed, then close that window. The logins are kept. Log in here, not during a run: Google refuses to sign in to a
   browser that is being remote-controlled ("This browser or app may not be secure").
5. **Run → Start**. The browser window opens and the loop begins.

## What happens in the loop

- **Job order**: pasted URLs come first. After that it searches the selected boards in turn: LinkedIn (Easy Apply
  preferred), Indeed, Greenhouse, Lever, Ashby and Wellfound. It never opens the same job twice, even through a
  different link (the URL is normalized).
- **Criteria check**: a posting that clearly fails your criteria is marked `skipped` and its tab is closed.
- **Documents**: for each job, one model call returns JSON that fills the resume template. The browser prints it to
  PDF together with a cover letter, in `documents\<date>_<company>_<title>\`. A resume naming an employer, school or
  number that isn't in your profile is sent back to the model to fix.
- **Unknown questions** (salary, visa, custom questions): the field is left empty, the question goes to the
  **Questions** tab, and the loop continues. Your answer is saved under `## Saved answers` in your profile, so later
  applications use it. Once every question of a job is answered (or closed with **Done** or **Skip**), Jobify goes
  back to that job's tab, locks Submit again, fills the missing fields and hands the tab back to you. The job's row
  is updated in place. **Fill again** in **History** does the same at any time.
- **Tracker:** in **History**, **Submitted ✓** marks a job as done. It's never applied to again, and its questions
  close.
- **Email applications**: when a posting asks you to email your CV, it opens a Gmail compose tab, writes the email,
  attaches the resume and cover letter PDFs, and leaves the draft unsent.
- **Daily cap**: filled, email-drafted and needs-input jobs count toward it. Skipped and failed jobs do not.
- **Pause / Stop**: Pause finishes the current job, then waits. Stop cancels the current job. The open tabs stay
  either way.

## When it needs you

- **Captcha, "verify you are human", login or a code:** a yellow banner appears with a sound, and the Jobify window
  and the job's tab come to the front. Do it in the browser and click **I did it, continue**; the agent carries on in
  the same job. Away for more than a minute? The job is parked in **Questions** (click **Done** when you have done it)
  and the loop moves on to the next job.
- **Take over:** stops the agent after its current step so you can use the browser. **Continue** finishes that job
  first, then the loop goes on.
- **Before searching** it checks that you are logged in and not blocked on each board and Gmail, and asks you to fix
  it; a board still blocked is skipped for that run.
- **Too many tabs waiting** (Setup: pause for review at N): the loop holds until you review some and click Continue.

## Reliability

- **Final check:** after each job, History shows "✓ All required fields filled" or "⚠ 2 required fields empty: …"
  plus any error messages on the form, with a screenshot of it.
- **Stuck detector:** the same step 5 times in a row parks the job instead of wasting steps.
- **Backup model** (Setup): used for a job when the main model stays busy.
- **Browser crash:** the browser is started again and the job retried once.
- **Fresh jobs first:** postings older than N days (Setup) are skipped; the newest go first.
- **History** shows today's summary and why unfinished jobs are waiting. **Setup** shows how complete your profile is
  and what forms will still have to ask.

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

## Files (in `%APPDATA%\Jobify`, see "Open data folder")

Jobify was called JobApply before: if `%APPDATA%\JobApply` exists, Jobify keeps using it, so nothing is lost.

| File | What |
|---|---|
| `applications.csv` | One row per job: `date, time, company, title, url, board, status, tab_id, resume, cover_letter, unanswered_questions, notes`. Status is `filled`, `email_drafted`, `needs_input`, `skipped` or `failed`. Opens in Excel. |
| `questions.json` | Questions you have answered and ones still open. |
| `documents\` | Per job: the resume and cover letter as PDF and HTML, the letter as text, and the model's JSON. |
| `browser-profile\` | The Jobify browser's logins. |
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

Your ChatGPT plan (Plus or Pro) pays for the AI, with no API key. Choose **Sign in with ChatGPT** in **Setup → AI
model**, click **Sign in**, and approve **Use your ChatGPT plan** in the browser that opens.

There's nothing to set up. Jobify uses OpenAI's
[ChatGPT plan usage for open-source and local apps](https://developers.openai.com/siwc/token-sharing-open-source),
through Pydantic AI's `OpenAIChatGPTProvider`:

- **Registration:** the first sign-in registers Jobify for your ChatGPT account automatically (`dynamic_agent_client`,
  with an ID unique to this installation). OpenAI returns a client ID, which later sign-ins reuse.
- **Login:** the browser comes back to `http://127.0.0.1:1455/auth/callback`. Close the Codex CLI first if it's
  running, because it uses the same port.
- **Tokens:** saved in `chatgpt_auth.json` and refreshed automatically. **Sign out** deletes them.
- **AI calls:** these go to the OpenAI Responses API and count against your plan's limits.

## Develop

```bash
uv sync --all-extras --group jobapply
uv run python -m jobapply                  # the window (pywebview)
uv run pytest tests/ci/test_jobapply*.py   # unit + end-to-end on headless Chrome with scripted models
```

To build the installer, run `.github/workflows/jobapply-windows.yml`, which uses PyInstaller
(`packaging/windows/jobapply.spec`) and then Inno Setup (`packaging/windows/installer.iss`).

## Updates

**Setup → Updates** shows the installed version. The app checks for a new one when it starts and shows an **Update**
button in the header. Installing it:

1. downloads the installer and checks its SHA-256 against the release;
2. closes Jobify and its browser;
3. installs the new version silently and opens it.

Your settings, profile, logins and history in the data folder are kept.

**To publish an update:** raise `__version__` in `jobapply/__init__.py` and merge to `main`. The `jobapply-windows`
workflow then builds the installer and publishes the GitHub release `jobapply-v<version>`, which every installed copy
offers. The first copy with the updater (0.2.0) has to be installed by hand once.

## Limits

- **Job boards**: LinkedIn and Indeed limit automation and may show checks or ask you to log in again. Keep the daily
  cap modest.
- **Accounts**: it never types passwords or creates accounts. A site that needs a login is marked `needs_input` with a
  question for you.
- **Closing the app**: closing Jobify closes its browser. Review your tabs before you close it.
