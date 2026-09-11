# 02 — Kernel tool: document that kernel Python is the uv tool env, not the project venv

**What to build:**
The kernel runs the harness's uv tool python (~/.local/share/uv/tools/js/bin/python, currently 3.13), not any project venv (lm_world_gen/.venv is 3.14). Packages installed into a project venv are invisible to kernel cells. The tool description (or a docs page it links) must state this and give the exact command to add a package to the kernel env: `uv pip install --python <kernel python> <pkg>`.

**Blocked by:** 01 — Kernel preflight

**Status:** ready-for-agent

- [ ] Tool description or linked docs states kernel python path differs from project venv
- [ ] Docs include the exact uv command to install a package into the kernel env
- [ ] A package installed only into a project venv is confirmed not importable in kernel, with the docs explaining why
