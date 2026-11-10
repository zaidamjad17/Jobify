"""The model the agent and the resume writer use: Gemini or OpenAI with an API key, or ChatGPT through Sign in with
ChatGPT."""

from __future__ import annotations

from typing import Any

from jobapply.settings import Settings


class ModelSetupError(ValueError):
	pass


def agent_llm(settings: Settings) -> Any:
	"""What browser_use.Agent(llm=...) takes."""
	from browser_use import ChatGoogle, ChatOpenAI

	if settings.provider == 'gemini':
		if not settings.gemini_api_key:
			raise ModelSetupError('Add your Gemini API key in Setup (aistudio.google.com/apikey).')
		return ChatGoogle(settings.gemini_model, api_key=settings.gemini_api_key)
	if settings.provider == 'openai':
		if not settings.openai_api_key:
			raise ModelSetupError('Add your OpenAI API key in Setup (platform.openai.com/api-keys).')
		return ChatOpenAI(settings.openai_model, api_key=settings.openai_api_key)
	return chatgpt_model(settings)


def chatgpt_model(settings: Settings) -> Any:
	from jobapply import chatgpt_auth

	try:
		return chatgpt_auth.model(settings.chatgpt.model)
	except chatgpt_auth.AuthError as e:
		raise ModelSetupError(str(e)) from None


def writer_model(llm: Any) -> Any:
	"""The Pydantic AI model behind agent_llm(), for the resume and cover letter call."""
	from browser_use.llm import resolve_model

	return resolve_model(llm)[0]
