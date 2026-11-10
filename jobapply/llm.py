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
	import httpx
	from pydantic_ai.models.openai import OpenAIResponsesModel
	from pydantic_ai.providers.openai import OpenAIProvider

	from jobapply import chatgpt_auth

	cfg = settings.chatgpt
	if not chatgpt_auth.status()['signed_in']:
		raise ModelSetupError('Sign in with ChatGPT in Setup first.')
	client = httpx.AsyncClient(auth=chatgpt_auth.TokenAuth(cfg), timeout=httpx.Timeout(600, connect=10))
	# api_key is a placeholder the OpenAI client requires; TokenAuth replaces the header with the signed-in token
	provider = OpenAIProvider(base_url=cfg.api_base, api_key='chatgpt-signed-in', http_client=client)
	return OpenAIResponsesModel(cfg.model, provider=provider)


def writer_model(llm: Any) -> Any:
	"""The Pydantic AI model behind agent_llm(), for the resume and cover letter call."""
	from browser_use.llm import resolve_model

	return resolve_model(llm)[0]
