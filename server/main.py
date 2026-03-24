"""
SmartGuard — server/main.py
Phase 4: FastAPI Server + Threshold Engine

Exposes the classifier as a REST API and optionally proxies safe prompts
to a live LLM backend (Groq / OpenAI / Anthropic — configured via .env).

Endpoints
---------
POST /classify          — classify a prompt, return verdict + category + confidence
POST /chat              — classify first; if safe, forward to LLM and return reply
GET  /health            — liveness check
GET  /config            — read current threshold
PATCH /config           — update threshold at runtime (no restart needed)

Run locally:
    uvicorn server.main:app --reload --port 8000

Then test:
    curl -X POST http://localhost:8000/classify \
         -H "Content-Type: application/json" \
         -d '{"prompt": "How do I make a bomb?"}'
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from classifier.model import classify, ClassifierResult

load_dotenv()

app = FastAPI(
    title="SmartGuard LLM Firewall",
    description="Classifies prompts before they reach an LLM backend.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ThresholdConfig:
    def __init__(self, default: float = 0.5):
        self._value = default

    @property
    def value(self) -> float:
        return self._value

    @value.setter
    def value(self, v: float):
        if not 0.0 < v < 1.0:
            raise ValueError(f"Threshold must be between 0 and 1, got {v}")
        self._value = v


config = ThresholdConfig(
    default=float(os.getenv("SMARTGUARD_THRESHOLD", "0.5"))
)

class ClassifyRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The prompt to classify")
    threshold: Optional[float] = Field(
        None,
        ge=0.01,
        le=0.99,
        description="Per-request threshold override. Falls back to global config if omitted.",
    )


class ClassifyResponse(BaseModel):
    verdict: str          
    category: str         
    confidence: float     
    latency_ms: float     
    threshold_used: float 


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    threshold: Optional[float] = Field(None, ge=0.01, le=0.99)


class ChatResponse(BaseModel):
    verdict: str
    category: str
    confidence: float
    latency_ms: float
    threshold_used: float
    llm_reply: Optional[str] = None   
    blocked: bool


class ConfigResponse(BaseModel):
    threshold: float


class ConfigUpdateRequest(BaseModel):
    threshold: float = Field(..., ge=0.01, le=0.99)


@app.on_event("startup")
async def warm_up():
    """
    Run one dummy classification at startup so the first real request
    doesn't pay the 300 ms model-load penalty (seen in Phase 3 smoke test).
    """
    classify("warm up", threshold=0.5)
    print("SmartGuard: classifier pre-warmed ✓")


@app.get("/health")
def health():
    return {"status": "ok", "model": "sumitranjan/PromptShield"}


@app.get("/config", response_model=ConfigResponse)
def get_config():
    """Return the current global threshold."""
    return {"threshold": config.value}


@app.patch("/config", response_model=ConfigResponse)
def update_config(body: ConfigUpdateRequest):
    try:
        config.value = body.threshold
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"threshold": config.value}


@app.post("/classify", response_model=ClassifyResponse)
def classify_prompt(body: ClassifyRequest):
    effective_threshold = body.threshold if body.threshold is not None else config.value
    result: ClassifierResult = classify(body.prompt, threshold=effective_threshold)

    return ClassifyResponse(
        verdict=result["verdict"],
        category=result["category"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
        threshold_used=effective_threshold,
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    effective_threshold = body.threshold if body.threshold is not None else config.value
    result: ClassifierResult = classify(body.prompt, threshold=effective_threshold)

    base = ChatResponse(
        verdict=result["verdict"],
        category=result["category"],
        confidence=result["confidence"],
        latency_ms=result["latency_ms"],
        threshold_used=effective_threshold,
        llm_reply=None,
        blocked=result["verdict"] == "unsafe",
    )

    if result["verdict"] == "unsafe":
        return base

    llm_reply = await _call_llm(body.prompt)
    base.llm_reply = llm_reply
    return base


async def _call_llm(prompt: str) -> str:
    """
    Forward a safe prompt to the configured LLM provider.
    Returns the model's text reply, or a stub if no provider is set.
    """
    provider = os.getenv("LLM_PROVIDER", "stub").lower()

    if provider == "groq":
        return await _call_groq(prompt)
    elif provider == "openai":
        return await _call_openai(prompt)
    elif provider == "anthropic":
        return await _call_anthropic(prompt)
    else:
        # Stub — no LLM key needed, useful for local testing
        return f"[STUB LLM] You said: {prompt[:80]}... (set LLM_PROVIDER in .env to use a real model)"


async def _call_groq(prompt: str) -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not set in .env")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set in .env")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_anthropic(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set in .env")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]