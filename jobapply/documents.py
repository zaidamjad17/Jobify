"""A resume and cover letter per job: the model returns JSON (TailoredDocs) from profile.md and the job description, the
JSON fills the resume template, and the browser prints both to PDF."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jobapply.paths import resource, sub
from jobapply.render import render


class Link(BaseModel):
	label: str = Field(description='e.g. LinkedIn, GitHub, Portfolio')
	url: str


class SkillGroup(BaseModel):
	name: str = Field('', description='group name, e.g. Languages, Frameworks; empty for a single flat list')
	items: list[str]


class Experience(BaseModel):
	title: str
	company: str
	location: str = ''
	start: str = Field('', description='as in the profile, e.g. Jan 2022')
	end: str = Field('', description='e.g. Present')
	bullets: list[str] = Field(default_factory=list, description='3-5 achievements, the most relevant to this job first')


class Education(BaseModel):
	degree: str
	school: str
	location: str = ''
	start: str = ''
	end: str = ''
	details: list[str] = Field(default_factory=list)


class Project(BaseModel):
	name: str
	link: str = ''
	bullets: list[str] = Field(default_factory=list)


class ResumeData(BaseModel):
	name: str
	headline: str = Field('', description='a title line matching this job, true to the profile')
	email: str = ''
	phone: str = ''
	location: str = ''
	links: list[Link] = Field(default_factory=list)
	summary: str = Field('', description='2-3 sentences aimed at this job')
	skills: list[SkillGroup] = Field(default_factory=list, description='the skills this job asks for first')
	experience: list[Experience] = Field(default_factory=list)
	projects: list[Project] = Field(default_factory=list)
	education: list[Education] = Field(default_factory=list)
	certifications: list[str] = Field(default_factory=list)
	languages: list[str] = Field(default_factory=list)


class JobSummary(BaseModel):
	"""What the job is, for the user's history (from the posting only)."""

	about: str = Field('', description='2-3 plain sentences: the role, the team or product, and the main work')
	responsibilities: list[str] = Field(default_factory=list, description='up to 6 main duties, short')
	requirements: list[str] = Field(default_factory=list, description='up to 6 key requirements, short')
	location: str = Field('', description='city / country / remote / hybrid, as the posting says')
	salary: str = Field('', description='the pay range when the posting states one, else empty')
	employment_type: str = Field('', description='full-time, part-time, contract, internship... when stated')
	why_match: list[str] = Field(default_factory=list, description='2-4 reasons from the profile why the candidate fits this job')


class TailoredDocs(BaseModel):
	resume: ResumeData
	cover_letter: str = Field(description='the letter body: greeting to sign-off, plain text, paragraphs split by blank lines')
	email_subject: str = Field(description='subject line for applying by email, e.g. "Application: <title> - <name>"')
	job_summary: JobSummary | None = Field(None, description='what the job is, from the posting')


INSTRUCTIONS = """You tailor a resume and write a cover letter for one job application.
Use ONLY facts from the candidate profile: never invent employers, schools, titles, dates, degrees, certifications,
numbers or skills. You may reorder, select, shorten and reword them so the most relevant come first, and mirror the
job's wording where the profile supports it. Leave a field empty rather than guessing.
The cover letter: {tone}. Address the company by name, name the role, give 2-3 concrete reasons from the profile, end
with a short sign-off with the candidate's name. No placeholders like [Company] in the output.
The job summary describes the posting itself (only what it states) so the candidate can recall the job later."""


class FactError(ValueError):
	pass


def check_facts(resume: ResumeData, profile: str) -> None:
	"""Employers, schools and numbers in the resume must appear in the profile."""
	text = _plain(profile)
	missing = [
		*(f'employer "{e.company}"' for e in resume.experience if e.company and _plain(e.company) not in text),
		*(f'school "{e.school}"' for e in resume.education if e.school and _plain(e.school) not in text),
	]
	profile_numbers = set(re.findall(r'\d+(?:[.,]\d+)?', profile))
	for e in resume.experience:
		for bullet in e.bullets:
			for n in re.findall(r'\d+(?:[.,]\d+)?', bullet):
				if n not in profile_numbers:
					missing.append(f'the number {n} in "{bullet[:60]}"')
	if missing:
		raise FactError('Not in the profile: ' + '; '.join(missing[:8]) + '. Use only facts from the profile.')


