"""What the agent is told: one search task per board, one apply task per job, and the rules both follow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RULES = """You fill job applications for the user in their own browser; they review every one and submit it themselves.

NEVER submit anything. Never press a final button such as Submit, Submit application, Send, Send application, Apply
(when it sends the application), Finish or Confirm, never press Ctrl+Enter in an email, and never call form.submit().
A guard blocks those buttons; if a click does nothing and a note says "left for you", that is the guard: stop there.
Buttons that only move to the next step (Next, Continue, Review, Save, Easy Apply, Apply now that opens a form) are fine.

Facts come only from the candidate profile below (it is all there: never search the disk for other resumes or
profiles; the only files to use are the ones prepare_documents returns). Never invent an answer. For a required question the profile does not
answer (salary, visa, availability, "why do you want to work here" when the profile gives no basis, a custom
question), call ask_user(question, field) once, leave that field empty, and keep filling the rest.
Salary expectations, notice period, start date, relocation and work authorization: fill them only with what the
profile states; never estimate them from the job's salary range or anything else. If the profile has no answer, ask_user.
When you paste multi-line text (a cover letter), keep its line breaks: set the field's value in page code with the text
as is, then dispatch bubbling 'input' and 'change' events. Optional
demographic questions: use the profile's answer, else "Prefer not to say" / "Decline to answer" when offered.

Documents: call prepare_documents(company, title, job_description) once per job with the full job description text.
It returns the paths of a resume PDF and a cover letter PDF made for this job, and the letter text. Upload the resume
PDF to every resume / CV field (upload_file(selector, path) on the input[type=file]) and the cover letter PDF to a cover
letter upload; paste the letter text into a cover letter text box. Verify each upload shows the file name.

Accounts: the browser keeps the user's logins. If a site asks you to sign in or create an account and you cannot
continue without it, do not type passwords: call ask_user('Please log in to <site> in the JobApply browser', 'login') and
finish with status needs_input. Never solve a site's account verification by email codes yourself.

Stay in the tabs of this job: do not switch to, close or change other tabs (the user is reviewing them).

<candidate_profile>
{profile}
</candidate_profile>

<job_criteria>
{criteria}
</job_criteria>"""

APPLY_TASK = """Fill the job application at {url} (already open in the current tab) up to, but not including, the final
submit.

1. Read the job posting. If it clearly fails the job criteria (title, location/remote, seniority, salary, must-avoid
   keywords), finish with status "skipped" and the reason in notes. Do not skip on a guess.
2. Call prepare_documents with the company, the job title and the full job description.
3. Apply:
   - An "Easy Apply" / "Apply" flow on the board, or an external application page (it may open in a new tab: work
     there), fill every field from the profile, upload the documents, step through the pages, and stop on the last
     page with the final Submit button still unpressed. Status "filled".
   - If the posting says to apply by email (e.g. "send your CV to jobs@company.com"), open a new tab with
     https://mail.google.com/mail/?view=cm&fs=1&to=<address>&su=<email_subject> , wait for the compose window, write a
     short email body (the cover letter text works), attach the resume PDF and cover letter PDF with
     upload_file('input[type=file]', path) and verify both attachments show, and leave the draft open without sending.
     Status "email_drafted". If Gmail asks you to sign in: ask_user('Please log in to Gmail in the JobApply browser',
     'login') and status "needs_input".
4. Scroll through the final page and check that the required fields are filled. Status "needs_input" when any
   required field is still empty because of a question you sent to ask_user, or a login is needed.
5. If the posting is closed or the page cannot be used, status "failed" with the reason.
Finish with the JobOutcome: company, title, the posting URL, the board (linkedin, indeed, greenhouse, lever, ashby,
wellfound, workday, company site, email), status, unanswered questions, and short notes for the user."""

SEARCH_TASK = """Find up to {count} job postings on {board} that match the job criteria and are not yet in the user's list.

Start at {start_url} (already open in the current tab) and search with the criteria's titles and locations, newest
first when the site offers it. {board_hint}
This is search round {round}: earlier rounds already went through the results before page {round}, so go to results
page {round} (use the site's page links or its start/offset parameter) and collect from there; continue to the next
pages while you have fewer than {count}.
For each candidate, call already_seen(url) and drop the ones it reports. Keep postings that are open and match the
criteria reasonably: a similar title and a compatible location are enough; do not drop one over a detail the posting
does not state (salary, exact years). Do not apply to anything and do not open application forms.
If the site blocks you (a captcha you cannot pass, a login wall, "unusual traffic"), stop and say so in blocked.
Finish with the list of jobs (url of the posting itself, company, title, board "{board_id}"). An empty list is fine
when nothing new matches."""

BOARD_HINTS = {
	'linkedin': 'Prefer jobs with "Easy Apply" (the Easy Apply filter). Use the job URL https://www.linkedin.com/jobs/view/<id>/.',
	'indeed': 'Use the job URL https://www.indeed.com/viewjob?jk=<id> (or the same path on the country site).',
	'greenhouse': 'Search Google for the titles with site:boards.greenhouse.io or site:job-boards.greenhouse.io; use posting URLs.',
	'lever': 'Search Google for the titles with site:jobs.lever.co; use posting URLs.',
	'ashby': 'Search Google for the titles with site:jobs.ashbyhq.com; use posting URLs.',
	'wellfound': 'Use the job URL https://wellfound.com/jobs/<id>.',
}


class JobOutcome(BaseModel):
	status: Literal['filled', 'email_drafted', 'needs_input', 'skipped', 'failed']
	company: str = ''
	title: str = ''
	url: str = ''
	board: str = ''
	unanswered_questions: list[str] = Field(default_factory=list)
	notes: str = Field('', description='one or two sentences for the user: what is left, why it was skipped or failed')


class FoundJob(BaseModel):
	url: str
	company: str = ''
	title: str = ''
	board: str = ''


class FoundJobs(BaseModel):
	jobs: list[FoundJob] = Field(default_factory=list)
	blocked: str = Field('', description='empty, or what stopped the search: captcha, login wall, unusual traffic')
