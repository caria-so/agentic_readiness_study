#!/usr/bin/env python3
"""
Run one batch: every stage, in order, into one timestamped folder.

  python run_batch.py --industry bathroom --lang ita                 # new batch, default stages
  python run_batch.py --industry bathroom --lang deu                 # same industry, German leads
  python run_batch.py --industry bathroom --lang deu --batch 20261101_0930      # resume that batch
  python run_batch.py --industry bathroom --lang ita --batch 20261003_merged --stages extract,headline
  python run_batch.py --industry bathroom --lang ita --dry-run       # print the commands only

Input:   leads/{industry}/{lang}.csv         (a column named URL / url / website, plus a name column)
Output:  results/{industry}/{lang}/{YYYYMMDD_HHMM}/   see utils/batch.py for the layout

Stages (in order). A default run does the first six; smoke and feed are asked for with --stages.
  discover  a_discovery_pipeline.py   robots, sitemap, homepage, JSON-LD            (network)
  sample    b_product_sample.py       product pages, saved to disk                  (network)
  extract   c_spec_extract.py         L1-L3 rules, offline
  llm       d_llm_prose.py            L4, local model through Ollama
  check     e_check.py                precision sample to mark by hand + repeated evidence
  headline  f_headline.py             headline table
  smoke     smoke_specs.py            label survey of the saved pages (diagnostic)
  feed      g_feed_check.py           public Shopify / WooCommerce feeds            (network)

Resuming: name the batch with --batch. Discovery skips the sites it already did, the sampler reuses
audits made in this batch, the LLM pass skips pages already done. Nothing is merged: one batch is
one folder. Hand-marked files in review/ are never overwritten (a second `check` makes a new sample,
independent of the earlier ones; on a resume it is skipped unless you name it in --stages).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from utils.batch import RESULTS, Batch, leads_file, lang_info, new_batch

HERE = Path(__file__).resolve().parent
ALL_STAGES = ["discover", "sample", "extract", "llm", "check", "headline", "smoke", "feed"]
DEFAULT_STAGES = ["discover", "sample", "extract", "llm", "check", "headline"]
NETWORK = {"discover", "sample", "feed"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def stage_command(stage: str, batch: Batch, args, resuming: bool, created: str) -> list[str]:
    py = [sys.executable, "-u"]
    b = ["--batch", str(batch.root)]
    if stage == "discover":
        cmd = py + [str(HERE / "a_discovery_pipeline.py"), "--urls", str(batch.root / "leads.csv"),
                    "--out", str(batch.root), "--lang", args.lang]
        return cmd + (["--resume"] if resuming else []) + shlex.split(args.discover_args)
    if stage == "sample":
        cmd = py + [str(HERE / "b_product_sample.py"), "--out", str(batch.root), "--lang", args.lang,
                    "--n", str(args.pages)]
        return cmd + (["--resume-since", created] if resuming else []) + shlex.split(args.sample_args)
    if stage == "extract":
        return py + [str(HERE / "c_spec_extract.py")] + b
    if stage == "llm":
        return py + [str(HERE / "d_llm_prose.py")] + b + ["--model", args.model]
    if stage == "check":
        return py + [str(HERE / "e_check.py")] + b + ["--n", str(args.sample_size)]
    if stage == "headline":
        return py + [str(HERE / "f_headline.py")] + b
    if stage == "smoke":
        return py + [str(HERE / "smoke_specs.py")] + b
    if stage == "feed":
        return py + [str(HERE / "g_feed_check.py")] + b
    raise ValueError(stage)


def run_logged(cmd: list[str], log_path: Path) -> int:
    """Run a stage, showing its output and keeping it in logs/<stage>.log (appended, one header per run)."""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')}  {shlex.join(cmd)}\n")
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        return proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--industry", required=True, help="e.g. bathroom (folder under leads/)")
    ap.add_argument("--lang", required=True, help="e.g. ita, deu (file name under leads/<industry>/)")
    ap.add_argument("--batch", help="resume this batch (its id, e.g. 20261101_0930); default: start a new one")
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                    help=f"comma-separated, any of: {', '.join(ALL_STAGES)}")
    ap.add_argument("--model", default="qwen2.5:32b", help="Ollama model for the llm stage")
    ap.add_argument("--pages", type=int, default=25, help="product pages per site")
    ap.add_argument("--sample-size", type=int, default=50, help="values in the precision sample")
    ap.add_argument("--discover-args", default="", help='extra flags for a_discovery_pipeline.py, '
                    'e.g. "--persistent-chrome --concurrency 1"')
    ap.add_argument("--sample-args", default="", help="extra flags for b_product_sample.py")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    args = ap.parse_args()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in ALL_STAGES]
    if bad:
        sys.exit(f"unknown stage(s): {', '.join(bad)}; known: {', '.join(ALL_STAGES)}")
    stages.sort(key=ALL_STAGES.index)
    explicit_stages = args.stages != ",".join(DEFAULT_STAGES)
    lang_info(args.lang)                                    # fails early on an unknown language code

    leads = leads_file(args.industry, args.lang)
    if args.batch:
        batch = Batch(RESULTS / args.industry / args.lang / args.batch)
        if not batch.root.is_dir():
            sys.exit(f"no batch {batch.root}")
    elif any(s in stages for s in ("discover", "sample")):
        if not leads.is_file():
            sys.exit(f"no leads file {leads}")
        batch = None
    else:
        sys.exit("stages after discovery need an existing batch: add --batch <id>")
    resuming = batch is not None

    if args.dry_run and batch is None:
        batch = Batch(RESULTS / args.industry / args.lang / datetime.now().strftime("%Y%m%d_%H%M"))
        print(f"(dry run: would create {batch.root})")
    elif batch is None:
        batch = new_batch(args.industry, args.lang)
    if not args.dry_run:
        batch.ensure()

    manifest = json.loads(batch.manifest.read_text(encoding="utf-8")) if batch.manifest.exists() else {}
    if not manifest:
        manifest = {"industry": args.industry, "lang": args.lang, "created": datetime.now().isoformat(timespec="seconds"),
                    "leads": str(leads.relative_to(HERE)) if leads.is_absolute() else str(leads),
                    "leads_sha256": sha256(leads) if leads.is_file() else None, "runs": []}
    if "discover" in stages and not (batch.root / "leads.csv").exists() and not args.dry_run:
        shutil.copyfile(leads, batch.root / "leads.csv")    # the list this batch was run on, frozen

    print(f"batch: {batch.root}   stages: {', '.join(stages)}")
    if args.lang != "ita":
        print("NOTE: discovery and the sampler follow --lang; the extraction rules (label synonyms, junk lists, "
              "size words, the LLM prompt) are Italian. Coverage for other languages will be understated "
              "until those are added. Check the per-site tables before reading anything into a number.")

    for stage in stages:
        if stage == "check" and not explicit_stages and list(batch.review.glob("l4_precision_sample*.csv")):
            print("\n── check: this batch already has a precision sample; skipped "
                  "(name `check` in --stages for another, independent one)")
            continue
        cmd = stage_command(stage, batch, args, resuming, manifest["created"])
        print(f"\n── {stage}{'  (network)' if stage in NETWORK else ''}\n   {shlex.join(cmd)}")
        if args.dry_run:
            continue
        started = datetime.now().isoformat(timespec="seconds")
        code = run_logged(cmd, batch.logs / f"{stage}.log")
        manifest["runs"].append({"stage": stage, "started": started,
                                 "finished": datetime.now().isoformat(timespec="seconds"), "exit": code})
        batch.manifest.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        if code:
            print(f"\n{stage} failed (exit {code}); log: {batch.logs / f'{stage}.log'}. "
                  f"Fix it and resume with --batch {batch.root.name} --stages {','.join(s for s in stages if ALL_STAGES.index(s) >= ALL_STAGES.index(stage))}")
            return code
    print(f"\nDone: {batch.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
