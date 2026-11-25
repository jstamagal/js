import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from js import cli, generate_cli, generate_writer, logins


def encoded(fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (2, 3), "purple").save(out, fmt)
    return out.getvalue()


def mock_http(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


def run(*args):
    return cli.main(["generate", *args])


def test_env_saved_login_and_explicit_writer_routes(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    logins.save_login(logins.Login("paint", sdk_provider_id="openai", provider_base_url="http://paint.test",
                                   provider_api_key="private-media-key"))
    monkeypatch.setenv("JS_IMAGE_MODEL", "paint/team/model")
    assert run("--t2i", "-p", "test", "--enhance-url", "http://refiner@write.test/v1", "--dry-run") == 0
    raw = capsys.readouterr().out
    assert "private-media-key" not in raw
    data = json.loads(raw)
    assert data["fields"] == {"model": "team/model", "prompt": "test"}
    assert data["url"] == "http://paint.test/v1/images/generations"
    assert data["writer"]["url"] == "http://write.test/v1"
    assert data["writer"]["model"] == "refiner"
    assert data["outputs"] == ["out.png"]
    assert not Path("out.png").exists()


def test_image_count_refs_and_conversion(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("a.png").write_bytes(encoded())
    Path("b.png").write_bytes(encoded())
    image = base64.b64encode(encoded("JPEG")).decode()
    def handler(req):
        assert req.url.path == "/v1/images/edits"
        assert req.content.index(b'filename="a.png"') < req.content.index(b'filename="b.png"')
        assert req.content.count(b'name="image[]"') == 2
        return httpx.Response(200, json={"data": [{"b64_json": image}] * 2})
    mock_http(monkeypatch, handler)
    args = ("--i2i", "-u", "http://m@paint.test/v1", "--image", "a.png", "--ref", "b.png", "-n", "2", "-p", "go")
    assert run(*args) == 0
    for path in ["out-001.png", "out-002.png"]:
        with Image.open(path) as pic:
            assert (pic.format, pic.size) == ("PNG", (2, 3))
    monkeypatch.setattr(generate_cli, "execute", lambda *args: pytest.fail("duplicate submission"))
    assert run(*args) != 0


def test_nested_agent_precedence_and_verbatim_tts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from js import paths
    directory = paths.home() / "agents" / "media" / "narrator"
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text("model: fallback-writer\n")
    (directory / "01-prompt.md").write_text("Whisper the directions.")
    script = "  Hello!\n"
    def writer(**kw):
        assert kw["model_id"] == "per-run"
        assert kw["provider_base_url"] == "http://writer.test/v1"
        assert kw["tools"] is None
        assert "Whisper the directions." in str(kw["messages"][0])
        return SimpleNamespace(text="Speak quietly.", finish_reason="stop")
    monkeypatch.setattr(generate_writer.model_client, "stream_model", writer)
    def handler(req):
        assert json.loads(req.content) == {"model": "tts", "response_format": "mp3", "input": script,
                                          "instructions": "Speak quietly.", "voice": "coral"}
        return httpx.Response(200, content=b"speech")
    mock_http(monkeypatch, handler)
    assert run("--tts", "-u", "http://tts@media.test/v1", "-a", "media:narrator", "--voice", "coral",
               "--enhance-url", "http://per-run@writer.test/v1", "-p", script) == 0
    assert Path("out.mp3").read_bytes() == b"speech"


def test_video_submit_resume_and_cross_origin_download(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JS_API_KEY", "private")
    seen = []
    def handler(req):
        seen.append((req.method, req.url.path))
        if req.method == "POST":
            assert json.loads(req.content)["generate_audio"] is False
            return httpx.Response(200, json={"request_id": "job1"})
        if req.url.host == "render":
            assert req.headers["authorization"] == "Bearer private"
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://cdn/clip.mp4"}})
        assert "authorization" not in req.headers
        return httpx.Response(200, content=b"video")
    mock_http(monkeypatch, handler)
    route = ("-u", "http://video@render/v1")
    assert run(*route, "--t2v", "-p", "hello", "--no-audio", "--submit-only") == 0
    assert capsys.readouterr().out.strip() == "job1"
    assert run(*route, "--resume", "job1") == 0
    assert seen == [("POST", "/v1/videos/generations"), ("GET", "/v1/videos/job1"), ("GET", "/clip.mp4")]
    assert Path("out.mp4").read_bytes() == b"video"


def test_space_submission_upload_resume_and_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    source = tmp_path / "first.png"
    source.write_bytes(encoded())
    methods = []
    def handler(req):
        methods.append(req.method)
        if req.url.host == "cdn":
            assert "authorization" not in req.headers
            return httpx.Response(200, content=b"clip")
        assert req.headers["authorization"] == "Bearer hf_test"
        if req.url.host == "huggingface.co":
            return httpx.Response(200, json={"host": "https://owner-demo.hf.space"})
        if req.url.path.endswith("/upload"):
            return httpx.Response(200, json=["/tmp/gradio/upload"])
        if req.method == "POST":
            assert req.url.path == "/gradio_api/call/v2/generate"
            assert json.loads(req.content) == {"prompt": "snow", "steps": 12, "image_path": {
                "path": "/tmp/gradio/upload", "meta": {"_type": "gradio.FileData"}}}
            return httpx.Response(200, json={"event_id": "abc"})
        assert req.url.path.endswith("/generate/abc")
        return httpx.Response(200, text='event: complete\ndata: [{"video":{"url":"https://cdn/v"}},42,"info"]\n\n')
    mock_http(monkeypatch, handler)
    assert run("--i2v", "--space", "owner/demo", "--first-frame", str(source), "-p", "snow", "--param-json",
               "steps=12", "--submit-only") == 0
    methods.clear()
    assert run("--resume", "abc", "-m", "hf.space/owner/demo") == 0
    assert methods == ["GET"] * 3
    assert Path("out.mp4").read_bytes() == b"clip"


def test_music_lyrics_and_segment_transcript(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-key")
    def handler(req):
        if req.url.host == "api.elevenlabs.io":
            assert req.headers["xi-api-key"] == "eleven-key" and "authorization" not in req.headers
            body = json.loads(req.content)
            section, = body["composition_plan"]["sections"]
            assert section["lines"] == ["One!", "Two."]
            assert section["duration_ms"] == 12000
            assert "prompt" not in body
            return httpx.Response(200, content=b"audio")
        assert req.url.path == "/v1/audio/transcriptions"
        assert b'name="diarize"' in req.content and b'segment' in req.content
        return httpx.Response(200, json={"text": "One!", "segments": [{"speaker_id": "one"}]})
    mock_http(monkeypatch, handler)
    assert run("--music", "-m", "elevenlabs/music_v1", "-p", "folk", "--lyrics", "One!\nTwo.", "--duration", "12") == 0
    assert run("--stt", "-u", "http://voxtral@stt/v1", "--audio-file", "out.mp3", "--diarize", "-o", "out.json") == 0
    assert json.loads(Path("out.json").read_text())["segments"][0]["speaker_id"] == "one"



def test_native_gemini_edit_roundtrip(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("ref.png").write_bytes(encoded())
    def handler(req):
        assert req.url.path == "/v1beta/models/gemini-3.1-flash-image:generateContent"
        body = json.loads(req.content)
        image, prompt = body["contents"][0]["parts"]
        assert image["inlineData"]["mimeType"] == "image/png"
        assert prompt == {"text": "Make it snow"}
        assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [image]}}]})
    mock_http(monkeypatch, handler)
    assert run("--i2i", "--ref", "ref.png", "--aspect-ratio", "16:9", "--resolution", "2K",
               "-p", "Make it snow", "-u", "http://gemini-3.1-flash-image@proxy/v1beta") == 0
    assert Path("out.png").read_bytes() == encoded()


def test_failed_resume_preserves_output_and_never_posts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("out.mp4").write_bytes(b"original")
    def handler(req):
        assert req.method == "GET"
        return httpx.Response(200, json={"status": "expired"})
    mock_http(monkeypatch, handler)
    assert run("--resume", "old", "--force", "-u", "http://m@localhost/v1") == 1
    assert Path("out.mp4").read_bytes() == b"original"


def test_default_h3_writer_is_overridable(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    args = ("--t2i", "-u", "http://minimax-h3-image@localhost/v1", "-p", "owl", "--dry-run",
            "--enhance-url", "http://qwen@localhost/v1")
    assert run(*args) == 0
    assert json.loads(capsys.readouterr().out)["writer"]["agent"] == "h3-image"
    assert run(*args, "-a", "media-writer") == 0
    assert json.loads(capsys.readouterr().out)["writer"]["agent"] == "media-writer"


def test_official_h3_turbo_workflow_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run("--t2v", "--space", "MiniMaxAI/MiniMax-H3-Turbo-Lora", "-p", "snow", "--duration", "3",
               "--param-json", "steps=4", "--dry-run") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["api_path"].endswith("/call/v2/output_video")
    fields = plan["fields"]
    assert (fields["in_0"], fields["in_1"], fields["in_4"], fields["in_5"]) == ("snow", None, 3, 4)