def _plain(text: str) -> str:
	return ' '.join(re.sub(r'[^\w]+', ' ', text.lower()).split())


def template_data(resume: ResumeData) -> dict[str, Any]:
	data = resume.model_dump()
	for group in data['skills']:
		group['joined'] = ', '.join(group['items'])
	data['languages_joined'] = ', '.join(data['languages'])
	return data


def default_template() -> str:
	return resource('templates/resume.html').read_text(encoding='utf-8')


def render_resume(resume: ResumeData, template: str | None = None) -> str:
	return render(template or default_template(), template_data(resume))


def render_letter(docs: TailoredDocs, company: str) -> str:
	r = docs.resume
	paragraphs = ''.join(
		f'<p>{html.escape(p).replace(chr(10), "<br>")}</p>' for p in re.split(r'\n\s*\n', docs.cover_letter.strip())
	)
	contact = ' | '.join(html.escape(x) for x in (r.email, r.phone, r.location) if x)
	return f"""<!doctype html><html><head><meta charset="utf-8"><title>Cover letter - {html.escape(company)}</title>
<style>@page {{ size: A4; margin: 22mm 22mm; }} body {{ font-family: "Segoe UI", Calibri, Arial, sans-serif; font-size: 11pt;
line-height: 1.5; color: #1d2329; }} h1 {{ font-size: 16pt; margin: 0; color: #12304d; }} .contact {{ color: #555;
font-size: 9.5pt; margin: 2px 0 20px; }} .date {{ margin-bottom: 16px; color: #333; }} p {{ margin: 0 0 11px; }}</style>
</head><body><h1>{html.escape(r.name)}</h1><div class="contact">{contact}</div>
<div class="date">{datetime.now().strftime('%B %d, %Y').replace(' 0', ' ')}</div>{paragraphs}</body></html>"""


def slug(text: str, limit: int = 40) -> str:
	return re.sub(r'[^A-Za-z0-9]+', '_', text).strip('_')[:limit] or 'job'


def job_folder(company: str, title: str) -> Path:
	folder = sub('documents') / f'{datetime.now():%Y-%m-%d}_{slug(company)}_{slug(title)}'
	n, base = 1, folder
	while folder.exists():
		n += 1
		folder = base.with_name(f'{base.name}_{n}')
	folder.mkdir(parents=True)
	return folder


async def tailor(model: Any, profile: str, job_description: str, company: str, title: str, tone: str) -> TailoredDocs:
	"""One model call; a resume with facts not in the profile is sent back to the model to fix (up to 2 times)."""
	from pydantic_ai import Agent, ModelRetry

	agent = Agent(model, output_type=TailoredDocs, instructions=INSTRUCTIONS.format(tone=tone), retries=3)

	@agent.output_validator
	def _facts(docs: TailoredDocs) -> TailoredDocs:
		try:
			check_facts(docs.resume, profile)
		except FactError as e:
			raise ModelRetry(str(e)) from None
		return docs

	prompt = (
		f'<candidate_profile>\n{profile}\n</candidate_profile>\n\n<job company="{company}" title="{title}">\n'
		f'{job_description[:20_000]}\n</job>'
	)
	result = await agent.run(prompt)
	return result.output


def write_documents(docs: TailoredDocs, company: str, title: str, template: str | None, print_pdf: Any) -> dict[str, str]:
	"""Write resume and cover letter (HTML, PDF, and the letter as text) into a new folder; print_pdf(html, pdf)."""
	folder = job_folder(company, title)
	name = slug(docs.resume.name, 30)
	resume_html = folder / 'resume.html'
	resume_html.write_text(render_resume(docs.resume, template), encoding='utf-8')
	letter_html = folder / 'cover_letter.html'
	letter_html.write_text(render_letter(docs, company), encoding='utf-8')
	(folder / 'cover_letter.txt').write_text(docs.cover_letter.strip() + '\n', encoding='utf-8')
	(folder / 'resume.json').write_text(docs.model_dump_json(indent=2), encoding='utf-8')
	resume_pdf = print_pdf(resume_html, folder / f'{name}_Resume.pdf')
	letter_pdf = print_pdf(letter_html, folder / f'{name}_Cover_Letter.pdf')
	return {
		'folder': str(folder),
		'resume_pdf': str(resume_pdf),
		'cover_letter_pdf': str(letter_pdf),
		'cover_letter_text': docs.cover_letter.strip(),
		'email_subject': docs.email_subject,
	}
