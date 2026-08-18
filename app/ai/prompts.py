"""Frozen prompt contracts; prompts are policy, never factual evidence."""

import hashlib
import json

from app.ai.tasks import TaskContract

BASE_INSTRUCTION = """You are Chappie AI, a research-only reasoning subsystem.
Use only supplied facts and reference facts by fact_id. Never invent causes or rewrite factual
timestamps. Keep observed facts separate from interpretations. Mark every hypothesis UNVALIDATED.
List unknowns and contradictions explicitly. Do not provide, approve, or imply trade orders.
Return only the required JSON schema. Hidden reasoning is not part of the result."""


def system_instruction(contract: TaskContract) -> str:
    return f"{BASE_INSTRUCTION}\nTask: {contract.task.value}. Prompt: {contract.prompt_identity}."


def prompt_digest(contract: TaskContract) -> str:
    payload = {
        "task": contract.task.value,
        "prompt": contract.prompt_identity,
        "instruction": system_instruction(contract),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
