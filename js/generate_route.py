"""Resolve media and writer routes from their respective CLI and setting values."""
from __future__ import annotations

import os
from dataclasses import replace
from urllib.parse import urlsplit

from . import endpoint_uri, logins, routing, setting_values


def resolve(*, url, explicit_model, default_model, values, missing, media=False):
    model = explicit_model or default_model
    extra_headers = setting_values.parse_header_lines(values.get("EXTRA_HEADERS"))
    if url:
        endpoint = endpoint_uri.split(url)
        if explicit_model and endpoint.model and explicit_model != endpoint.model:
            raise ValueError("the explicit model and the model embedded in its endpoint URL disagree")
        model = explicit_model or endpoint.model or default_model
        if not model:
            raise ValueError(missing)
        key = endpoint.api_key or os.getenv("JS_API_KEY")
        if media and urlsplit(endpoint.base_url).hostname == "api.elevenlabs.io":
            key = key or os.getenv("ELEVENLABS_API_KEY")
        return routing.ModelRoute(model, endpoint.provider_id, endpoint.base_url,
                                  key or values.get("ENDPOINT_DEFAULT_KEY"), headers=extra_headers)
    if not model:
        raise ValueError(missing)
    if media and model.startswith("elevenlabs/") and not logins.get_login("elevenlabs"):
        return routing.ModelRoute(model.split("/", 1)[1], "elevenlabs", "https://api.elevenlabs.io/v1",
                                  os.getenv("ELEVENLABS_API_KEY"), headers=extra_headers)
    route = routing.resolve_model_route(model, configured_provider_id=values.get("PROVIDER"),
                                        configured_base_url=values.get("BASE_URL"),
                                        configured_api_key=values.get("API_KEY"),
                                        explicit_model=True, prefix_overrides_provider=True)
    if route.base_url and endpoint_uri.is_endpoint_form(route.base_url):
        endpoint = endpoint_uri.split(route.base_url)
        route = replace(route, base_url=endpoint.base_url, api_key=endpoint.api_key or route.api_key,
                        provider_id=endpoint.provider_id)
    return replace(route, headers={**route.headers, **extra_headers})
