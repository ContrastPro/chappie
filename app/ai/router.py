"""Model-profile routing independent of agents and concrete providers."""

from app.ai.errors import AIRoutingError
from app.ai.provider import ModelProvider
from app.ai.tasks import ModelProfile


class ModelRouter:
    def __init__(self, routes: dict[ModelProfile, ModelProvider]) -> None:
        self._routes = dict(routes)

    def provider_for(self, profile: ModelProfile) -> ModelProvider:
        try:
            provider = self._routes[profile]
        except KeyError as error:
            raise AIRoutingError(f"no provider configured for profile {profile.value}") from error
        return provider

    def close(self) -> None:
        seen: set[int] = set()
        for provider in self._routes.values():
            if id(provider) not in seen:
                provider.close()
                seen.add(id(provider))
