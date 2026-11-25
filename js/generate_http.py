"""Submit once, retrieve artifacts, and reconnect to Grok video jobs by ID."""
from __future__ import annotations

import base64
import json
import sys
import time
from urllib.parse import quote

from .generate_files import Asset, InlineImage, checked, download, pretty


def wire_value(value, client):
    if isinstance(value, InlineImage):
        _, blob, mime = value.upload(client)
        return {"inlineData": {"mimeType": mime, "data": base64.b64encode(blob).decode("ascii")}}
    if isinstance(value, Asset):
        return value.url(client)
    if isinstance(value, list):
        return [wire_value(v, client) for v in value]
    if isinstance(value, dict):
        return {k: wire_value(v, client) for k, v in value.items()}
    return value


def execute(client, req, a, route):
    headers = {}
    if route.api_key:
        headers[req.api_key_header] = (("Bearer " if req.api_key_header == "Authorization" else "") + route.api_key)
    headers.update(route.headers)
    if a.resume:
        return [wait_video(client, req.url, headers)]
    if req.method == "GET":
        response = client.get(req.url, headers=headers)
    elif req.files:
        files = [(k, src.upload(client)) for k, src in req.files]
        fields = {k: v if isinstance(v, str) else json.dumps(v) for k, v in req.fields.items()}
        response = client.post(req.url, headers=headers, params=req.query, data=fields, files=files)
    else:
        response = client.post(req.url, headers=headers, params=req.query, json=wire_value(req.fields, client))
    checked(response)
    if a.list_voices:
        return [pretty(response.json())]
    if a.op == "stt":
        data = response.json()
        if a.timestamps or a.diarize or (a.format or "").lower() == "json" or (a.output or "").lower().endswith(".json"):
            return [pretty(data)]
        return [(data["text"].rstrip("\n") + "\n").encode()]
    if a.kind == "image":
        results = []
        data = response.json()
        if req.url.endswith(":generateContent"):
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if "inlineData" in part and not part.get("thought"):
                        results.append(base64.b64decode(part["inlineData"]["data"], validate=True))
            if not results:
                raise ValueError(f"Gemini returned no image: {data.get('promptFeedback') or 'no image part'}")
            return results
        if req.url.endswith(":predict"):
            return [base64.b64decode(item["bytesBase64Encoded"], validate=True) for item in data["predictions"]]
        for item in data["data"]:
            if item.get("b64_json"):
                results.append(base64.b64decode(item["b64_json"], validate=True))
            else:
                results.append(download(client, item["url"], req.url, headers))
        return results
    if a.kind == "video":
        job = response.json().get("request_id")
        if not isinstance(job, str) or not job:
            raise ValueError("video endpoint did not return a request_id")
        if a.submit_only:
            print(job, flush=True)
            return []
        print(f"request_id={job}", file=sys.stderr, flush=True)
        return [wait_video(client, req.url.rsplit("/", 1)[0] + "/" + quote(job, safe=""), headers)]
    if response.headers.get("content-type", "").split(";")[0] in {"application/json", "text/html"}:
        raise ValueError(f"expected audio bytes, received {response.text[:500]}")
    return [response.content]


def wait_video(client, url, headers):
    while True:
        data = checked(client.get(url, headers=headers)).json()
        status = data.get("status")
        if status == "done":
            return download(client, data["video"]["url"], url, headers)
        if status not in {"pending", "queued", "in_progress", "processing", "running"}:
            raise ValueError(f"video job {status}: {data.get('error') or data.get('message') or status}")
        time.sleep(5)
