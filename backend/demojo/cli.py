"""Command line: diagnose | fixtures | smoke | serve | worker."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


def _ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def cmd_diagnose(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .costs import estimate_chat_usd, estimate_tts_usd, pricing_from_endpoints
    from .errors import AppError
    from .providers.openrouter import OpenRouterClient

    s = get_settings()
    failures = 0
    print("Demojo diagnostics (the API key is never printed)")
    print(f"  provider mode : {s.provider_mode}")
    print(f"  data dir      : {s.data_dir}")
    print(f"  vision model  : {s.vision_model}")
    print(f"  story model   : {s.story_model}")
    print(f"  tts model     : {s.tts_model} (voice {s.tts_voice}, format {s.tts_format})")
    print(f"  budget        : ${s.ai_project_budget_usd:.2f} per project (app-side guard)")
    for tool in ("ffmpeg", "ffprobe"):
        (_ok if shutil.which(tool) else _fail)(f"{tool} {'found' if shutil.which(tool) else 'missing'}")
        failures += 0 if shutil.which(tool) else 1
    client = OpenRouterClient(s)
    print("\nAPI key")
    if not s.openrouter_api_key:
        _fail("OPENROUTER_API_KEY is not set (add it to .env on the server)")
        failures += 1
    else:
        try:
            info = client.key_info()
            _ok("key accepted by OpenRouter")
            for k in ("usage", "limit", "limit_remaining", "is_free_tier"):
                if k in info:
                    print(f"         {k}: {info[k]}")
        except AppError as e:
            _fail(f"{e.code}: {e.message}")
            failures += 1

    print("\nModel capabilities (public catalog)")
    try:
        all_models = {m["id"]: m for m in _public_models(s, None)}
        speech_models = {m["id"]: m for m in _public_models(s, "speech")}
    except Exception as e:  # noqa: BLE001
        _fail(f"could not fetch the model catalog: {e}")
        return 1
    est_total = 0.0
    priced = True
    for role, model in (("vision", s.vision_model), ("story", s.story_model)):
        m = all_models.get(model)
        if m is None:
            _fail(f"{role} model {model} not found in the catalog")
            failures += 1
            continue
        inputs = (m.get("architecture") or {}).get("input_modalities") or []
        params = m.get("supported_parameters") or []
        if role == "vision" and "image" not in inputs:
            _fail(f"{model} does not accept image input")
            failures += 1
        if "structured_outputs" in params:
            _ok(f"{role}: {model} supports structured outputs" + (" and image input" if role == "vision" else ""))
        elif "response_format" in params:
            _warn(f"{role}: {model} supports response_format but not strict structured outputs; local validation + repair will apply")
        else:
            _fail(f"{role}: {model} lists neither structured_outputs nor response_format")
            failures += 1
        if m.get("expiration_date"):
            _warn(f"{model} is scheduled to expire on {m['expiration_date']}")
    tm = speech_models.get(s.tts_model)
    if tm is None:
        _fail(f"TTS model {s.tts_model} not found among speech models")
        failures += 1
    else:
        voices = tm.get("supported_voices") or []
        if s.tts_voice in voices:
            _ok(f"tts: {s.tts_model} lists voice '{s.tts_voice}'")
        else:
            _fail(f"voice '{s.tts_voice}' is not listed for {s.tts_model}. Listed: {', '.join(voices[:12])}…")
            failures += 1

    print("\nPricing (max across endpoints) and a typical-project estimate")
    try:
        vp = pricing_from_endpoints(s.vision_model, "chat", _public_endpoints(s, s.vision_model))
        sp = pricing_from_endpoints(s.story_model, "chat", _public_endpoints(s, s.story_model))
        tp = pricing_from_endpoints(s.tts_model, "speech", _public_endpoints(s, s.tts_model))
        for p in (vp, sp, tp):
            unit = "per 1M tokens" if p.unit == "token" else "per 1M characters"
            print(f"         {p.model}: prompt {_m(p.prompt)} / completion {_m(p.completion)} {unit} — {p.note}")
        parts = {
            "6 image analyses": sum(estimate_chat_usd(vp, prompt_text_chars=1500, images=1, max_completion_tokens=1400) or 0 for _ in range(6)),
            "recording analysis (24 frames)": estimate_chat_usd(vp, prompt_text_chars=3000, images=24, max_completion_tokens=3000),
            "storyboard plan": estimate_chat_usd(sp, prompt_text_chars=15000, images=0, max_completion_tokens=5000),
            "60 s narration (~900 chars)": estimate_tts_usd(tp, 900),
        }
        for k, v in parts.items():
            if v is None:
                priced = False
                _warn(f"{k}: cannot be priced reliably")
            else:
                est_total += v
                print(f"         {k}: up to ${v:.4f}")
        if priced:
            _ok(f"typical project upper-bound estimate ${est_total:.4f} (includes one repair per AI call)")
    except Exception as e:  # noqa: BLE001
        _warn(f"pricing lookup failed: {e}")

    if args.live:
        print("\nLive calls (--live; costs a fraction of a cent)")
        failures += _live_checks(s)
    print("\nResult:", "all checks passed" if failures == 0 else f"{failures} problem(s) found")
    return 0 if failures == 0 else 1


def _m(v: float | None) -> str:
    return "n/a" if v is None else f"${v * 1e6:.3f}"


def _public_models(s, modality: str | None) -> list[dict]:
    import httpx

    params = {"output_modalities": modality} if modality else None
    r = httpx.get(f"{s.openrouter_base_url}/models", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or []


def _public_endpoints(s, model: str) -> list[dict]:
    import httpx

    r = httpx.get(f"{s.openrouter_base_url}/models/{model}/endpoints", timeout=30)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("endpoints") or []


def _live_checks(s) -> int:
    from . import db
    from .ai_schemas import IMAGE_ANALYSIS_SCHEMA, ImageAnalysis
    from .costs import Ledger
    from .errors import AppError
    from .fixtures_gen import ui_dashboard
    from .projects import create_project, delete_project
    from .providers.openrouter import OpenRouterClient, data_url

    db.init_db()
    p = create_project("diagnostics")
    fails = 0
    try:
        client = OpenRouterClient(s)
        ledger = Ledger(p["id"], None)
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "d.jpg"
            im = ui_dashboard()
            im.thumbnail((1024, 1024))
            im.save(img, quality=85)
            try:
                res = client.chat_json(ledger, purpose="analyze_image", model=s.vision_model, messages=[
                    {"role": "user", "content": [{"type": "text", "text": "Analyse this screenshot. Return JSON per schema."},
                                                 {"type": "image_url", "image_url": {"url": data_url(img)}}]}],
                    schema_name="image_analysis", schema=IMAGE_ANALYSIS_SCHEMA, result_model=ImageAnalysis, max_tokens=800)
                _ok(f"vision + structured output: kind={res.kind}; summary={res.summary[:80]!r}")
            except AppError as e:
                _fail(f"vision call: {e.code}: {e.message}")
                fails += 1
            try:
                audio, gen = client.speech(ledger, text="Demojo diagnostic check.", model=s.tts_model, voice=s.tts_voice)
                out = Path(td) / "t.mp3"
                out.write_bytes(audio)
                from .ffmpeg import audio_duration

                _ok(f"speech: {len(audio)} bytes, {audio_duration(out):.2f}s decodable audio")
            except Exception as e:  # noqa: BLE001
                _fail(f"speech call: {e}")
                fails += 1
        from .costs import project_costs

        c = project_costs(p["id"])
        print(f"         reported cost ${c['reported_usd']:.6f}; unreported estimate ${c['estimated_unreported_usd']:.6f}")
    finally:
        delete_project(p["id"])
    return fails


# ---------------------------------------------------------------------------
# fixtures / smoke
# ---------------------------------------------------------------------------


def cmd_fixtures(args: argparse.Namespace) -> int:
    from .fixtures_gen import generate

    out = Path(args.out).resolve()
    m = generate(out)
    print(f"Wrote fixtures to {out}")
    for k, v in m.items():
        print(f"  {k}: {len(v['images'])} images, recording={v['recording']}, logo={v['logo']}")
    return 0


CASES = {
    "images": ("physical", True, False),
    "recording": ("software", False, True),
    "mixed": ("software", True, True),
}


def run_smoke_case(case: str, aspect: str, quality: str, fixtures: Path, out_dir: Path, *, style: str | None = None,
                   target: int | None = None) -> dict:
    """Create a project from fixtures, generate a (fixture) story, and render it through the real pipeline."""
    from . import jobs, pipeline
    from .projects import add_asset, create_project, get_project, list_exports, update_project

    kind, use_images, use_rec = CASES[case]
    manifest = json.loads((fixtures / "manifest.json").read_text())[kind]
    brief = dict(manifest["brief"])
    p = create_project(f"{brief['product_name']} {case} {aspect}")
    pid = p["id"]

    def upload(rel: str, role: str = "media") -> dict:
        tmp = Path(tempfile.mkdtemp()) / Path(rel).name
        shutil.copy(fixtures / rel, tmp)
        return add_asset(pid, tmp, Path(rel).name, role)  # type: ignore[arg-type]

    if use_images:
        for rel in manifest["images"]:
            upload(rel)
    if use_rec and manifest["recording"]:
        upload(manifest["recording"])
    logo = upload(manifest["logo"], "logo")
    details = {k: brief[k] for k in ("product_name", "description", "audience", "selling_points", "cta_text",
                                     "website_text", "product_type", "workflow_notes", "accent_color")}
    details.update(aspect_ratio=aspect, logo_asset_id=logo["id"], target_duration_s=target or 30,
                   style=style or ("guided_walkthrough" if case == "recording" else "clean_launch"))
    update_project(pid, details=details)

    def run(kind: str, params: dict, revision: int | None = None) -> dict:
        job, _ = jobs.enqueue(pid, kind, params, revision=revision)
        claimed = jobs.claim("smoke")
        assert claimed and claimed["id"] == job["id"], "unexpected job in queue"
        pipeline.execute(claimed)
        return jobs.get_job(job["id"])

    t0 = time.time()
    j = run("generate_story", {})
    if j["status"] != "story_ready":
        raise SystemExit(f"story generation failed: {j['error']}")
    rev = get_project(pid)["current_revision"]
    j = run("render", {"quality": quality, "accept_runtime": True}, revision=rev)
    if j["status"] != "completed":
        raise SystemExit(f"render failed: {j['error']}")
    exp = list_exports(pid)[0]
    from .projects import export_file

    name = f"{kind}_{case}_{aspect.replace(':', 'x')}_{quality}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f, ext in (("video", "mp4"), ("storyboard", "storyboard.json"), ("captions", "srt"), ("script", "script.txt")):
        src, _, _ = export_file(pid, exp["id"], f)
        shutil.copy(src, out_dir / f"{name}.{ext}")
    return {"case": case, "aspect": aspect, "quality": quality, "project_id": pid, "seconds": round(time.time() - t0, 1),
            "duration_s": exp["duration_s"], "size_bytes": exp["size_bytes"], "file": str(out_dir / f"{name}.mp4")}


def cmd_smoke(args: argparse.Namespace) -> int:
    data = Path(args.data or tempfile.mkdtemp(prefix="demojo-smoke-")).resolve()
    os.environ["DATA_DIR"] = str(data)
    os.environ["DEMOJO_PROVIDER_MODE"] = "fixture"
    from .config import reset_settings_cache

    reset_settings_cache()
    from . import db

    db.init_db()
    fixtures = Path(args.fixtures).resolve()
    if not (fixtures / "manifest.json").exists():
        from .fixtures_gen import generate

        generate(fixtures)
    cases = list(CASES) if args.case == "all" else [args.case]
    aspects = ["16:9", "9:16"] if args.aspect == "both" else [args.aspect]
    results = []
    for c in cases:
        for a in aspects:
            r = run_smoke_case(c, a, args.quality, fixtures, Path(args.out).resolve())
            print(json.dumps(r))
            results.append(r)
    print(f"Smoke test OK: {len(results)} render(s). FIXTURE MODE — template storyboards and espeak-ng narration, not AI output.")
    return 0


# ---------------------------------------------------------------------------
# serve / worker
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("demojo.api:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from .worker import main as worker_main

    worker_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="demojo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose", help="validate key, models, voices, and pricing (never prints the key)")
    d.add_argument("--live", action="store_true", help="also make two tiny paid calls (vision + speech)")
    d.set_defaults(fn=cmd_diagnose)
    f = sub.add_parser("fixtures", help="generate deterministic synthetic fixture assets")
    f.add_argument("--out", default=str(ROOT / "fixtures"))
    f.set_defaults(fn=cmd_fixtures)
    s = sub.add_parser("smoke", help="offline end-to-end render with fixture providers")
    s.add_argument("--case", choices=["images", "recording", "mixed", "all"], default="all")
    s.add_argument("--aspect", choices=["16:9", "9:16", "both"], default="both")
    s.add_argument("--quality", choices=["draft", "final"], default="draft")
    s.add_argument("--fixtures", default=str(ROOT / "fixtures"))
    s.add_argument("--out", default=str(ROOT / "samples"))
    s.add_argument("--data", default=None, help="data dir for the smoke run (default: a temp dir)")
    s.set_defaults(fn=cmd_smoke)
    sv = sub.add_parser("serve", help="run the API + UI server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    w = sub.add_parser("worker", help="run the job worker")
    w.set_defaults(fn=cmd_worker)
    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
